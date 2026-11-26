"""The leak scan level (strict, standard, relaxed, off) and how a legacy `leak_threshold` maps onto it."""

import pytest

from dom.config import LEAK_SCAN_CUTOFFS, Config, load_config
from test_guard import OUTGOING, FakeJev
from test_traits import put
from test_usage import deliver, online
from tghelpers import GROUP, OWNER, tg


def level_of(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)
    config = load_config(tmp_path)
    return config.leak_level, config.leak_cutoff


def test_each_level_has_its_cutoff_and_standard_is_the_old_default(tmp_path):
    assert LEAK_SCAN_CUTOFFS == {"strict": 0.7, "standard": 1.2, "relaxed": 2.0}
    assert LEAK_SCAN_CUTOFFS["strict"] < LEAK_SCAN_CUTOFFS["standard"] < LEAK_SCAN_CUTOFFS["relaxed"]
    assert Config(home=tmp_path).leak_threshold == 1.2  # what existing instances ran with
    assert level_of(tmp_path, "") == ("standard", 1.2)
    for level, cutoff in LEAK_SCAN_CUTOFFS.items():
        assert level_of(tmp_path, f'leak_scan = "{level}"') == (level, cutoff)
    assert level_of(tmp_path, 'leak_scan = "off"') == ("off", None)
    with pytest.raises(ValueError):
        level_of(tmp_path, 'leak_scan = "paranoid"')


@pytest.mark.parametrize(("threshold", "level"), [
    (0.7, "strict"), (0.62, "strict"), (1.2, "standard"), (1.29, "standard"), (2.0, "relaxed"), (2.08, "relaxed"),
    (1.6, "custom"), (0.0, "custom"), (3.0, "custom"),
])
def test_a_legacy_threshold_maps_to_the_nearest_level_or_stays_custom(tmp_path, threshold, level):
    # The legacy value is still the cutoff that applies, whatever level it is shown as.
    assert level_of(tmp_path, f"leak_threshold = {threshold}") == (level, threshold)


def test_a_chosen_level_beats_a_legacy_threshold_and_the_switch_stays_the_master_off(tmp_path):
    assert level_of(tmp_path, 'leak_scan = "strict"\nleak_threshold = 2.5') == ("strict", 0.7)
    assert level_of(tmp_path, 'leak_check = false\nleak_scan = "strict"') == ("off", None)
    assert level_of(tmp_path, 'leak_check = false\nleak_threshold = 1.6') == ("off", None)


@pytest.mark.parametrize("score", [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0])
@pytest.mark.parametrize(("level", "held"), [
    ("strict", {0.8, 1.0, 1.2, 1.5, 2.0, 3.0}),
    ("standard", {1.2, 1.5, 2.0, 3.0}),
    ("relaxed", {2.0, 3.0}),
    ("off", set()),
])
async def test_replies_are_held_at_each_levels_cutoff(make_app, level, held, score):
    app = make_app(contents=(OUTGOING,))
    await online(app)
    app.config = app.config.model_copy(update={"leak_check": True, "leak_scan": level})
    app.dispatch.jev = fake = FakeJev(score)
    put(app.home, "private/USER.md", "# Access\nThe keypad number is 7319.\n")
    await deliver(app, tg())
    assert len(fake.scored) == (0 if level == "off" else 1)  # off makes no Jev call at all
    assert bool(app.bot.of("send", OWNER)) is (score in held)
    assert bool(app.bot.of("send", GROUP)) is (score not in held)


async def test_off_still_blocks_a_credential_in_a_plugin_input(make_app, tmp_path):
    from dom.guard import check_local_private_text

    app = make_app()
    app.config = app.config.model_copy(update={"leak_scan": "off"})
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=credential-value-12345\n")
    with pytest.raises(ValueError, match="instance credential"):
        check_local_private_text(app.config, app.home, ["use credential-value-12345 for it"])

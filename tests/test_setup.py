"""`dom setup`: where the studio runs (same machine, rented cloud GPU, or external APIs only)."""

import stat
import tomllib

import pytest

from dom import cli, setup
from dom.config import load_config
from dom.home import init_home

TOKEN = "studio-token-value-0001"


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    init_home(root)
    return root


def toml(home):
    return tomllib.loads((home / "config.toml").read_text())


def run(home, answers, **kw):
    said, asked = [], iter(answers)
    code = setup.run_setup(home, ask=lambda _: next(asked), ask_secret=lambda _: next(asked), say=said.append, **kw)
    return code, "\n".join(said)


def test_local_path_points_at_this_machine_and_keeps_the_token_in_env(home):
    todo = setup.apply_studio_path(home, "local", token=TOKEN)
    studio = load_config(home).studio
    assert studio.enabled and studio.base_url == "http://127.0.0.1:8080" and todo == []
    assert (home / ".env").read_text().endswith(f"STUDIO_TOKEN={TOKEN}\n")
    assert TOKEN not in (home / "config.toml").read_text()
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    assert "# Dom instance config." in (home / "config.toml").read_text()  # comments survive
    assert (home / "config.toml.bak").is_file()


def test_local_path_without_a_token_says_where_to_put_it(home):
    todo = setup.apply_studio_path(home, "local", base_url="http://localhost:9000")
    assert load_config(home).studio.base_url == "http://localhost:9000"
    assert todo == ["Put the studio's token in .env as STUDIO_TOKEN, then restart Dom."]
    assert not (home / ".env").exists()


def test_cloud_path_needs_a_remote_address_and_a_token(home):
    before = (home / "config.toml").read_text()
    for kwargs in ({}, {"base_url": "http://gpu.example.test:8080"}, {"token": TOKEN}):
        with pytest.raises(ValueError, match="address and its token"):
            setup.apply_studio_path(home, "cloud", **kwargs)
    assert (home / "config.toml").read_text() == before
    setup.apply_studio_path(home, "cloud", base_url="http://gpu.example.test:8080", token=TOKEN)
    studio = load_config(home).studio
    assert studio.enabled and studio.base_url == "http://gpu.example.test:8080"
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600 and TOKEN in (home / ".env").read_text()


def test_apis_only_path_turns_the_studio_off_and_sets_cloud_backups(home):
    todo = setup.apply_studio_path(home, "apis")
    config = load_config(home)
    studio = config.studio
    assert not studio.enabled and studio.base_url == "" and studio.voice and studio.images
    assert [b.provider for b in studio.voice_fallbacks] == ["openrouter_tts"]
    assert [b.provider for b in studio.image_fallbacks] == ["openrouter_image"]
    assert studio.video_fallbacks == ()  # no video endpoint is zero-data-retention: a deliberate choice later
    assert {b.api_key_env for b in studio.fallbacks} == {"OPENROUTER_MEDIA_API_KEY"}
    assert any("OPENROUTER_MEDIA_API_KEY" in line for line in todo)


def test_apis_path_keeps_backups_the_owner_already_chose(home):
    (home / "config.toml").write_text(
        (home / "config.toml").read_text()
        + '\n[[studio.voice_fallbacks]]\nprovider = "openrouter_tts"\nmodel = "my/voice-model"\napi_key_env = "MY_MEDIA_KEY"\n'
    )
    setup.apply_studio_path(home, "apis")
    studio = load_config(home).studio
    assert [b.model for b in studio.voice_fallbacks] == ["my/voice-model"]
    assert [b.model for b in studio.image_fallbacks] == ["google/gemini-2.5-flash-image"]


def test_a_bad_address_changes_nothing(home):
    before = (home / "config.toml").read_text()
    with pytest.raises(ValueError):
        setup.apply_studio_path(home, "local", base_url="studio.example.test/v1", token=TOKEN)
    assert (home / "config.toml").read_text() == before and not (home / ".env").exists()
    with pytest.raises(ValueError):
        setup.apply_studio_path(home, "local", base_url="http://localhost:9000", token="two words")
    assert (home / "config.toml").read_text() == before and not (home / ".env").exists()


def test_the_wizard_shows_the_zdr_recommendation_once_as_text_and_asks_where_the_studio_runs(home):
    code, said = run(home, ["2", "http://gpu.example.test:8080", TOKEN])
    assert code == 0
    assert said.count(setup.ZDR_RECOMMENDATION) == 1
    assert "warning" not in said.lower()
    assert "On this machine" in said and "rented cloud GPU" in said and "external APIs only" in said
    assert setup.STUDIO_NOTE_TAILSCALE in said and "Tailscale" in said
    assert load_config(home).studio.base_url == "http://gpu.example.test:8080" and TOKEN not in said


@pytest.mark.parametrize(("answer", "enabled"), [("1", True), ("3", False), ("", False)])
def test_the_wizard_offers_three_paths_and_defaults_to_external_apis(home, answer, enabled):
    answers = [answer, "", ""] if answer == "1" else [answer]
    code, said = run(home, answers)
    assert code == 0 and load_config(home).studio.enabled is enabled
    if answer == "1":
        assert load_config(home).studio.base_url == "http://127.0.0.1:8080"
        assert "Next: Put the studio's token in .env" in said


def test_the_wizard_refuses_a_choice_it_does_not_offer_and_reports_a_failed_write(home):
    code, said = run(home, ["9"])
    assert code == 1 and "Choose 1, 2 or 3" in said and not toml(home)["studio"]["enabled"]
    code, said = run(home, ["2", "http://gpu.example.test:8080", ""])
    assert code == 1 and "address and its token" in said


def test_the_cli_takes_flags_and_skips_the_questions(home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _="": pytest.fail("asked"))
    monkeypatch.setattr("getpass.getpass", lambda _="": "")
    assert cli.main(["setup", str(home), "--studio", "apis"]) == 0
    assert not load_config(home).studio.enabled and load_config(home).studio.voice_fallbacks
    assert capsys.readouterr().out.count(setup.ZDR_RECOMMENDATION) == 1
    assert cli.main(["setup", str(home), "--studio", "local", "--studio-url", "http://localhost:9000"]) == 0
    assert load_config(home).studio.base_url == "http://localhost:9000"

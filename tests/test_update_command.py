"""/update in Telegram: owner DM only, one confirmation, deploy launched detached."""

from pathlib import Path

import pytest

from dom import release
from dom.telegram.handlers import handle_callback, handle_message
from test_owner_controls import buttons, callback
from tghelpers import FRIEND, GROUP, OWNER, tg

OFFER = release.Release("v0.10.0", "b" * 40, "## [0.10.0] - 2026-11-20\n\nRelease notes")


@pytest.fixture
def fake_release(tmp_path, monkeypatch):
    layout = release.Layout(tmp_path / "dom")
    checked, launched = [], []
    monkeypatch.setattr(release.Layout, "installed", classmethod(lambda cls, base=None: layout))

    def check(lay, url, current):
        checked.append(url)
        return OFFER

    monkeypatch.setattr(release, "check", check)
    monkeypatch.setattr(release, "launch", lambda lay, home, offer, python=None: launched.append((offer.tag, offer.commit)))
    return checked, launched


def sent(app):
    return [call for call in app.bot.calls if call[0] == "send"]


async def test_update_is_owner_dm_only(make_app, fake_release):
    checked, _ = fake_release
    app = make_app()
    await handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/update"))
    await handle_message(app, tg(chat_id=GROUP, sender=OWNER, text="/update"))
    assert checked == []


async def test_offer_shows_notes_and_deploys_once_on_the_owners_confirmation(make_app, fake_release):
    checked, launched = fake_release
    app = make_app()
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    assert checked == [app.config.update.repo_url]
    _, chat_id, text, kw = sent(app)[-1]
    assert chat_id == OWNER and "v0.10.0" in text and "Release notes" in text
    [[(label, data), (_, cancel)]] = buttons(kw["reply_markup"])
    assert "v0.10.0" in label and data.startswith("update:go:")

    await handle_callback(app, callback(app, FRIEND, data))
    assert launched == []
    await handle_callback(app, callback(app, OWNER, data))
    await handle_callback(app, callback(app, OWNER, data))
    assert launched == [(OFFER.tag, OFFER.commit)]
    assert ("answer", "cb1", "already handled") in app.bot.calls

    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    data = buttons(sent(app)[-1][3]["reply_markup"])[0][1][1]
    await handle_callback(app, callback(app, OWNER, data))  # "Not now" discards the offer
    await handle_callback(app, callback(app, OWNER, buttons(sent(app)[-1][3]["reply_markup"])[0][0][1]))
    assert launched == [(OFFER.tag, OFFER.commit)]


async def test_a_stale_button_cannot_deploy_a_newer_offer(make_app, fake_release):
    _, launched = fake_release
    app = make_app()
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    old = buttons(sent(app)[-1][3]["reply_markup"])[0][0][1]
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    await handle_callback(app, callback(app, OWNER, old))
    assert launched == []


async def test_up_to_date_and_unreleased_layouts_are_reported(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(release.Layout, "installed", classmethod(lambda cls, base=None: release.Layout(Path("/x"))))
    monkeypatch.setattr(release, "check", lambda lay, url, current: None)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    assert "up to date" in sent(app)[-1][2]

    def not_release(cls, base=None):
        raise release.DeployError("Dom is not running from a release folder")

    monkeypatch.setattr(release.Layout, "installed", classmethod(not_release))
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/update"))
    assert "release folder" in sent(app)[-1][2]

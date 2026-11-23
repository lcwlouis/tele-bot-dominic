import re
from datetime import time, timedelta

import pytest

from dom.models import StoredMessage
from dom.presence import read_habits
from dom.web.api import APP_INDEX, activity

from tghelpers import FRIEND, GROUP
from test_web_pages import browser  # noqa: F401 (fixture)


async def reply(app, chat_id, text, msg_id, at):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), role="assistant", text=text, msg_id=msg_id, is_bot=True,
        created_at=at.isoformat(), visibility_scope=app.store.visibility_for(chat_id),
    ))


async def test_home_api_needs_the_dashboard_auth(make_app, browser):
    app = make_app()
    async with browser(app) as client:
        response = await client.get("/api/home", headers={"Authorization": ""})
    assert response.status_code == 403


async def test_home_api_before_the_owner_claims_dom(make_app, browser):
    async with browser(make_app(owner_id=None)) as client:
        response = await client.get("/api/home")
    assert response.json() == {"setup": True}


async def test_home_api_reads_presence_vibe_waiting_today_and_recent_replies(make_app, browser):
    app = make_app()
    app.home.write_habits_line(time(23), time(7))
    app.home.write_preset("classic", "---\ntitle: Classic\ndescription: Easy and warm.\ntraits:\n  humor: 0.8\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    await app.store.set_chat_auth_status(-2002, "ignored", title="Strangers")
    await app.dispatch.presence.wake(GROUP, read_habits(app.home))
    now = app.dispatch.now()
    app.home._write_regular("SOUL.md", "One old line")
    await app.proposals.create("soul_edit", "Add a line about tea", {"old": "One old line", "new": "Tea"}, "reflection")
    await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.25, 1)
    await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, 5, "Replied in 1 bubble")
    await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, 5, "Stayed quiet")
    await app.dispatch.reminders.create((now + timedelta(hours=1)).replace(tzinfo=None).isoformat(), "Water the plants")
    await reply(app, GROUP, "An older reply", 1, now - timedelta(minutes=9))
    await reply(app, GROUP, "See you all at eight.", 2, now - timedelta(minutes=5))
    await reply(app, FRIEND, "Take your time.", 3, now - timedelta(minutes=7))
    await reply(app, -2002, "Not approved, never shown", 4, now)

    async with browser(app) as client:
        response = await client.get("/api/home")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["setup"] is False
    assert body["presence"]["state"] == "online"
    assert body["presence"]["title"] == "Online in Book Club"
    assert body["activity"] == "idle"
    assert body["vibe"]["mode"] in ("auto", "none", "locked")
    assert body["waiting"]["count"] == 1
    assert body["waiting"]["items"] == [{"label": "Soul edit, suggested in reflection", "detail": "Add a line about tea"}]
    today = body["today"]
    assert (today["turns"], today["replies"], today["spend"]) == (2, 1, 0.25)
    assert today["cap"] == app.config.daily_spend_cap_usd
    assert len(today["hourly"]) == 24
    assert today["next_reminder"]["text"] == "Water the plants"
    assert [(row["chat"], row["text"]) for row in body["recent"]] == [
        ("Book Club", "See you all at eight."), ("DM with Reader", "Take your time."),
    ]
    assert body["desktop"] == {"enabled": False, "task": None}


@pytest.mark.parametrize(("replying", "desktop_task", "presence", "expected"), [
    (True, True, "asleep", "replying"),
    (False, True, "online", "desktop"),
    (False, False, "asleep", "asleep"),
    (False, False, "online", "idle"),
    (False, False, "offline", "idle"),
])
def test_orbit_activity_prefers_a_turn_then_a_desktop_task_then_sleep(replying, desktop_task, presence, expected):
    assert activity(replying=replying, desktop_task=desktop_task, presence=presence) == expected


@pytest.mark.parametrize("path", ["/app", "/app/", "/app/work/reminders", "/app/gallery"])
async def test_every_app_path_serves_the_built_page_under_the_csp(make_app, browser, path):
    async with browser(make_app()) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert response.text == APP_INDEX.read_text()


def test_built_page_has_no_inline_script_style_or_external_host():
    page = APP_INDEX.read_text()
    assert "<style" not in page and " style=" not in page
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts and all(re.search(r'\bsrc="/static/app/', tag) for tag in scripts)
    assert not re.search(r'(?:src|href)="(?:https?:)?//', page)


def test_built_app_files_name_no_external_host():
    assets = APP_INDEX.parent / "assets"
    for path in assets.iterdir():
        if path.suffix in (".js", ".css"):
            text = path.read_text()
            assert not re.search(r"url\((?:['\"])?(?:https?:)?//", text), path.name
            assert "data:font" not in text and "data:image" not in text, path.name



def test_a_recent_reply_is_a_one_line_preview():
    from dom.web.api import _preview

    assert _preview("See you\n  all at eight.") == "See you all at eight."
    long = _preview("word " * 60)
    assert len(long) == 120 and long.endswith("…")


async def test_a_missing_file_under_app_is_a_404_not_the_page(make_app, browser):
    async with browser(make_app()) as client:
        missing = await client.get("/app/assets/missing.js")
        head = await client.head("/app/gallery")
    assert missing.status_code == 404
    assert head.status_code == 200

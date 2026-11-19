from datetime import time, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from dom.models import StoredMessage
from dom.presence import read_habits
from dom.telegram import owner
from dom.telegram.handlers import handle_callback

from tghelpers import FRIEND, GROUP, OWNER, tg
from test_owner_controls import callback
from test_web_auth import ORIGIN, POST_HEADERS


@pytest.fixture
def browser(monkeypatch):
    from dom.web.app import create_app
    from dom.web.auth import Auth

    monkeypatch.setenv("DOM_TEST_TOKEN", "test-token")

    def connect(app):
        config = app.config.model_copy(update={
            "web": app.config.web.model_copy(update={"dev_token_env": "DOM_TEST_TOKEN"}),
        })
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app, Auth(config))),
            base_url=ORIGIN, headers={"Authorization": "Bearer test-token"},
        )

    return connect


async def test_dashboard_pages_and_day_dial(make_app, browser):
    app = make_app()
    app.home.write_habits_line(time(23), time(7))
    app.home.write_preset("classic", "---\ntitle: Classic\ntraits:\n  humor: 0.8\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    await app.store.set_chat_preset(GROUP, "classic")
    now = app.dispatch.now()
    for sender in (OWNER, FRIEND):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(GROUP), role="user", text="Hello", msg_id=sender,
            sender_user_id=sender, created_at=(now - timedelta(hours=1)).isoformat(),
            visibility_scope=app.store.visibility_for(GROUP),
        ))
    habits = read_habits(app.home)
    await app.dispatch.presence.wake(OWNER, habits)
    app.dispatch.now.advance(minutes=5)
    await app.dispatch.presence.sleep(OWNER, habits)
    await app.dispatch.presence.wake(GROUP, habits)
    app.dispatch.now.advance(minutes=5)
    app.home._write_regular("SOUL.md", "One old line")
    await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "<b>A new line</b>"}, "reflection")
    await app.memory_import.run("chatgpt", "- Likes a library\n- Has a notebook")
    await app.memory_import.run("chatgpt", "- Likes a walk")
    await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.8, 1)
    await app.store.record_usage(
        (now - timedelta(days=1)).isoformat(), "search", "search-model", "example.org", GROUP, 2, 3, 0.1, 0,
    )
    await app.store.record_turn(now, GROUP, "group", 2, ["web_search", "final"], "example-model", True, 250, "Replied")
    await app.store.record_turn(now, OWNER, "owner_dm", 1, ["final"], "example-model", False, 50, "Stayed quiet")

    async with browser(app) as client:
        index = await client.get("/")
        assert index.status_code == 200
        assert all(label in index.text for label in ("Waiting for you", "Chats", "Usage", "Turn log"))
        assert 'id="dial"' in index.text and "Online in Book Club" in index.text and "until 12:35" in index.text
        assert "23:00 to 07:00" in index.text
        assert index.text.count('class="online-arc"') == 2
        assert 'data-hour="11"' in index.text and 'data-count="2"' in index.text
        assert "/static/dashboard.css" in index.text and "/static/htmx.min.js" in index.text
        assert "<style" not in index.text and " style=" not in index.text
        chats = (await client.get("/chats")).text
        assert all(label in chats for label in ("Classic", "Humor", "Your DM", "Friend DM", "Reader", "Leave group"))
        proposals = (await client.get("/proposals")).text
        assert "&lt;b&gt;A new line&lt;/b&gt;" in proposals and "Approve all" in proposals
        assert proposals.count("memories imported from ChatGPT") == 1
        assert "1 memory imported from ChatGPT" in proposals
        assert "Likes a library" not in (await client.get("/proposals?kind=soul_edit")).text
        assert "A change" not in (await client.get("/proposals?source=import%3Achatgpt")).text
        usage = (await client.get("/usage")).text
        assert "$0.80" in usage and "$0.10" in usage and "search-model" in usage and 'class="fallback-mark"' in usage
        turns = (await client.get(f"/turns?chat={GROUP}")).text
        assert "Replied" in turns and "Stayed quiet" not in turns and "Search" in turns and "Fallback" in turns
        assert (await client.get("/static/htmx.min.js")).status_code == 200


@pytest.mark.parametrize("action,callback_data,shared", [
    ("wake", f"wake:{GROUP}", "override_chat"),
    ("sleep", f"sleep:{GROUP}", "override_chat"),
    ("leave", f"leaveyes:{GROUP}", "leave_chat"),
    ("preset", f"presetset:{GROUP}:classic", "set_preset"),
])
async def test_chat_actions_share_owner_functions(make_app, browser, monkeypatch, action, callback_data, shared):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    app.home.write_preset("classic", "A preset")
    call = AsyncMock(wraps=getattr(owner, shared))
    monkeypatch.setattr(owner, shared, call)
    async with browser(app) as client:
        response = await client.post(
            f"/chats/{GROUP}/{action}", headers=POST_HEADERS, data={"preset": "classic"},
        )
    assert response.status_code == 200
    assert call.await_count == 1
    if action == "leave":
        await app.store.set_chat_auth_status(GROUP, "approved")
    await handle_callback(app, callback(app, OWNER, callback_data))
    assert call.await_count == 2
    if action == "leave":
        assert app.bot.of("leave", GROUP)
    assert any(message.role == "event" for message in await app.store.get_recent_chat_window(OWNER))


async def test_web_decides_through_proposals_including_selected_imports(make_app, browser, monkeypatch):
    app = make_app()
    await app.memory_import.run("chatgpt", "- Likes a library\n- Has a notebook")
    await app.memory_import.run("chatgpt", "- Likes a walk")
    first, second, third = await app.proposals.pending()
    decide = AsyncMock(wraps=app.proposals.decide)
    monkeypatch.setattr(app.proposals, "decide", decide)
    async with browser(app) as client:
        response = await client.post(
            f"/proposals/import/{first['id']}", headers=POST_HEADERS,
            data={"decision": "selected", "selected": [first["id"], third["id"]]},
        )
        assert response.status_code == 200
        decide.assert_awaited_once_with(first["id"], True)
        assert [p["id"] for p in await app.proposals.pending()] == [second["id"], third["id"]]
        response = await client.post(
            f"/proposals/{second['id']}/reject", headers=POST_HEADERS,
        )
        assert response.status_code == 200
        decide.assert_awaited_with(second["id"], False)


async def test_agent_and_dispatch_write_turn_and_daily_retention(make_app):
    app = make_app()
    now = app.dispatch.now()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.dispatch.presence.wake(GROUP, read_habits(app.home))
    from dom.telegram import handlers
    from tghelpers import drain_dispatch

    await handlers.handle_message(app, tg(chat_id=GROUP))
    await drain_dispatch(app.dispatch)
    rows = await app.store.recent_turns()
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_id"] == str(GROUP)
    assert row["scope"] == "group" and row["calls"] == 1 and row["model"] == "m"
    assert row["trace"] == '["final"]' and row["fallback"] == 0
    assert row["outcome"] == "Replied in 1 bubble" and row["duration_ms"] >= 0
    for age in (30, 31):
        await app.store.record_turn(now - timedelta(days=age), OWNER, "owner_dm", 0, [], "", False, 0, "Stayed quiet")
    await app.dispatch.jobs.tick()
    assert len(await app.store.recent_turns()) == 2

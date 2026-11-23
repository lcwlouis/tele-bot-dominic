"""What the dashboard APIs share with Telegram and the stores: the owner functions, the proposals store, the turn log
and the day's figures. (The old server-rendered pages were removed in v0.8.2; these ran through them.)"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from dom.presence import read_habits
from dom.telegram import owner
from dom.telegram.handlers import handle_callback

from tghelpers import FRIEND, GROUP, OWNER, tg
from test_owner_controls import callback
from test_web_auth import POST_HEADERS


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
        if action == "preset":
            response = await client.post("/api/chats/preset", json={"chat_id": GROUP, "preset": "classic"}, headers=POST_HEADERS)
        else:
            response = await client.post("/api/chats/action", json={"chat_id": GROUP, "action": action}, headers=POST_HEADERS)
    assert response.status_code == 200
    assert call.await_count == 1
    if action == "leave":
        await app.store.set_chat_auth_status(GROUP, "approved")
    await handle_callback(app, callback(app, OWNER, callback_data))
    assert call.await_count == 2
    if action == "leave":
        assert app.bot.of("leave", GROUP)
    assert any(message.role == "event" for message in await app.store.get_recent_chat_window(OWNER))


async def test_the_api_decides_through_proposals_including_selected_imports(make_app, browser, monkeypatch):
    app = make_app()
    await app.memory_import.run("chatgpt", "- Likes a library\n- Has a notebook")
    await app.memory_import.run("chatgpt", "- Likes a walk")
    first, second, third = await app.proposals.pending()
    decide = AsyncMock(wraps=app.proposals.decide)
    monkeypatch.setattr(app.proposals, "decide", decide)
    async with browser(app) as client:
        response = await client.post(
            "/api/waiting/import", headers=POST_HEADERS,
            json={"id": first["id"], "decision": "selected", "selected": [first["id"], third["id"]]},
        )
        assert response.status_code == 200
        decide.assert_awaited_once_with(first["id"], True)
        assert [p["id"] for p in await app.proposals.pending()] == [second["id"], third["id"]]
        response = await client.post("/api/waiting/decide", headers=POST_HEADERS, json={"id": second["id"], "decision": "reject"})
        assert response.status_code == 200
        decide.assert_awaited_with(second["id"], False)


async def test_deciding_on_the_dashboard_settles_the_telegram_card(make_app, browser):
    # Plugin #7 was approved on the dashboard and its Telegram card kept live Approve and Reject buttons.
    app = make_app()
    skill = {"name": "report", "description": "Summarise a web table", "body": "Open the page and summarise the table."}
    proposal_id = await app.proposals.create("skill", "Skill: report", skill, "chat")
    card_id = app.bot._next_id - 1  # the card, then the skill's procedure attached as a file
    async with browser(app) as client:
        response = await client.post("/api/waiting/decide", headers=POST_HEADERS, json={"id": proposal_id, "decision": "approve"})
    assert response.status_code == 200
    _, chat_id, message_id, markup = app.bot.of("markup")[-1]
    assert (chat_id, message_id) == (OWNER, card_id)
    (button,), = markup.inline_keyboard
    assert (button.text, button.callback_data) == ("✅ Approved", "noop")
    await app.proposals.settle_cards()  # settled once: nothing more to edit
    assert len(app.bot.of("markup")) == 1


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
    trace = json.loads(row["trace"])
    assert trace[0] == "final" and [item.partition("@")[0] for item in trace if "@" in item] == [
        "debounce", "model", "typing", "send",
    ]
    assert row["fallback"] == 0
    assert row["outcome"] == "Replied in 1 bubble" and row["duration_ms"] >= 0
    for age in (30, 31):
        await app.store.record_turn(now - timedelta(days=age), OWNER, "owner_dm", 0, [], "", False, 0, "Stayed quiet")
    await app.dispatch.jobs.tick()
    assert len(await app.store.recent_turns()) == 2


async def test_turn_timing_bar_and_call_details_are_owner_only(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    call = {
        "kind": "chat", "model": "test-model", "fallback": False, "status": 200, "duration_ms": 1900,
        "input_tokens": 2048, "output_tokens": 16, "cached_tokens": 1024,
        "system_hash": "systemhash", "tools_hash": "toolshash", "user_hash": "userhash",
        "user_prefix_hash": "prefixhash",
    }
    now = app.dispatch.now()
    await app.store.record_turn(
        now, GROUP, "group", 1, ["final", "lock_wait@2500", "model@1900", "typing@1600", "send@100"],
        "test-model", False, 3700, "Replied in 2 bubbles", model_calls=[call],
        first_bubble_ms=5100, last_bubble_ms=6100,
    )
    await app.store.record_turn(now, OWNER, "owner_dm", 0, [], "", False, 0, "Stayed quiet")
    async with browser(app) as client:
        page = await client.get("/api/system/turns", params={"chat": GROUP})
        assert page.status_code == 200
        turn, = page.json()["turns"]
        assert turn["title"] == "Book Club" and turn["outcome"] == "Replied in 2 bubbles"
        assert [(s["stage"], s["x"], s["ms"]) for s in turn["segments"]] == [("lock_wait", 0, 2500), ("model", 2500, 1900), ("typing", 4400, 1600), ("send", 6000, 100)]
        assert (turn["first_bubble_ms"], turn["last_bubble_ms"]) == (5100, 6100)
        shown, = turn["model_calls"]
        assert (shown["input_tokens"], shown["output_tokens"], shown["cached_tokens"]) == (2048, 16, 1024)
        assert [shown[key] for key in ("system_hash", "tools_hash", "user_hash", "user_prefix_hash")] == ["systemhash", "toolshash", "userhash", "prefixhash"]
        client.headers.pop("Authorization")
        denied = await client.get("/api/system/turns", params={"chat": GROUP})
        assert denied.status_code == 403 and "systemhash" not in denied.text


@pytest.mark.parametrize("kind,source,label", [
    ("soul_edit", "reflection", "Soul edit, suggested in reflection"),
    ("soul_edit", "dashboard", "Soul edit, asked for in the dashboard"),
    ("memory_fact", "import:chatgpt", "Memory imported from ChatGPT"),
    ("memory_fact", "import:muse", "Memory imported from Muse"),
    ("skill", "desktop", "Skill proposed after a desktop task"),
    ("plugin", "draft_plugin", "Plugin draft"),
])
def test_proposal_labels_are_sentences(kind, source, label):
    from dom.web.shared import _proposal_label

    assert _proposal_label(kind, source) == label
    assert "·" not in label


async def test_the_days_figures_read_usage_turns_and_reminders(make_app, browser):
    app = make_app()
    now = app.dispatch.now()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    async with browser(app) as client:
        today = lambda: client.get("/api/home")  # noqa: E731
        empty = (await today()).json()["today"]
        assert (empty["turns"], empty["replies"], empty["spend"], empty["next_reminder"]) == (0, 0, 0, None)
        await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.8, 0)
        await app.store.record_usage((now - timedelta(days=1)).isoformat(), "chat", "m", "example.org", GROUP, 2, 3, 5, 0)
        for outcome in ("Replied in 2 bubbles", "Replied privately", "Stayed quiet", "Reacted \U0001f923"):
            await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, 5, outcome)
        await app.store.record_turn(now - timedelta(days=1), GROUP, "group", 1, [], "m", False, 5, "Replied in 1 bubble")
        await app.dispatch.reminders.create("2026-01-01T18:30:00", "Water the plants")
        await app.dispatch.reminders.create("2026-01-01T15:00:00", "Call the dentist")
        filled = (await today()).json()["today"]
    assert (filled["turns"], filled["replies"], filled["spend"]) == (4, 2, 0.8)
    assert filled["next_reminder"] == {"when": "15:00", "text": "Call the dentist"}


async def test_today_counts_all_turns_across_the_owners_local_day(make_app, browser):
    app = make_app()
    zone = ZoneInfo("America/New_York")
    app.config = app.config.model_copy(update={"timezone": zone.key})
    start = datetime(2026, 11, 1, tzinfo=zone)  # This local day has 25 hours.
    end = start + timedelta(days=1)
    app.dispatch.now.at = end - timedelta(minutes=1)

    async def record(at, outcome):
        await app.store.record_turn(at, OWNER, "owner_dm", 1, [], "m", False, 5, outcome)

    await record(start - timedelta(microseconds=1), "Replied")
    for minute in range(205):
        await record(start + timedelta(minutes=minute), "Replied privately" if minute % 2 == 0 else "replied")
    await record(app.dispatch.now(), "Replied")
    await record(end, "Replied")

    async with browser(app) as client:
        today = (await client.get("/api/home")).json()["today"]
    assert today["turns"] == 206
    assert today["replies"] == 104


async def test_a_reminder_on_another_day_names_the_day(make_app, browser):
    app = make_app()
    await app.dispatch.reminders.create("2026-01-03T09:00:00", "Renew the pass")
    async with browser(app) as client:
        assert (await client.get("/api/home")).json()["today"]["next_reminder"] == {"when": "Sat 09:00", "text": "Renew the pass"}


async def test_pending_chat_approvals_share_the_owner_decision_function(make_app, browser, monkeypatch):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "pending_approval", title="Book Club", invited_by_handle="@inviter")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval", title="Fran", invited_by_handle="@newbie")
    decide = AsyncMock(wraps=owner.decide_pending_chat)
    monkeypatch.setattr(owner, "decide_pending_chat", decide)

    async with browser(app) as client:
        waiting = (await client.get("/api/waiting")).json()
        by_id = {chat["id"]: chat for chat in waiting["chats"]}
        assert by_id[GROUP]["label"] == "Group approval" and by_id[GROUP]["title"] == "Book Club"
        assert "Invited by @inviter" in by_id[GROUP]["detail"]
        assert by_id[FRIEND]["label"] == "Stranger DM approval" and by_id[FRIEND]["title"] == "DM with Fran"
        assert "From @newbie" in by_id[FRIEND]["detail"] and waiting["count"] == 2

        # Filtering by kind includes or excludes pending chats properly
        app.home._write_regular("SOUL.md", "One old line")
        await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "A new line"}, "reflection", notify=False)
        only_soul = (await client.get("/api/waiting", params={"kind": "soul_edit"})).json()
        assert [card["title"] for card in only_soul["cards"]] == ["A change"] and only_soul["chats"] == []
        only_chats = (await client.get("/api/waiting", params={"kind": "chat_approval"})).json()
        assert len(only_chats["chats"]) == 2 and only_chats["cards"] == []

        approved = await client.post("/api/waiting/chat", headers=POST_HEADERS, json={"chat_id": GROUP, "decision": "approve"})
        assert approved.status_code == 200 and "Approved: Book Club" in approved.json()["message"]
        assert [chat["id"] for chat in approved.json()["chats"]] == [FRIEND]
        assert await app.store.is_chat_approved(GROUP)
        assert decide.await_count == 1

        # Re-deciding the same chat reports Already handled
        stale = await client.post("/api/waiting/chat", headers=POST_HEADERS, json={"chat_id": GROUP, "decision": "ignore"})
        assert stale.status_code == 200 and stale.json()["message"] == "Already handled."
        assert await app.store.is_chat_approved(GROUP)

    # Telegram owner-DM button uses the exact same decision function
    await handle_callback(app, callback(app, OWNER, f"ignore:{FRIEND}"))
    assert decide.await_count == 3
    assert (await app.store.get_chat_auth_status(FRIEND))["authorization_status"] == "ignored"
    events = await app.store.get_recent_chat_window(OWNER)
    event_texts = [m.text for m in events if m.role == "event"]
    assert any("approved Book Club" in text for text in event_texts)
    assert any("ignored DM with Fran" in text for text in event_texts)


async def test_an_oversized_upload_stops_being_read_at_the_limit():
    import pytest
    from fastapi import HTTPException

    from dom.web.shared import _bounded_stream

    read = []

    class Request:
        async def stream(self):
            for chunk in (b"a" * 6, b"b" * 6, b"c" * 6):
                read.append(chunk)
                yield chunk

    with pytest.raises(HTTPException) as refused:
        async for _ in _bounded_stream(Request(), 10):
            pass
    assert refused.value.status_code == 413 and len(read) == 2  # the third chunk is never read

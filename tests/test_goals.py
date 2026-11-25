"""Tests for owner-only goals (R6): CRUD, history, reminder links, privacy filter, check-ins and API."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from dom.config import OpenersConfig
from dom.goals import CATEGORIES, RHYTHMS, STATUSES, prompt_block
from dom.models import StoredMessage
from dom.presence import parse_habits
from dom.tools import GOALS_TOOL, REMIND_TOOL, TurnCtx, enabled_tools
from test_openers import Draw, NO_RESPONSE, always_try, record as opener_record
from test_usage import deliver, online
from test_web_api_memory_chats import JSON
from tghelpers import FINAL, FRIEND, GROUP, OWNER, tg, turn_requests


async def test_goal_crud_states_history_and_reminder_links(make_app):
    app = make_app()
    now = app.dispatch.now()

    # Create with category, target date and rhythm
    goal = await app.goals.create(
        "Run 3 times a week",
        category="health",
        target_date="2026-06-01",
        check_in_rhythm="weekly",
    )
    assert goal["id"] == 1
    assert goal["text"] == "Run 3 times a week"
    assert goal["category"] == "health"
    assert goal["target_date"] == "2026-06-01"
    assert goal["check_in_rhythm"] == "weekly"
    assert goal["status"] == "active"
    assert [h["kind"] for h in goal["history"]] == ["created"]

    # Update text and target date
    updated = await app.goals.update(goal["id"], text="Run 4 times a week", target_date="2026-07-01")
    assert updated["text"] == "Run 4 times a week"
    assert updated["target_date"] == "2026-07-01"
    assert updated["category"] == "health"

    # Add progress note
    progressed = await app.goals.add_progress(goal["id"], "Did 3 runs this week")
    assert progressed["progress_note"] == "Did 3 runs this week"

    # Link and unlink a reminder
    rid = await app.dispatch.reminders.create((now + timedelta(hours=2)).replace(tzinfo=None).isoformat(), "Lace up shoes")
    linked = await app.goals.link_reminder(goal["id"], rid)
    assert [r["id"] for r in linked["reminders"]] == [rid]

    unlinked = await app.goals.unlink_reminder(goal["id"], rid)
    assert unlinked["reminders"] == []

    # Re-link and verify deleting the reminder cleans up the join table
    await app.goals.link_reminder(goal["id"], rid)
    rem_row = await app.store.get_reminder(OWNER, rid)
    ok, _ = await app.dispatch.reminders.delete(rid, rem_row["created_at"])
    assert ok
    after_rem_delete = await app.goals.get(goal["id"])
    assert after_rem_delete["reminders"] == []

    # State transitions: active -> paused -> active -> done -> dropped, all recorded in history
    for status, note in (("paused", "traveling"), ("active", "back home"), ("done", "hit target"), ("dropped", "moved on")):
        changed = await app.goals.set_status(goal["id"], status, note=note)
        assert changed["status"] == status
        if status in ("done", "dropped"):
            assert changed["closed_at"] is not None
        else:
            assert changed["closed_at"] is None

    full = await app.goals.get(goal["id"])
    status_transitions = [(h["from_status"], h["to_status"]) for h in full["history"] if h["kind"] == "status"]
    assert status_transitions == [
        ("done", "dropped"),
        ("active", "done"),
        ("paused", "active"),
        ("active", "paused"),
    ]

    # Delete with stale stamp fails, real stamp archives and deletes
    ok, msg = await app.goals.delete(goal["id"], "wrong-stamp")
    assert not ok and "changed" in msg.lower()
    ok, msg = await app.goals.delete(goal["id"], full["created_at"])
    assert ok
    assert await app.goals.get(goal["id"]) is None


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"text": "   "}, "non-empty"),
    ({"text": "Save money", "category": "bogus"}, "category"),
    ({"text": "Save money", "target_date": "2026-02-30"}, "target_date"),
    ({"text": "Save money", "target_date": "next week"}, "YYYY-MM-DD"),
    ({"text": "Save money", "check_in_rhythm": "hourly"}, "check_in_rhythm"),
])
async def test_goal_validation_rejects_bad_inputs(make_app, kwargs, match):
    app = make_app()
    with pytest.raises(ValueError, match=match):
        await app.goals.create(**kwargs)


async def test_goal_tool_and_remind_goal_link_in_owner_dm(make_app):
    app = make_app()
    now = app.dispatch.now()
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, OWNER, reminders=app.dispatch.reminders, app=app)

    created = json.loads(await GOALS_TOOL.handler({
        "action": "create", "text": "Read 20 pages a day", "category": "interests",
        "target_date": "2026-12-31", "check_in_rhythm": "every_few_days",
    }, ctx))
    gid = created["id"]

    rem = json.loads(await REMIND_TOOL.handler({
        "when": (now + timedelta(hours=3)).replace(tzinfo=None).isoformat(),
        "text": "Read before bed",
        "goal_id": gid,
    }, ctx))
    assert rem["goal_id"] == gid

    listed = json.loads(await GOALS_TOOL.handler({"action": "list"}, ctx))
    assert len(listed) == 1
    assert listed[0]["text"] == "Read 20 pages a day"
    assert [r["id"] for r in listed[0]["reminders"]] == [rem["id"]]

    noted = json.loads(await GOALS_TOOL.handler({"action": "note", "goal_id": gid, "note": "Finished chapter 2"}, ctx))
    assert noted["progress_note"] == "Finished chapter 2"

    paused = json.loads(await GOALS_TOOL.handler({"action": "set_status", "goal_id": gid, "status": "paused"}, ctx))
    assert paused["status"] == "paused"

    bad_link = await REMIND_TOOL.handler({
        "when": (now + timedelta(hours=4)).replace(tzinfo=None).isoformat(),
        "text": "Orphan",
        "goal_id": 9999,
    }, ctx)
    assert bad_link.startswith("error:")


async def test_goals_privacy_filter_keeps_goals_out_of_groups_and_friends(make_app):
    app = make_app()
    secret_goal = "Pay off secret credit card debt"
    await app.goals.create(secret_goal, category="money", check_in_rhythm="weekly")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Fran")

    # 1. Tool offered ONLY in owner_dm, never in group or friend_dm
    assert "goals" in [t.name for t in await enabled_tools(app, "owner_dm")]
    assert "goals" not in [t.name for t in await enabled_tools(app, "group")]
    assert "goals" not in [t.name for t in await enabled_tools(app, "friend_dm")]

    # 2. Direct tool invocation from non-owner scopes or wrong chat/sender is refused in code
    for bad_ctx in (
        TurnCtx("group", GROUP, FRIEND, app.home, app.store, OWNER, app=app),
        TurnCtx("friend_dm", FRIEND, FRIEND, app.home, app.store, OWNER, app=app),
        TurnCtx("owner_dm", GROUP, OWNER, app.home, app.store, OWNER, app=app),
        TurnCtx("owner_dm", OWNER, FRIEND, app.home, app.store, OWNER, app=app),
    ):
        res = await GOALS_TOOL.handler({"action": "list"}, bad_ctx)
        assert res.startswith("error:") and secret_goal not in res

    # 3. prompt_block / turn_note returns empty string outside owner_dm
    active = await app.goals.active()
    assert secret_goal in prompt_block(active, "owner_dm", OWNER, OWNER)
    assert prompt_block(active, "group", GROUP, OWNER) == ""
    assert prompt_block(active, "friend_dm", FRIEND, OWNER) == ""
    assert prompt_block(active, "owner_dm", FRIEND, OWNER) == ""
    assert await app.goals.turn_note("group", GROUP) == ""
    assert await app.goals.turn_note("friend_dm", FRIEND) == ""

    # 4. Actual dispatched turns: owner_dm sees the goal; group and friend_dm never see it
    await online(app, GROUP)
    await online(app, FRIEND)
    await online(app, OWNER)
    await deliver(app, tg(GROUP, FRIEND, "hey dom", msg_id=10))
    await deliver(app, tg(FRIEND, FRIEND, "hey dom", msg_id=11))
    await deliver(app, tg(OWNER, OWNER, "hey dom", msg_id=12))

    reqs = turn_requests(app)
    assert len(reqs) == 3
    assert secret_goal not in json.dumps(reqs[0])
    assert secret_goal not in json.dumps(reqs[1])
    assert secret_goal in json.dumps(reqs[2])


async def test_goal_checkin_respects_rhythm_and_opener_daily_limit(make_app):
    app = make_app(contents=(NO_RESPONSE, FINAL, FINAL, FINAL))
    openers, now = always_try(app, enabled=True, max_per_day=2)

    # Goal with no rhythm never triggers check-in
    await app.goals.create("Learn guitar", category="interests", check_in_rhythm="")
    # Goal with weekly rhythm created right now is not due yet
    weekly = await app.goals.create("Stretch every morning", category="health", check_in_rhythm="weekly")
    # Paused daily goal must never trigger check-in
    paused = await app.goals.create("Paused goal", category="productivity", check_in_rhythm="daily")
    await app.goals.set_status(paused["id"], "paused")

    assert await app.goals.due_checkin(now()) is None

    # Advance 6 days: still not due for weekly
    now.advance(days=6)
    assert await app.goals.due_checkin(now()) is None

    # Advance 1 more day (day 7): now due!
    now.advance(days=1)
    due = await app.goals.due_checkin(now())
    assert due is not None and due["id"] == weekly["id"]

    # First attempt answers no_response -> must NOT mark last_checkin_at
    await openers.maybe(now())
    assert not app.bot.of("send", OWNER)
    assert (await app.goals.get(weekly["id"]))["last_checkin_at"] is None
    assert "Stretch every morning" in turn_requests(app)[-1]["messages"][1]["content"]

    # Wait spacing (3h) and try again -> sends check-in and records last_checkin_at
    now.advance(hours=3)
    await openers.maybe(now())
    assert len(app.bot.of("send", OWNER)) == 1
    checked = await app.goals.get(weekly["id"])
    assert checked["last_checkin_at"] is not None
    assert any(h["kind"] == "checkin" for h in checked["history"])

    # Owner replies, 3h pass, day 8 -> weekly goal is NOT due again yet (ordinary opener runs instead)
    await opener_record(app, 6000, chat_id=OWNER, minutes_ago=0)
    await app.store.mark_chat_read(OWNER, 6000)
    now.advance(days=1)
    assert await app.goals.due_checkin(now()) is None
    await openers.maybe(now())
    assert "<goal_checkin>" not in turn_requests(app)[-1]["messages"][1]["content"]

    # Advance to day 14 (7 days after check-in) -> due again, but blocked when openers.max_per_day is reached
    now.advance(days=6)
    await opener_record(app, 6005, chat_id=OWNER, minutes_ago=60)
    await app.store.mark_chat_read(OWNER, 6005)
    assert (await app.goals.due_checkin(now()))["id"] == weekly["id"]
    day = openers._day(now())
    await app.store.kv_set("opener:sent", f"{day}/2")  # daily limit already hit
    sent_before = len(app.bot.of("send", OWNER))
    await openers.maybe(now())
    assert len(app.bot.of("send", OWNER)) == sent_before


async def test_goals_owner_only_api(make_app, browser):
    app = make_app()
    now = app.dispatch.now()
    rid = await app.dispatch.reminders.create((now + timedelta(hours=5)).replace(tzinfo=None).isoformat(), "Check budget")

    async with browser(app) as client:
        # Auth and same-origin JSON required
        assert (await client.get("/api/goals", headers={"Authorization": ""})).status_code == 403
        assert (await client.post("/api/goals/create", json={"text": "No origin"})).status_code == 403
        assert (await client.post("/api/goals/create", json={"text": "Bad origin"}, headers={"Origin": "https://evil.example.org"})).status_code == 403

        # Create
        r = await client.post("/api/goals/create", json={
            "text": "Save 20% of income", "category": "money", "target_date": "2026-12-01", "check_in_rhythm": "monthly",
        }, headers=JSON)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        gid = data["goal"]["id"]

        # Read list & detail
        listing = (await client.get("/api/goals")).json()
        assert len(listing["goals"]) == 1
        assert set(listing["categories"]) == {c for c in CATEGORIES if c}
        assert set(listing["rhythms"]) == {r for r in RHYTHMS if r}
        assert listing["statuses"] == list(STATUSES)

        detail = (await client.get(f"/api/goals/{gid}")).json()["goal"]
        assert detail["text"] == "Save 20% of income"

        # Update + progress note + link/unlink reminder + status change
        assert (await client.post("/api/goals/update", json={"id": gid, "text": "Save 25%", "clear_target_date": True}, headers=JSON)).json()["goal"]["target_date"] is None
        assert (await client.post("/api/goals/note", json={"id": gid, "note": "Hit 22% in Jan"}, headers=JSON)).json()["goal"]["progress_note"] == "Hit 22% in Jan"
        linked = (await client.post("/api/goals/link-reminder", json={"goal_id": gid, "reminder_id": rid}, headers=JSON)).json()["goal"]
        assert [x["id"] for x in linked["reminders"]] == [rid]
        unlinked = (await client.post("/api/goals/unlink-reminder", json={"goal_id": gid, "reminder_id": rid}, headers=JSON)).json()["goal"]
        assert unlinked["reminders"] == []
        assert (await client.post("/api/goals/status", json={"id": gid, "status": "done", "note": "Reached!"}, headers=JSON)).json()["goal"]["status"] == "done"

        # Status filter
        assert len((await client.get("/api/goals?status=active")).json()["goals"]) == 0
        assert len((await client.get("/api/goals?status=done")).json()["goals"]) == 1

        # Home shows active count
        assert (await client.get("/api/home")).json()["goals"] == {"active_count": 0}

        # Delete
        created_at = detail["created_at"]
        assert (await client.post("/api/goals/delete", json={"id": gid, "expected": "stale"}, headers=JSON)).status_code == 409
        deleted = await client.post("/api/goals/delete", json={"id": gid, "expected": created_at}, headers=JSON)
        assert deleted.status_code == 200 and deleted.json()["goals"] == []
        assert (await client.get(f"/api/goals/{gid}")).status_code == 404

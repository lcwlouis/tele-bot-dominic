"""Tests for owner-only noticings and ideas (R7): nightly cap, group privacy, openers, cards, buttons and API."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from dom.config import SelfWorkConfig
from dom.models import StoredMessage, VISIBILITY_OWNER_PRIVATE
from dom.noticings import MAX_PER_NIGHT, contains_group_verbatim
from dom.telegram.handlers import handle_callback
from test_openers import always_try, record as opener_record
from test_traits import put
from test_usage import chat, completion, usage_rows
from test_web_api_memory_chats import JSON
from tghelpers import FINAL, FRIEND, GROUP, OWNER, NOW


async def add_msg(app, chat_id, text, msg_id, *, role="user", sender=None, delta=timedelta()):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id),
        msg_id=msg_id,
        role=role,
        sender_user_id=sender or (chat_id if chat_id > 0 else FRIEND),
        visibility_scope=VISIBILITY_OWNER_PRIVATE if chat_id == OWNER else f"chat:{chat_id}",
        text=text,
        created_at=(app.dispatch.now() - delta).isoformat(),
    ))


def make_callback(data: str, *, user_id: int = OWNER, chat_id: int = OWNER, chat_type: str = "private", msg_id: int = 5001) -> CallbackQuery:
    msg = Message(
        message_id=msg_id,
        date=NOW,
        chat=Chat(id=chat_id, type=chat_type, title=None if chat_type == "private" else "Book Club"),
        from_user=User(id=999, is_bot=True, first_name="Dom"),
        text="Noticing card",
    )
    return CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="Owner"),
        chat_instance="inst",
        message=msg,
        data=data,
    )


async def test_reflection_caps_noticings_at_three_updates_goals_and_tracks_spend(make_app):
    app = make_app()
    goal = await app.goals.create("Finish tax filing", category="money", target_date="2026-04-15")
    await add_msg(app, OWNER, "I gathered my receipts for taxes, and my passport expires next month.", msg_id=101)

    five_noticings = {
        "goal_updates": [
            {"goal_id": goal["id"], "note": "Gathered tax receipts"},
            {"goal_id": 9999, "note": "Unknown goal ignored"},
        ],
        "noticings": [
            {
                "kind": "deadline", "title": f"Item {i}", "body": f"Saw item {i} — want me to set a reminder?",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "owner_dm", "source_chat_id": str(OWNER), "source_msg_id": 101,
            }
            for i in range(1, 6)
        ],
    }

    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        assert not app.locks[OWNER].locked()
        return completion(json.dumps(five_noticings), {"cost": 0.004})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        created = await app.noticings.reflect()
        # Running a second time on the same waking day is a no-op (at most one call per night).
        second = await app.noticings.reflect()

    assert calls == 1
    assert len(created) == MAX_PER_NIGHT == 3
    assert second == []
    assert [n["title"] for n in created] == ["Item 1", "Item 2", "Item 3"]
    assert all(n["source_chat_id"] == str(OWNER) and n["source_msg_id"] == 101 for n in created)

    # Goal progress updated from reflection
    updated_goal = await app.goals.get(goal["id"])
    assert updated_goal["progress_note"] == "Gathered tax receipts"
    assert any(h["kind"] == "progress" and h["source"] == "reflection" for h in updated_goal["history"])

    # Spend attributed to reflection under owner
    assert usage_rows(app)[-1][0] == "reflection"
    assert usage_rows(app)[-1][3] == OWNER

    # Store-level cap also refuses more than 3 on the same day
    extra = await app.store.create_noticings(
        created[0]["day"],
        [{
            "kind": "idea", "title": "Fourth", "body": "Too many",
            "action_kind": "chat", "action_payload_json": "{}",
            "source_kind": "memory", "source_chat_id": None, "source_msg_id": None, "source_label": "Memory",
        }],
        app.dispatch.now().isoformat(),
        daily_cap=3,
    )
    assert extra == []


async def test_reflection_respects_spend_cap(make_app):
    app = make_app()
    await add_msg(app, OWNER, "Remind me to check flight prices", msg_id=101)
    # Spend exceeds default daily cap
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": app.config.daily_spend_cap_usd + 1.0})

    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        return completion(json.dumps({"goal_updates": [], "noticings": []}), {"cost": 0.01})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        created = await app.noticings.reflect()

    assert calls == 0
    assert created == []


async def test_group_verbatim_leak_and_r13_privacy_filter(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Weekend Crew")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Fran")
    await app.store.set_chat_auth_status(-2005, "pending_approval", title="Unapproved Group")

    group_secret = "Hey @newbie let's book the secret mountain cabin on Friday evening before prices jump"
    friend_secret = "FRIEND_DM_ONLY_SECRET_TEXT"
    unapproved_secret = "UNAPPROVED_GROUP_SECRET"

    await add_msg(app, GROUP, group_secret, msg_id=301, sender=FRIEND)
    await add_msg(app, FRIEND, friend_secret, msg_id=401, sender=FRIEND)
    await add_msg(app, -2005, unapproved_secret, msg_id=501, sender=FRIEND)
    await add_msg(app, OWNER, "Busy week ahead", msg_id=101, sender=OWNER)

    # People notes with R13 tags: friend DM line must NEVER reach reflection; group-tagged line is guarded
    put(
        app.home,
        f"memory/people/{FRIEND}.md",
        f"# Fran\n- Loves hiking {{any}}\n- Secret friend DM detail {{chat {FRIEND}}}\n"
        f"- Mentioned bringing specialty pour over coffee beans to the cabin {{chat {GROUP}}}\n",
    )

    # Model attempts multiple leak vectors + one valid clean one-line group paraphrase
    model_reply = {
        "goal_updates": [],
        "noticings": [
            # 1. Verbatim 5-word phrase from group message -> MUST BE DROPPED
            {
                "kind": "followup", "title": "Cabin plans",
                "body": "Saw the group talk about book the secret mountain cabin on Friday.",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "group_paraphrase", "source_chat_id": str(GROUP), "source_msg_id": 301,
            },
            # 2. Leak disguised as memory source -> MUST BE DROPPED
            {
                "kind": "help", "title": "Coffee gear",
                "body": "Someone mentioned bringing specialty pour over coffee beans to the cabin.",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "memory", "source_chat_id": None, "source_msg_id": None,
            },
            # 3. Group handle leaked -> MUST BE DROPPED
            {
                "kind": "followup", "title": "Check in with @newbie",
                "body": "Want to coordinate weekend plans with @newbie?",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "group_paraphrase", "source_chat_id": str(GROUP), "source_msg_id": 301,
            },
            # 4. Friend DM source pointer -> MUST BE DROPPED
            {
                "kind": "help", "title": "Friend chat",
                "body": "Want me to follow up with Fran?",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "group_paraphrase", "source_chat_id": str(FRIEND), "source_msg_id": 401,
            },
            # 5. Verbatim group text hidden in action_payload -> MUST BE DROPPED
            {
                "kind": "reminder", "title": "Weekend trip",
                "body": "Looks like a weekend getaway came up — want a reminder?",
                "action_kind": "reminder",
                "action_payload": {"when": "2026-01-02T09:00:00", "text": "book the secret mountain cabin on Friday"},
                "source_kind": "group_paraphrase", "source_chat_id": str(GROUP), "source_msg_id": 301,
            },
            # 6. Clean one-line paraphrase following R13 -> ALLOWED!
            {
                "kind": "followup", "title": "Weekend trip plans",
                "body": "Sounds like your weekend group is eyeing a getaway — want me to help look up options?",
                "action_kind": "chat", "action_payload": {},
                "source_kind": "group_paraphrase", "source_chat_id": str(GROUP), "source_msg_id": 301,
            },
        ],
    }
    app.script.contents = [json.dumps(model_reply)]
    created = await app.noticings.reflect()

    # Friend DM, unapproved group, and friend-DM-tagged note NEVER entered the reflection prompt
    prompt_blob = json.dumps(app.script.requests[-1])
    assert friend_secret not in prompt_blob
    assert unapproved_secret not in prompt_blob
    assert "Secret friend DM detail" not in prompt_blob
    assert "Loves hiking" in prompt_blob
    assert "{chat" not in prompt_blob and "{any}" not in prompt_blob

    # Only the clean one-line paraphrase survived code filtering
    assert len(created) == 1
    kept = created[0]
    assert kept["title"] == "Weekend trip plans"
    assert kept["source_kind"] == "group_paraphrase"
    assert kept["source_chat_id"] == str(GROUP)
    assert kept["source_msg_id"] == 301
    assert "\n" not in kept["body"]
    assert not contains_group_verbatim(kept["body"], [group_secret])


async def test_at_most_one_noticing_opener_per_day_as_telegram_card(make_app):
    app = make_app(contents=(FINAL, FINAL))
    openers, now = always_try(app, enabled=True, max_per_day=3)

    day = now().date().isoformat()
    created = await app.store.create_noticings(
        day,
        [
            {
                "kind": "deadline", "title": "Renew passport",
                "body": "You mentioned your passport is up soon — want me to set a reminder?",
                "action_kind": "chat", "action_payload_json": "{}",
                "source_kind": "owner_dm", "source_chat_id": str(OWNER), "source_msg_id": 10, "source_label": "Your DM",
            },
            {
                "kind": "idea", "title": "Compare coffee grinders",
                "body": "I could browse a few grinder reviews on my own computer tomorrow if you'd like?",
                "action_kind": "chat", "action_payload_json": "{}",
                "source_kind": "self_work", "source_chat_id": None, "source_msg_id": None, "source_label": "Own work",
            },
        ],
        now().isoformat(),
        daily_cap=3,
    )
    assert len(created) == 2

    # First opener today sends the newest pending noticing as a Telegram card with both buttons (0 model calls!)
    await openers.maybe(now())
    assert len(app.script.requests) == 0
    sends = app.bot.of("send", OWNER)
    assert len(sends) == 1
    _, _, text, kw = sends[0]
    assert "grinder" in text
    markup = kw["reply_markup"]
    buttons = [(b.text, b.callback_data) for b in markup.inline_keyboard[0]]
    assert buttons == [("Let's do it", f"notice:do:{created[1]['id']}"), ("Not now", f"notice:no:{created[1]['id']}")]

    # Owner replies, 3h pass on the SAME day -> second noticing MUST NOT go out as a second noticing opener!
    await opener_record(app, 6000, chat_id=OWNER, minutes_ago=0)
    await app.store.mark_chat_read(OWNER, 6000)
    now.advance(hours=4)
    await openers.maybe(now())
    # Ordinary opener ran instead (used the chat model, no noticing card buttons)
    assert len(app.script.requests) == 1
    assert app.bot.of("send", OWNER)[-1][3].get("reply_markup") is None
    assert (await app.noticings.get(created[0]["id"]))["sent_as_opener_at"] is None

    # Next local day -> the second pending noticing can go out as today's 1 noticing opener!
    await opener_record(app, 6010, chat_id=OWNER, minutes_ago=0)
    await app.store.mark_chat_read(OWNER, 6010)
    now.advance(days=1)
    await openers.maybe(now())
    last_send = app.bot.of("send", OWNER)[-1]
    assert "passport" in last_send[2]
    assert last_send[3]["reply_markup"].inline_keyboard[0][0].callback_data == f"notice:do:{created[0]['id']}"


async def test_not_now_button_dismisses_settles_card_and_feeds_preferences(make_app):
    app = make_app()
    now = app.dispatch.now()
    [item] = await app.store.create_noticings(
        now.date().isoformat(),
        [{
            "kind": "idea", "title": "Track daily water intake",
            "body": "Want me to ping you about water?",
            "action_kind": "chat", "action_payload_json": "{}",
            "source_kind": "memory", "source_chat_id": None, "source_msg_id": None, "source_label": "Memory",
        }],
        now.isoformat(),
    )

    # Non-owner tap is ignored
    await handle_callback(app, make_callback(f"notice:no:{item['id']}", user_id=FRIEND, chat_id=FRIEND))
    assert (await app.noticings.get(item["id"]))["status"] == "pending"

    # Owner taps "Not now"
    await handle_callback(app, make_callback(f"notice:no:{item['id']}", user_id=OWNER, chat_id=OWNER, msg_id=5555))
    settled = await app.noticings.get(item["id"])
    assert settled["status"] == "dismissed"
    assert app.bot.of("answer")[-1][2] == "Dismissed."
    assert app.bot.of("markup")[-1][3].inline_keyboard[0][0].text == "Not now"

    # Double tap answers "already handled"
    await handle_callback(app, make_callback(f"notice:no:{item['id']}", user_id=OWNER, chat_id=OWNER, msg_id=5555))
    assert app.bot.of("answer")[-1][2] == "already handled"

    # Next night's reflection sees the dismissed preference in <dismissed_preferences>
    app.dispatch.now.advance(days=1)
    await add_msg(app, OWNER, "Long day at work", msg_id=202)
    app.script.contents = [json.dumps({"goal_updates": [], "noticings": []})]
    await app.noticings.reflect()
    req_text = app.script.requests[-1]["messages"][1]["content"]
    assert "<dismissed_preferences>" in req_text
    assert "Track daily water intake" in req_text


async def test_lets_do_it_flows_reminder_self_work_cross_chat_and_chat(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={
        "desktop_mode": "local",
        "self_work": SelfWorkConfig(enabled=True, delay_hours=1, max_minutes=15, max_steps=15),
    })
    now = app.dispatch.now()
    goal = await app.goals.create("Renew ID", category="productivity")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Weekend Crew")

    items = await app.store.create_noticings(
        now.date().isoformat(),
        [
            # 1. Reminder action linked to a goal
            {
                "kind": "deadline", "title": "Renew ID reminder",
                "body": "Want me to remind you tomorrow morning?",
                "action_kind": "reminder",
                "action_payload_json": json.dumps({
                    "when": (now + timedelta(hours=6)).replace(tzinfo=None).isoformat(),
                    "text": "Renew ID card",
                    "importance": "normal",
                    "repeat": "",
                    "goal_id": goal["id"],
                }),
                "source_kind": "owner_dm", "source_chat_id": str(OWNER), "source_msg_id": 1, "source_label": "Your DM",
            },
            # 2. Idea -> Own work task
            {
                "kind": "idea", "title": "Research hiking trails",
                "body": "I could check trail conditions on my own computer tomorrow?",
                "action_kind": "self_work",
                "action_payload_json": json.dumps({
                    "goal": "Check local trail status",
                    "why": "Owner wants to hike this weekend",
                    "tools": ["browse", "read_page"],
                    "sites": ["example.org"],
                    "minutes": 10,
                    "steps": 12,
                }),
                "source_kind": "self_work", "source_chat_id": None, "source_msg_id": None, "source_label": "Own work",
            },
            # 3. Cross-chat action -> MUST create an approval card, NEVER send to GROUP directly!
            {
                "kind": "followup", "title": "Reply to Weekend Crew",
                "body": "Want me to draft a quick RSVP to Weekend Crew?",
                "action_kind": "cross_chat",
                "action_payload_json": json.dumps({
                    "chat": "Weekend Crew",
                    "text": "Count us in for Saturday!",
                    "reason": "Owner wanted to RSVP",
                }),
                "source_kind": "group_paraphrase", "source_chat_id": str(GROUP), "source_msg_id": 2, "source_label": "Weekend Crew",
            },
        ],
        now.isoformat(),
        daily_cap=3,
    )

    # 1. Tap "Let's do it" on reminder noticing
    await handle_callback(app, make_callback(f"notice:do:{items[0]['id']}", msg_id=5101))
    rems = await app.dispatch.reminders.active()
    assert len(rems) == 1 and rems[0]["text"] == "Renew ID card"
    assert [r["id"] for r in (await app.goals.get(goal["id"]))["reminders"]] == [rems[0]["id"]]
    assert app.bot.of("markup")[-1][3].inline_keyboard[0][0].text == "✅ Let's do it"

    # 2. Tap "Let's do it" on Own work idea noticing
    await handle_callback(app, make_callback(f"notice:do:{items[1]['id']}", msg_id=5102))
    tasks = await app.store.self_tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "planned"
    assert tasks[0]["goal"] == "Check local trail status"

    # 3. Tap "Let's do it" on cross_chat noticing -> approval card created, ZERO direct messages sent to GROUP!
    await handle_callback(app, make_callback(f"notice:do:{items[2]['id']}", msg_id=5103))
    assert app.bot.of("send", GROUP) == []
    pending_props = [p for p in await app.proposals.pending() if p["kind"] == "cross_chat"]
    assert len(pending_props) == 1

    # 4. Chat action noticing spawns follow-up turn in owner DM
    app.dispatch.now.advance(days=1)
    [chat_item] = await app.store.create_noticings(
        app.dispatch.now().date().isoformat(),
        [{
            "kind": "help", "title": "Draft packing list",
            "body": "Want me to put together a quick packing list?",
            "action_kind": "chat", "action_payload_json": "{}",
            "source_kind": "owner_dm", "source_chat_id": str(OWNER), "source_msg_id": 3, "source_label": "Your DM",
        }],
        app.dispatch.now().isoformat(),
    )
    app.script.contents = [json.dumps({"no_response": False, "bubbles": [{"text": "Here is your packing list!"}]})]
    await handle_callback(app, make_callback(f"notice:do:{chat_item['id']}", msg_id=5104))
    # Wait for spawned background follow-up task to finish
    if app.dispatch._tasks:
        await asyncio.gather(*list(app.dispatch._tasks), return_exceptions=True)
    assert any("Here is your packing list!" in call[2] for call in app.bot.of("send", OWNER))


async def test_noticings_owner_only_api_and_home_for_you(make_app, browser):
    app = make_app()
    now = app.dispatch.now()
    [item] = await app.store.create_noticings(
        now.date().isoformat(),
        [{
            "kind": "idea", "title": "Weekend hike research",
            "body": "Want me to look up trails for Saturday?",
            "action_kind": "chat", "action_payload_json": "{}",
            "source_kind": "self_work", "source_chat_id": None, "source_msg_id": None, "source_label": "Own work",
        }],
        now.isoformat(),
    )
    await app.noticings.mark_opener_sent(item["id"], now, 7777)

    async with browser(app) as client:
        assert (await client.get("/api/noticings", headers={"Authorization": ""})).status_code == 403
        assert (await client.post("/api/noticings/decide", json={"id": item["id"], "decision": "dismiss"})).status_code == 403
        assert (await client.post("/api/noticings/decide", json={"id": item["id"], "decision": "dismiss"}, headers={"Origin": "https://evil.example.org"})).status_code == 403

        # Home exposes "for_you" pending items
        home_data = (await client.get("/api/home")).json()
        assert home_data["for_you"]["count"] == 1
        assert home_data["for_you"]["items"][0]["title"] == "Weekend hike research"

        # List with filter
        listed = (await client.get("/api/noticings?status=pending&kind=idea")).json()
        assert listed["pending_count"] == 1
        assert listed["noticings"][0]["id"] == item["id"]

        # Decide dismiss via API -> settles Telegram card too!
        decided = await client.post("/api/noticings/decide", json={"id": item["id"], "decision": "dismiss"}, headers=JSON)
        assert decided.status_code == 200
        body = decided.json()
        assert body["ok"] is True and body["noticing"]["status"] == "dismissed" and body["pending_count"] == 0
        assert any(call[2] == 7777 for call in app.bot.of("markup"))

        # Delete with stale check
        assert (await client.post("/api/noticings/delete", json={"id": item["id"], "expected": "wrong"}, headers=JSON)).status_code == 409
        deleted = await client.post("/api/noticings/delete", json={"id": item["id"], "expected": item["created_at"]}, headers=JSON)
        assert deleted.status_code == 200 and deleted.json()["noticings"] == []


async def test_reflection_handles_malformed_json_gracefully(make_app, caplog):
    app = make_app(contents=("not valid json at all",))
    await add_msg(app, OWNER, "Mentioned a dentist appointment next Tuesday", msg_id=101)
    with caplog.at_level("WARNING"):
        created = await app.noticings.reflect()
    assert created == []
    assert "malformed" in caplog.text

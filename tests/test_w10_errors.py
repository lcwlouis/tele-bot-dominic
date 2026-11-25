"""Tests for v0.8.1 W10 error-handling leftovers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageText, SendChatAction, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from dom.actions import TELEGRAM_TEXT_LIMIT, Actions, Bubble, utf16_len
from dom.dispatch import (
    FRIEND_NO_ANSWER_LINE,
    GROUP_NO_ANSWER_COOLDOWN,
    NO_ANSWER_NOTICE,
    SPEND_CAP_LINE,
    Dispatcher,
)
from dom.memory_pass import MemoryPass
from dom.models import StoredMessage
from dom.presence import Presence, parse_habits
from dom.reminders import Reminders
from dom.telegram import executor, handlers
from dom.telegram.format import format_text
from test_dispatch import FakeJev
from test_gifs import StubGifs, animation_bot
from test_owner_events import events
from test_reminders import add as add_reminder
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, NOW, OWNER, Clock, FixedRandom, drain_dispatch, no_sleep, tg, turn_requests


def _retry_after(seconds: int = 2) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=GROUP, text="x"),
        message=f"Flood control exceeded. Retry in {seconds} seconds",
        retry_after=seconds,
    )


# ---- 1. Telegram flood control & unguarded send_chat_action -----------------------------------


async def test_send_message_waits_retry_after_and_respects_budget_and_max_retries(make_app):
    app = make_app()
    sleeps: list[float] = []
    attempts = 0
    real_send = app.bot.send_message

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    async def flaky_send(chat_id, text, **kw):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _retry_after(attempts + 1)
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = flaky_send
    res = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(bubbles=[Bubble(text="hello")]), sleep=fake_sleep,
    )
    assert len(res) == 1 and res.flood_limited is False
    assert attempts == 3
    assert [s for s in sleeps if s >= 2] == [2.0, 3.0]

    # Without a budget, retry_telegram raises TelegramRetryAfter after MAX_FLOOD_RETRIES.
    always_attempts = 0

    async def always_flood():
        nonlocal always_attempts
        always_attempts += 1
        raise _retry_after(1)

    with pytest.raises(TelegramRetryAfter):
        await executor.retry_telegram(always_flood, sleep=no_sleep)
    assert always_attempts == executor.MAX_FLOOD_RETRIES + 1

    # Inside perform, exceeding the flood budget returns flood_limited=True without sleeping past budget.
    big_sleeps: list[float] = []

    async def big_flood(chat_id, text, **kw):
        raise _retry_after(30)

    app.bot.send_message = big_flood
    res2 = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(bubbles=[Bubble(text="hello")]),
        sleep=lambda s: _record_sleep(big_sleeps, s),
    )
    assert res2 == [] and res2.flood_limited is True
    assert all(s < 30 for s in big_sleeps)


async def _record_sleep(log_list: list[float], seconds: float) -> None:
    log_list.append(seconds)


async def test_type_for_skips_flooded_or_failed_send_chat_action(make_app):
    app = make_app()
    sleeps: list[float] = []
    action_calls = 0

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    # Flooded typing indicator is skipped immediately, never retried and never sleeps retry_after.
    async def flooded_action(chat_id, action, **kw):
        nonlocal action_calls
        action_calls += 1
        raise TelegramRetryAfter(method=SendChatAction(chat_id=chat_id, action=action), message="flood", retry_after=9)

    app.bot.send_chat_action = flooded_action
    ids = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(bubbles=[Bubble(text="hi")]), sleep=fake_sleep,
    )
    assert len(ids) == 1 and action_calls == 1 and 9.0 not in sleeps

    # Hard failure in send_chat_action must never abort the reply.
    async def broken_action(chat_id, action, **kw):
        raise RuntimeError("chat action failed")

    app.bot.send_chat_action = broken_action
    ids2 = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(bubbles=[Bubble(text="still delivered")]), sleep=no_sleep,
    )
    assert len(ids2) == 1
    assert app.bot.of("send", GROUP)[-1][2] == "still delivered"


async def test_flood_budget_releases_lock_and_leaves_newer_messages_unread(make_app):
    # Turn 1 tries to send 3 bubbles; bubble 1 succeeds, bubble 2 hits flood waits exceeding the 10s budget.
    # While bubble 2 is sleeping on the first flood wait, message 2 arrives in the same chat.
    # The reply stops after bubble 1, releases the lock, marks only message 1 read, and leaves message 2 unread
    # so it gets answered once the flood passes.
    payload1 = json.dumps({"bubbles": [{"text": "one"}, {"text": "two"}, {"text": "three"}]})
    payload2 = json.dumps({"bubbles": [{"text": "got your second message"}]})
    app = make_app(contents=(payload1, payload2))
    await online(app, GROUP)

    real_send = app.bot.send_message
    flood_sleeps: list[float] = []
    injected_msg2 = False
    bubble2_attempts = 0
    flood_active = True
    allow_turn2 = asyncio.Event()
    turn2_debounce_started = False

    async def debounce_sleep(s: float) -> None:
        nonlocal turn2_debounce_started
        if injected_msg2 and not turn2_debounce_started:
            turn2_debounce_started = True
            await allow_turn2.wait()

    async def fake_sleep(s: float) -> None:
        nonlocal injected_msg2
        if s >= 5.0:
            flood_sleeps.append(s)
            if not injected_msg2:
                injected_msg2 = True
                # Newer message arrives while turn 1 is holding the chat lock in a flood wait.
                await handlers.handle_message(app, tg(GROUP, FRIEND, text="follow up", msg_id=2))

    async def flaky_send(chat_id, text, **kw):
        nonlocal bubble2_attempts
        if flood_active and text == "two":
            bubble2_attempts += 1
            raise _retry_after(6)  # first wait 6s (4s left), second needs 6s > 4s -> budget exhausted!
        return await real_send(chat_id, text, **kw)

    app.sleep = fake_sleep
    app.dispatch.sleep = debounce_sleep
    app.bot.send_message = flaky_send

    await handlers.handle_message(app, tg(GROUP, FRIEND, text="first", msg_id=1))
    async def turn_one_finished() -> None:
        while not (bubble2_attempts == 2 and GROUP not in app.dispatch._active):
            await asyncio.sleep(0)

    await asyncio.wait_for(turn_one_finished(), timeout=5)

    # Only bubble 1 was sent, total flood wait stayed within 10s (slept 6s once, refused second 6s wait).
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["one"]
    assert flood_sleeps == [6.0]
    assert bubble2_attempts == 2
    assert not app.locks[GROUP].locked()
    # Message 1 was covered and marked read, while message 2 remains unread!
    unread_count, newest_unread = await app.store.get_unread(GROUP)
    assert unread_count == 1 and newest_unread is not None and newest_unread.msg_id == 2
    # Turn log and owner event recorded the flood limit.
    turns = await app.store.recent_turns(str(GROUP))
    assert any("flood limited" in t["outcome"].lower() for t in turns)
    assert any("flood limit" in e["text"].lower() for e in await events(app))

    # Once the flood clears and turn 2 runs, message 2 is answered!
    flood_active = False
    allow_turn2.set()
    await drain_dispatch(app.dispatch)
    assert "got your second message" in [c[2] for c in app.bot.of("send", GROUP)]
    assert (await app.store.get_unread(GROUP))[0] == 0


async def test_edit_message_retries_on_flood_control(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())
    sleeps: list[float] = []
    edit_calls = 0
    real_edit = app.bot.edit_message_text

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    async def flood_edit(text, chat_id=None, message_id=None, **kw):
        nonlocal edit_calls
        edit_calls += 1
        if edit_calls == 1:
            raise TelegramRetryAfter(
                method=EditMessageText(text=text, chat_id=chat_id, message_id=message_id),
                message="flood",
                retry_after=4,
            )
        return await real_edit(text, chat_id=chat_id, message_id=message_id, **kw)

    app.sleep = fake_sleep
    app.bot.edit_message_text = flood_edit
    prompt = tg(chat_id=OWNER, sender=999, text="prompt", msg_id=60)
    await handlers.handle_callback(app, CallbackQuery(
        id="cb1", from_user=User(id=OWNER, is_bot=False, first_name="O"),
        chat_instance="ci", data=f"approve:{GROUP}", message=prompt,
    ))
    assert edit_calls == 2 and sleeps == [4.0]


# ---- 2. Over-long bubble splitting in UTF-16 code units ---------------------------------------


def test_hard_split_keeps_zwj_emoji_and_grapheme_clusters_whole():
    family = "👨‍👩‍👧‍👦"  # 11 UTF-16 code units with ZWJs
    technologist = "👨‍💻"  # 5 UTF-16 code units with ZWJ
    heart_vs = "❤️"  # base + U+FE0F variation selector
    wave_skin = "👋🏽"  # base + skin-tone modifier
    accented = "e\u0301"  # base + combining acute accent

    for cluster in (family, technologist, heart_vs, wave_skin, accented):
        # Craft a limit that lands strictly inside the cluster at the end of a prefix
        cluster_units = utf16_len(cluster)
        for offset in range(1, cluster_units):
            limit = 20 + offset
            text = ("x" * 20) + cluster + ("y" * 20)
            parts = [part for part, _ in format_text(text, limit=limit)]
            assert "".join(parts) == text
            assert all(utf16_len(p) <= limit for p in parts)
            assert any(cluster in p for p in parts), f"cluster {cluster!r} was torn at offset {offset}: {parts!r}"

    # Repeated ZWJ emoji stream at Telegram limit never tears a sequence
    stream = family * 450  # 4950 UTF-16 code units
    parts = [part for part, _ in format_text(stream)]
    assert len(parts) == 2
    assert "".join(parts) == stream
    assert all(utf16_len(p) <= TELEGRAM_TEXT_LIMIT for p in parts)
    assert all(p.count(family) * len(family) == len(p) for p in parts)


async def test_overlong_bubble_splits_without_triggering_model_retry(make_app):
    long_text = ("First paragraph. " * 150).strip() + "\n\n" + ("Second paragraph. " * 150).strip()
    assert utf16_len(long_text) > TELEGRAM_TEXT_LIMIT
    payload = json.dumps({"bubbles": [{"text": long_text, "reply_to": 1, "delete_after": 10}]})
    app = make_app(contents=(payload,))
    await online(app, GROUP)
    await deliver(app, tg(GROUP, FRIEND, "tell me everything"))
    assert len(turn_requests(app)) == 1
    sent = app.bot.of("send", GROUP)
    assert len(sent) == 2
    assert all(utf16_len(call[2]) <= TELEGRAM_TEXT_LIMIT for call in sent)
    assert sent[0][3]["reply_parameters"].message_id == 1
    assert sent[1][3].get("reply_parameters") is None


# ---- 3. Forum topics survive restart and catch-up ---------------------------------------------


async def test_forum_topic_reply_uses_stored_thread_id_after_restart_and_catchup(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved")
    topic_msg = tg(GROUP, FRIEND, text="topic question", msg_id=10).model_copy(
        update={"message_thread_id": 77, "is_topic_message": True},
    )
    await handlers.handle_message(app, topic_msg)
    assert app.bot.of("send", GROUP) == []

    clock = Clock(app.dispatch.now() + timedelta(hours=2))
    presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence, None, no_sleep, clock)
    await app.dispatch.tick()

    assert app.bot.of("send", GROUP)[0][3]["message_thread_id"] == 77
    assert app.bot.of("action", GROUP)[0][3]["message_thread_id"] == 77
    stored = await app.store.get_recent_chat_window(GROUP)
    assert [m.thread_id for m in stored] == [77, 77]


# ---- 4. Supergroup migration (simplified, alias-based, conflict-safe) -------------------------


async def test_supergroup_migration_carries_settings_keeps_history_and_searches_through_alias(make_app):
    old_id, new_id = -444, -10444
    app = make_app()
    await app.store.set_chat_auth_status(old_id, "approved", title="Weekend Plans")
    await app.store.set_chat_preset(old_id, "classic")
    await app.store.set_chat_traits(old_id, {"sarcasm": 0.8})
    await online(app, old_id)
    put(app.home, f"memory/chats/{old_id}.md", "- loves ramen\n")
    await deliver(app, tg(old_id, FRIEND, text="remember the ramen spot", msg_id=50))
    app.bot.calls.clear()

    migrate_to = Message(
        message_id=51, date=NOW,
        chat=Chat(id=old_id, type="group", title="Weekend Plans"),
        from_user=User(id=OWNER, is_bot=False, first_name="Owner"),
        migrate_to_chat_id=new_id,
    )
    await handlers.handle_message(app, migrate_to)
    # Companion migrate_from_chat_id must be idempotent.
    migrate_from = Message(
        message_id=1, date=NOW,
        chat=Chat(id=new_id, type="supergroup", title="Weekend Plans"),
        from_user=User(id=OWNER, is_bot=False, first_name="Owner"),
        migrate_from_chat_id=old_id,
    )
    await handlers.handle_message(app, migrate_from)

    # Approval, preset, traits, and chat note carried to new_id.
    assert (await app.store.get_chat_auth_status(new_id))["authorization_status"] == "approved"
    assert await app.store.get_chat_personality(new_id) == ("classic", json.dumps({"sarcasm": 0.8}))
    assert "loves ramen" in app.home.read(f"memory/chats/{new_id}.md")
    assert "loves ramen" in app.home.read(f"memory/chats/{old_id}.md")

    # New chat starts its own window with one upgrade event and fresh memory cursor; old history stays on old_id
    # and is searchable from new_id through the alias.
    new_window = await app.store.get_recent_chat_window(new_id)
    assert len(new_window) == 1 and new_window[0].role == "event" and "upgraded to a supergroup" in new_window[0].text
    assert await app.store.get_memory_cursor(new_id) == 0
    old_window = await app.store.get_recent_chat_window(old_id)
    assert any("remember the ramen spot" in m.text for m in old_window)
    search_hits = await app.store.search_chat_history("ramen", str(new_id))
    assert len(search_hits) == 1 and search_hits[0]["chat_id"] == str(old_id)

    # Alias resolves for owner commands; old_id is marked 'left' so approved lists show only new_id.
    assert await app.store.resolve_chat_id(old_id) == str(new_id)
    assert await app.dispatch.override(old_id, "/sleep") is True
    assert (await app.dispatch.presence.state(new_id, parse_habits("")))[0] == "asleep"
    assert await app.dispatch.override(old_id, "/wake") is True
    assert (await app.dispatch.presence.state(new_id, parse_habits("")))[0] == "online"
    approved_ids = {c["chat_id"] for c in await app.store.list_chats("approved")}
    assert str(new_id) in approved_ids and str(old_id) not in approved_ids

    # Exactly one owner event recorded.
    migration_events = [e["text"] for e in await events(app) if "migrated to supergroup" in e["text"]]
    assert len(migration_events) == 1
    assert "Weekend Plans" in migration_events[0] and str(new_id) in migration_events[0]

    # New messages in the new supergroup are unread and answered.
    await deliver(app, tg(new_id, FRIEND, text="we upgraded!", msg_id=2, chat_type="supergroup"))
    assert [call[2] for call in app.bot.of("send", new_id)] == ["hey"]


async def test_old_group_edit_does_not_touch_new_group_message(make_app):
    old_id, new_id = -666, -10666
    app = make_app()
    await online(app, old_id)
    await deliver(app, tg(old_id, FRIEND, text="old message fifty", msg_id=50))

    migrate_to = Message(
        message_id=51, date=NOW,
        chat=Chat(id=old_id, type="group", title="Book Club"),
        from_user=User(id=OWNER, is_bot=False, first_name="Owner"),
        migrate_to_chat_id=new_id,
    )
    await handlers.handle_message(app, migrate_to)

    # New supergroup receives its own message with the same msg_id=50.
    await deliver(app, tg(new_id, FRIEND, text="new supergroup fifty", msg_id=50, chat_type="supergroup"))

    # A late edit arrives for old_id's msg_id=50.
    await handlers.handle_edited_message(app, tg(old_id, FRIEND, text="old message fifty EDITED", msg_id=50))

    old_msgs = {m.msg_id: m.text for m in await app.store.get_recent_chat_window(old_id) if m.msg_id is not None}
    new_msgs = {m.msg_id: m.text for m in await app.store.get_recent_chat_window(new_id) if m.msg_id is not None}
    assert old_msgs[50] == "old message fifty EDITED"
    assert new_msgs[50] == "new supergroup fifty"


async def test_supergroup_migration_refuses_destination_conflict(make_app):
    # Case 1: destination already exists and is approved.
    old_id, new_id = -777, -10777
    app = make_app()
    await app.store.set_chat_auth_status(old_id, "approved", title="Old Crew")
    await app.store.set_chat_preset(old_id, "classic")
    put(app.home, f"memory/chats/{old_id}.md", "- old note\n")
    await app.store.set_chat_auth_status(new_id, "approved", title="Existing Super")
    await app.store.set_chat_preset(new_id, "warm")
    put(app.home, f"memory/chats/{new_id}.md", "- existing note\n")

    outcome = await app.dispatch.migrate_chat(old_id, new_id, title="Old Crew")
    assert outcome == "conflict"
    assert await app.store.resolve_chat_id(old_id) == str(old_id)
    assert (await app.store.get_chat_auth_status(old_id))["authorization_status"] == "approved"
    assert (await app.store.get_chat_personality(new_id))[0] == "warm"
    assert app.home.read(f"memory/chats/{new_id}.md") == "- existing note\n"
    assert any("destination chat already exists" in e["text"] for e in await events(app))

    # Case 2: destination already exists as pending_approval -> also refused without merging.
    old2, new2 = -778, -10778
    await app.store.set_chat_auth_status(old2, "approved", title="Old Pending Test")
    await app.store.set_chat_auth_status(new2, "pending_approval", title="Pending Dest")
    outcome2 = await app.dispatch.migrate_chat(old2, new2, title="Old Pending Test")
    assert outcome2 == "conflict"
    assert await app.store.resolve_chat_id(old2) == str(old2)
    assert (await app.store.get_chat_auth_status(new2))["authorization_status"] == "pending_approval"
    assert (await app.store.get_chat_auth_status(old2))["authorization_status"] == "approved"


async def test_inflight_memory_pass_on_old_id_does_not_write_cursor_to_new_id(make_app):
    old_id, new_id = -888, -10888
    app = make_app()
    await online(app, old_id)
    await deliver(app, tg(old_id, FRIEND, text="we should hike on Sunday", msg_id=10))

    started = asyncio.Event()
    release = asyncio.Event()
    real_complete = app.client.complete

    async def slow_complete(messages, tools, **kw):
        started.set()
        await release.wait()
        return await real_complete(messages, tools, **kw)

    app.client.complete = slow_complete
    # Start a memory pass on old_id and pause while it is in flight.
    mem_task = asyncio.create_task(MemoryPass(app).run(old_id))
    await started.wait()

    # Migrate old_id -> new_id while the memory pass is in flight.
    assert await app.dispatch.migrate_chat(old_id, new_id, title="Hikers") == "migrated"
    release.set()
    await mem_task

    # In-flight memory pass never writes a cursor to new_id.
    assert await app.store.get_memory_cursor(new_id) == 0

    # Even a late cursor write on old_id (e.g., if a pass had already passed its approval check) stays on old_id only.
    await app.store.advance_memory_cursor(old_id, 10)
    assert await app.store.get_memory_cursor(old_id) == 10
    assert await app.store.get_memory_cursor(new_id) == 0


async def test_telegram_migrate_to_chat_error_uses_shared_helper(make_app):
    old_id, new_id = -555, -10555
    app = make_app()
    await online(app, old_id)

    async def fail_migrate(chat_id, text, **kw):
        raise TelegramMigrateToChat(
            method=SendMessage(chat_id=chat_id, text=text),
            message="Bad Request: group chat was upgraded to a supergroup chat",
            migrate_to_chat_id=new_id,
        )

    app.bot.send_message = fail_migrate
    await deliver(app, tg(old_id, FRIEND, text="hello", msg_id=1))
    assert await app.store.resolve_chat_id(old_id) == str(new_id)
    assert await app.store.is_chat_approved(new_id) is True
    assert not app.locks[old_id].locked() and not app.locks[new_id].locked()
    assert any("migrated to supergroup" in e["text"] for e in await events(app))


# ---- 5. NoFinalAnswer in friends' and groups' chats + cooldown + owner event ------------------


async def test_no_final_answer_group_cooldown_and_unaddressed_silence(make_app):
    app = make_app(contents=("p", "p") * 6)
    await online(app, GROUP)

    # First directly addressed failure -> sends short in-character line + records owner event.
    await deliver(app, tg(GROUP, FRIEND, text="@dombot what do you think?", msg_id=10))
    sent = app.bot.of("send", GROUP)
    assert len(sent) == 1 and sent[0][2] == FRIEND_NO_ANSWER_LINE
    assert sent[0][3]["reply_parameters"].message_id == 10
    assert sent[0][2] != NO_ANSWER_NOTICE

    # Second directly addressed failure 5 minutes later -> suppressed by 30-min cooldown, owner event still recorded.
    app.bot.calls.clear()
    app.dispatch.now.advance(minutes=5)
    await deliver(app, tg(GROUP, FRIEND, text="@dombot hello?", msg_id=11))
    assert app.bot.of("send", GROUP) == []

    # Unaddressed group chatter -> stays quiet, still records owner event.
    await deliver(app, tg(GROUP, FRIEND, text="anyone want coffee?", msg_id=12))
    assert app.bot.of("send", GROUP) == []

    # After the cooldown passes, directly addressed failure sends the line again.
    app.dispatch.now.advance(minutes=int(GROUP_NO_ANSWER_COOLDOWN.total_seconds() // 60) + 1)
    await online(app, GROUP)
    await deliver(app, tg(GROUP, FRIEND, text="@dombot back yet?", msg_id=13))
    assert [c[2] for c in app.bot.of("send", GROUP)] == [FRIEND_NO_ANSWER_LINE]

    ev = [e["text"] for e in await events(app) if "no usable final answer" in e["text"]]
    assert len(ev) == 4


async def test_no_final_answer_owner_dm_records_owner_event(make_app):
    app = make_app(contents=("prose", "more prose"))
    await online(app, OWNER)
    await deliver(app, tg(OWNER, OWNER, text="hey dom", msg_id=1))
    assert [c[2] for c in app.bot.of("send", OWNER)] == [NO_ANSWER_NOTICE]
    assert any("no usable final answer" in e["text"] for e in await events(app))


# ---- 6. Spend-cap line goes only to friend DMs, never groups ----------------------------------


async def test_friend_dm_under_spend_cap_gets_one_line_per_day_and_groups_get_none(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Asia/Singapore"})
    clock = app.dispatch.now
    clock.at = datetime(2026, 1, 1, 15, 59, tzinfo=timezone.utc)  # 23:59 local
    await online(app, FRIEND)
    await online(app, GROUP)
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1.5})

    # Group gets NO spend-cap line.
    await deliver(app, tg(GROUP, FRIEND, text="hey group", msg_id=1))
    assert app.bot.of("send", GROUP) == []

    # Friend DM gets exactly one line for this cap period, no model calls, no numbers or mechanism words.
    await deliver(app, tg(FRIEND, FRIEND, text="hey you around?", msg_id=1))
    await deliver(app, tg(FRIEND, FRIEND, text="hello??", msg_id=2))
    assert app.script.requests == []
    sent = [c[2] for c in app.bot.of("send", FRIEND)]
    assert sent == [SPEND_CAP_LINE]
    assert not re.search(r"\d|\$|cap|budget|limit|spend|quota|cost|api|token|midnight|bot", sent[0], re.IGNORECASE)
    assert (await app.store.get_unread(FRIEND))[0] == 2

    # Next local day: cap resets; hitting cap again sends one line for the new period.
    clock.advance(minutes=1)
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1.5})
    await deliver(app, tg(FRIEND, FRIEND, text="morning!", msg_id=3))
    assert [c[2] for c in app.bot.of("send", FRIEND)] == [SPEND_CAP_LINE, SPEND_CAP_LINE]


async def test_asleep_friend_under_spend_cap_gets_no_line(make_app):
    app = make_app()
    app.dispatch.now.at = app.dispatch.now.at.replace(hour=2)  # asleep
    await app.store.set_chat_auth_status(FRIEND, "approved")
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1.5})
    await deliver(app, tg(FRIEND, FRIEND, text="late text", msg_id=1))
    assert app.bot.of("send", FRIEND) == []


# ---- 7. Private action ineligible reason ------------------------------------------------------


async def test_executor_private_ineligible_uses_supplied_reason(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Study Group")
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(private=True, bubbles=[Bubble(text="secret")]),
        ephemeral_recipient=None, ephemeral_ineligible_reason="ephemeral replies are off", sleep=no_sleep,
    )
    assert sent == []
    assert [e["text"] for e in await events(app)] == [
        "Held back a private reply in Study Group: ephemeral replies are off",
    ]


# ---- 8. react() result handled in executor.perform and dispatch -------------------------------


async def test_failed_reaction_is_logged_and_not_treated_as_sent(make_app, caplog):
    app = make_app(contents=(json.dumps({"react": "🔥"}),))
    await online(app, GROUP)

    async def refuse_react(chat_id, message_id, reaction=None, **kw):
        raise TelegramBadRequest(method=None, message="Bad Request: REACTION_INVALID")

    app.bot.set_message_reaction = refuse_react
    with caplog.at_level(logging.WARNING):
        await deliver(app, tg(GROUP, FRIEND, text="nice", msg_id=5))

    assert "not sent" in caplog.text or "rejected" in caplog.text
    assert GROUP in app.dispatch._reactions_off
    [turn] = await app.store.recent_turns(str(GROUP))
    assert turn["outcome"] == "Reaction failed"
    assert not turn["outcome"].startswith("Reacted")


async def test_failed_triage_reaction_is_not_logged_as_sent(make_app):
    from dom import turn_log

    class LoggingJev(FakeJev):
        async def decide(self, state, questions):
            turn_log.step("triage")
            return await super().decide(state, questions)

    app = make_app()
    app.dispatch.jev = LoggingJev("react", confidence=0.95)
    await online(app, GROUP)

    async def fail_react(chat_id, message_id, reaction=None, **kw):
        raise TelegramNetworkError(method=None, message="down")

    app.bot.set_message_reaction = fail_react
    await deliver(app, tg(GROUP, FRIEND, text="cool", msg_id=6))
    [turn] = await app.store.recent_turns(str(GROUP))
    assert turn["outcome"] == "Reaction failed"


async def test_flood_limited_arrival_reaction_leaves_the_message_unread(make_app):
    app = make_app()
    app.dispatch.jev = FakeJev("react", confidence=0.95)
    await online(app, GROUP)

    async def limited(chat_id, message_id, reaction=None, **kw):
        raise _retry_after(60)  # longer than the reply's flood budget

    app.bot.set_message_reaction = limited
    await deliver(app, tg(GROUP, FRIEND, text="cool", msg_id=6))
    [turn] = await app.store.recent_turns(str(GROUP))
    assert turn["outcome"] == "Flood limited"
    assert (await app.store.get_unread(GROUP))[0] == 1


# ---- 9. Text fallback for failed sticker or GIF -----------------------------------------------


async def test_a_numbered_or_wordy_sticker_never_reaches_the_chat_as_text(make_app):
    app = make_app()
    # an unknown number for an emoji that is not loaded: the bare emoji, never "📚9"
    await executor.perform(app.bot, app.store, GROUP, 1, Actions(sticker="📚9"), stickers={"🍵": ["file-c"]}, sleep=no_sleep)
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["📚"]
    app.bot.calls.clear()
    # words after the emoji: nothing is sent at all
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(sticker="📚2 Owl on a book stack"), stickers={}, sleep=no_sleep,
    )
    assert len(sent) == 0 and app.bot.of("send", GROUP) == []


async def test_failed_sticker_only_sends_emoji_fallback(make_app):
    app = make_app()
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(sticker="🍵"), stickers={"📚": ["file-a"]}, sleep=no_sleep,
    )
    assert len(sent) == 1 and sent.media_fallback is True
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["🍵"]

    app.bot.calls.clear()

    async def fail_sticker(chat_id, sticker, **kw):
        raise TelegramNetworkError(method=None, message="down")

    app.bot.send_sticker = fail_sticker
    sent2 = await executor.perform(
        app.bot, app.store, GROUP, 2, Actions(sticker="📚"), stickers={"📚": ["file-a"]}, sleep=no_sleep,
    )
    assert len(sent2) == 1 and sent2.media_fallback is True
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["📚"]
    notes = [row["text"] for row in await events(app, GROUP)]
    assert len(notes) == 1 and "sticker was not sent" in notes[0]


async def test_failed_gif_only_sends_text_standin(make_app):
    app = make_app()
    animation_bot(app, error=TelegramNetworkError(method=None, message="down"))
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="happy dance"), gifs=StubGifs(), sleep=no_sleep,
    )
    assert len(sent) == 1 and sent.media_fallback is True
    call, = app.bot.of("send", GROUP)
    assert call[2] == "happy dance" and call[3]["entities"][0].type == "italic"

    app.bot.calls.clear()
    sent2 = await executor.perform(
        app.bot, app.store, GROUP, 2, Actions(gif="shrug"), gifs=StubGifs(url=None), sleep=no_sleep,
    )
    assert len(sent2) == 1 and sent2.media_fallback is True
    call, = app.bot.of("send", GROUP)
    assert call[2] == "shrug" and call[3]["entities"][0].type == "italic"
    notes = [row["text"] for row in await events(app, GROUP)]
    assert len(notes) == 1 and "gif was not sent" in notes[0].lower()


async def test_telegram_reply_failure_is_noted_in_that_chat_and_rate_limited(make_app):
    app = make_app()

    async def fail(chat_id, text, **kwargs):
        raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="network down")

    app.bot.send_message = fail
    for msg_id in (1, 2):
        with pytest.raises(TelegramNetworkError):
            await executor.perform(
                app.bot, app.store, GROUP, msg_id, Actions(bubbles=[Bubble(text="hello")]), sleep=no_sleep,
            )
    notes = [row["text"] for row in await events(app, GROUP)]
    assert len(notes) == 1 and "reply was not sent" in notes[0]
    assert await events(app, OWNER) == []


async def test_failed_sticker_or_gif_with_bubble_sends_bubble_without_extra_standin(make_app):
    app = make_app()
    animation_bot(app, error=TelegramNetworkError(method=None, message="down"))
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1,
        Actions(bubbles=[Bubble(text="See you there!")], sticker="🍵", gif="wave"),
        stickers={}, gifs=StubGifs(), sleep=no_sleep,
    )
    assert len(sent) == 1 and sent.media_fallback is False
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["See you there!"]


# ---- 10. Reminders: lock released during flood, persisted critical notice, undeliverable ------


async def test_reminder_delivery_lock_released_during_flood_wait(make_app):
    app = make_app()
    rem1 = await app.dispatch.reminders.create("2026-01-01T12:01", "First reminder", "normal")
    rem2 = await app.dispatch.reminders.create("2026-01-01T12:30", "Second reminder", "casual")

    sleeping = asyncio.Event()
    resume = asyncio.Event()
    send_orig = app.bot.send_message
    attempts = 0

    async def paused_sleep(seconds: float) -> None:
        if seconds >= 2.0:
            sleeping.set()
            await resume.wait()

    async def flood_once(chat_id, text, **kw):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _retry_after(2)
        return await send_orig(chat_id, text, **kw)

    app.sleep = paused_sleep
    app.bot.send_message = flood_once
    app.dispatch.now.advance(minutes=1)

    tick_task = asyncio.create_task(app.dispatch.reminders.tick())
    await asyncio.wait_for(sleeping.wait(), timeout=2.0)

    # While rem1 is sleeping on Telegram flood control, _delivery_lock is NOT held, so cancel() completes immediately.
    assert not app.dispatch.reminders._delivery_lock.locked()
    assert await asyncio.wait_for(app.dispatch.reminders.cancel(rem2), timeout=1.0) is True

    resume.set()
    await asyncio.wait_for(tick_task, timeout=2.0)
    assert [r["id"] for r in await app.dispatch.reminders.active()] == []
    assert attempts == 2 and rem1 > 0


async def test_critical_reminder_persists_state_and_does_not_repeat_notice_after_restart(make_app, caplog):
    app = make_app()
    reminder_id = await add_reminder(app, "critical")
    send_orig = app.bot.send_message
    failing = {"on": True}
    delivery_attempts = 0
    alert_attempts = 0

    async def controlled_send(chat_id, text, **kw):
        nonlocal delivery_attempts, alert_attempts
        if text.startswith("Critical reminder #"):
            alert_attempts += 1
        else:
            delivery_attempts += 1
        if failing["on"]:
            raise OSError("network down")
        return await send_orig(chat_id, text, **kw)

    app.bot.send_message = controlled_send
    app.dispatch.now.advance(minutes=1)
    with caplog.at_level(logging.ERROR):
        for _ in range(11):
            await app.dispatch.tick()
            app.dispatch.now.advance(minutes=60)

    assert delivery_attempts == 11
    assert alert_attempts == 1
    assert [r["id"] for r in await app.dispatch.reminders.active()] == [reminder_id]
    owner_events = [e["text"] for e in await events(app) if "Critical reminder" in e["text"]]
    assert len(owner_events) == 1 and "still retrying" in owner_events[0]

    # Simulate process restart: fresh Reminders instance with empty in-memory state.
    app.dispatch.reminders = Reminders(app)
    for _ in range(2):
        await app.dispatch.tick()
        app.dispatch.now.advance(minutes=60)

    # Still retried, still active, and owner notice was NOT repeated after restart!
    assert delivery_attempts == 13
    assert alert_attempts == 1
    owner_events_after = [e["text"] for e in await events(app) if "Critical reminder" in e["text"]]
    assert len(owner_events_after) == 1

    # Once Telegram recovers, the critical reminder is delivered (marked late after the outage) and finished.
    failing["on"] = False
    await app.dispatch.tick()
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["hey\n⏰ Check the kettle (late)"]
    assert await app.dispatch.reminders.active() == []


@pytest.mark.parametrize("error_factory", [
    lambda: TelegramForbiddenError(
        method=SendMessage(chat_id=OWNER, text="x"),
        message="Forbidden: bot was blocked by the user",
    ),
    lambda: TelegramBadRequest(
        method=SendMessage(chat_id=OWNER, text="x"),
        message="Bad Request: chat not found",
    ),
])
async def test_critical_reminder_blocked_or_missing_marked_undeliverable_and_kept_active(make_app, error_factory):
    app = make_app()
    reminder_id = await app.dispatch.reminders.create("2026-01-01T12:01", "Take meds", "critical")
    send_attempts = 0

    async def gone_send(chat_id, text, **kw):
        nonlocal send_attempts
        send_attempts += 1
        raise error_factory()

    app.bot.send_message = gone_send
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()

    # Marked undeliverable while remaining active, owner event recorded once.
    active = await app.dispatch.reminders.active()
    assert len(active) == 1 and active[0]["id"] == reminder_id
    assert active[0]["status"] == "active" and active[0]["undeliverable"] == 1
    assert send_attempts == 1
    owner_events = [e["text"] for e in await events(app) if "undeliverable" in e["text"]]
    assert len(owner_events) == 1

    # Subsequent ticks (and even after restart) do not keep hammering Telegram or repeating the event.
    app.dispatch.now.advance(hours=2)
    await app.dispatch.tick()
    app.dispatch.reminders = Reminders(app)
    app.dispatch.now.advance(hours=2)
    await app.dispatch.tick()
    assert send_attempts == 1
    assert len([e["text"] for e in await events(app) if "undeliverable" in e["text"]]) == 1


async def test_undeliverable_critical_reminder_shown_in_dm_dashboard_and_excluded_from_home(make_app, browser):
    from dom.telegram import owner

    app = make_app()
    orig_send = app.bot.send_message
    rem_id = await app.dispatch.reminders.create("2026-01-01T12:01", "Take meds", "critical")

    async def blocked_send(chat_id, text, **kw):
        raise TelegramForbiddenError(method=SendMessage(chat_id=OWNER, text="x"), message="Forbidden: bot was blocked")

    app.bot.send_message = blocked_send
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()

    # Restore send so /reminders can render the undeliverable reminder.
    app.bot.send_message = orig_send
    await owner.show_reminders(app)
    dm_text = app.bot.of("send", OWNER)[-1][2]
    assert f"#{rem_id}" in dm_text and "undeliverable" in dm_text

    async with browser(app) as client:
        # The reminders API lists it as undeliverable.
        listed, = (await client.get("/api/work/reminders")).json()["reminders"]
        assert listed["id"] == rem_id and listed["importance"] == "critical" and listed["undeliverable"] is True

        # Home page "Next reminder" excludes the undeliverable critical reminder.
        assert (await client.get("/api/home")).json()["today"]["next_reminder"] is None

        # When a later deliverable reminder exists, home shows the deliverable one.
        await app.dispatch.reminders.create("2026-01-01T15:00", "Call the dentist", "normal")
        upcoming = (await client.get("/api/home")).json()["today"]["next_reminder"]
        assert upcoming["text"] == "Call the dentist" and "15:00" in upcoming["when"]


async def test_remaining_telegram_send_paths_retry_on_flood_control(make_app):
    from types import SimpleNamespace
    from dom.plugins import surfaces
    from dom.t3.tools import Bridge
    from dom.tools import WRITE_BRIEF_TOOL, TurnCtx

    app = make_app()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    app.sleep = record_sleep
    orig_send = app.bot.send_message
    fail_next = True

    async def flaky_send(chat_id, text, **kw):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise _retry_after(1)
        return await orig_send(chat_id, text, **kw)

    app.bot.send_message = flaky_send

    # 1. plugins/surfaces._reply
    fail_next = True
    await surfaces._reply(app, OWNER, "plugin reply")
    assert any(c[2] == "plugin reply" for c in app.bot.of("send", OWNER))

    # 2. selfwork._announce and _report
    now = app.dispatch.now()
    rows = await app.store.add_self_tasks(
        [{"goal": "Read docs", "why": "Learn", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10}],
        now, now,
    )
    fail_next = True
    assert await app.self_work._announce("Trying something", rows, now) is True
    fail_next = True
    await app.self_work._report({**rows[0], "status": "failed", "error": "stopped"})
    assert any("Stopped my own task" in c[2] for c in app.bot.of("send", OWNER))

    # 3. memory_pass._tell_owner
    fail_next = True
    await MemoryPass(app)._tell_owner("likes oolong")
    assert any("Noted: likes oolong" in c[2] for c in app.bot.of("send", OWNER))

    # 4. t3/tools: notify, stage, remind, show
    bridge = Bridge(app)
    fail_next = True
    await bridge.notify("t3 ping")
    assert any(c[2] == "t3 ping" for c in app.bot.of("send", OWNER))

    fail_next = True
    res = await bridge.stage(
        {"environmentId": "e1", "label": "dev"},
        {"threadId": "t1", "title": "Fix bug"},
        "hello thread",
    )
    assert res == "Confirmation sent to the owner."

    bridge.token_path.write_text(json.dumps({"expires_at": 100.0}))
    fail_next = True
    await bridge.remind()
    assert any("sign-in has expired" in c[2] for c in app.bot.of("send", OWNER))

    bridge.client = SimpleNamespace(environments=lambda: [{"environmentId": "e1", "label": "dev", "status": "reachable"}])
    fail_next = True
    await bridge.show()
    assert any("T3 bridge on" in c[2] for c in app.bot.of("send", OWNER))

    # 5. tools._write_brief send_document retries under TelegramRetryAfter and handles FloodBudgetExhausted cleanly
    sent_docs: list[tuple] = []
    doc_error = {"retry_after": 1, "once": True}

    async def flaky_send_document(chat_id, document, **kw):
        if doc_error["once"]:
            doc_error["once"] = False
            raise _retry_after(doc_error["retry_after"])
        sent_docs.append((chat_id, document.filename, kw.get("caption")))
        return SimpleNamespace(message_id=9001)

    app.bot.send_document = flaky_send_document
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, OWNER, app=app)
    brief_args = {
        "title": "Retry brief send",
        "problem": "Flood control can interrupt document delivery.",
        "proposal": "Route brief document sends through retry_telegram.",
        "acceptance": "Document is delivered after a transient flood wait.",
    }
    first = json.loads(await WRITE_BRIEF_TOOL.handler(brief_args, ctx))
    assert "error" not in first and len(sent_docs) == 1

    # Exceeding flood budget returns clean JSON error instead of raising.
    doc_error["retry_after"] = 30
    doc_error["once"] = True
    second = json.loads(await WRITE_BRIEF_TOOL.handler(brief_args, ctx))
    assert "error" in second and "could not send" in second["error"]

    # Every retried path waited via app.sleep.
    assert sleeps.count(1.0) >= 9


async def test_an_undeliverable_critical_reminder_is_retried_slowly_until_one_delivery_clears_it(make_app):
    app = make_app()
    reminder_id = await app.dispatch.reminders.create("2026-01-01T12:01", "Take meds", "critical")
    gone, attempts = True, 0
    real_send = app.bot.send_message

    async def send(chat_id, text, **kw):
        nonlocal attempts
        attempts += 1
        if gone:
            raise TelegramForbiddenError(method=SendMessage(chat_id=OWNER, text="x"), message="Forbidden: bot was blocked by the user")
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = send
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert attempts == 1 and (await app.dispatch.reminders.active())[0]["undeliverable"] == 1

    app.dispatch.now.advance(hours=5)  # inside the slow cadence: left alone
    await app.dispatch.tick()
    assert attempts == 1

    app.dispatch.now.advance(hours=2)  # past it: tried again, still gone, still active, no second owner event
    await app.dispatch.tick()
    assert attempts == 2
    assert (await app.dispatch.reminders.active())[0]["undeliverable"] == 1
    assert len([e["text"] for e in await events(app) if "undeliverable" in e["text"]]) == 1

    app.dispatch.reminders = Reminders(app)  # a restart keeps the cadence
    app.dispatch.now.advance(hours=1)
    await app.dispatch.tick()
    assert attempts == 2

    gone = False  # the owner unblocked the bot
    app.dispatch.now.advance(hours=6)
    await app.dispatch.tick()
    assert attempts == 3
    assert await app.dispatch.reminders.active() == []  # delivered: finished, and the mark went with it
    row = await app.store.get_reminder(OWNER, reminder_id)
    assert row["status"] != "active" and not row["undeliverable"]


# ---- 11. v0.8.2 W6 edge cases left from v0.8.1 reviews ----------------------------------------


async def test_forum_catchup_scopes_prompt_unread_and_reply_per_topic_including_general(make_app):
    app = make_app(contents=(
        json.dumps({"bubbles": [{"text": "reply general"}]}),
        json.dumps({"bubbles": [{"text": "reply alpha"}]}),
        json.dumps({"bubbles": [{"text": "reply beta"}]}),
    ))
    await app.store.set_chat_auth_status(GROUP, "approved")
    app.dispatch.now.at = app.dispatch.now.at.replace(hour=2)  # asleep

    general_msg = tg(GROUP, FRIEND, text="general question", msg_id=10).model_copy(
        update={"date": app.dispatch.now(), "message_thread_id": None, "is_topic_message": False},
    )
    alpha_msg = tg(GROUP, FRIEND, text="alpha topic question", msg_id=11).model_copy(
        update={"date": app.dispatch.now(), "message_thread_id": 77, "is_topic_message": True},
    )
    beta_msg = tg(GROUP, FRIEND, text="beta topic question", msg_id=12).model_copy(
        update={"date": app.dispatch.now(), "message_thread_id": 88, "is_topic_message": True},
    )
    for message in (general_msg, alpha_msg, beta_msg):
        await handlers.handle_message(app, message)
    assert app.bot.of("send", GROUP) == []

    # Simulate restart and wake up 6 hours later for catch-up.
    clock = Clock(app.dispatch.now() + timedelta(hours=6))
    presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence, None, no_sleep, clock)
    await app.dispatch.tick()

    requests = turn_requests(app)
    assert len(requests) == 3
    prompts = [req["messages"][-1]["content"] for req in requests]
    assert "general question" in prompts[0] and "alpha topic" not in prompts[0] and "beta topic" not in prompts[0]
    assert "alpha topic question" in prompts[1] and "general question" not in prompts[1] and "beta topic" not in prompts[1]
    assert "beta topic question" in prompts[2] and "general question" not in prompts[2] and "alpha topic" not in prompts[2]

    sent = app.bot.of("send", GROUP)
    assert [(c[2], c[3]["message_thread_id"]) for c in sent] == [
        ("reply general", None),
        ("reply alpha", 77),
        ("reply beta", 88),
    ]
    assert await app.store.get_unread(GROUP) == (0, None)


async def test_forum_marking_one_topic_read_leaves_earlier_unread_topics_unread(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved")
    await online(app, GROUP)

    # Record unread messages in General (msg 10) and Topic 77 (msg 20).
    for msg_id, thread_id, is_topic, text in [
        (10, None, False, "waiting in general"),
        (20, 77, True, "newer in topic 77"),
    ]:
        await app.store.record_new_message(
            StoredMessage(
                chat_id=str(GROUP), msg_id=msg_id, thread_id=thread_id, sender_user_id=FRIEND,
                visibility_scope=app.store.visibility_for(GROUP), text=text,
                created_at=app.dispatch.now().isoformat(),
            )
        )
    # Mark ONLY Topic 77 read up to msg 20: General msg 10 must remain unread!
    await app.store.mark_chat_read(GROUP, 20, thread_id=77)
    topics = await app.store.get_unread_topics(GROUP)
    assert [(tid, count, msg.msg_id) for tid, count, msg in topics] == [(None, 1, 10)]

    # TOPIC_CLOSED on Topic 77 gives up on Topic 77 only, still answering General.
    await app.store.record_new_message(
        StoredMessage(
            chat_id=str(GROUP), msg_id=21, thread_id=77, sender_user_id=FRIEND,
            visibility_scope=app.store.visibility_for(GROUP), text="closed topic ping",
            created_at=(app.dispatch.now() - timedelta(seconds=30)).isoformat(),
        )
    )
    real_send = app.bot.send_message

    async def closed_77(chat_id, text, **kw):
        if kw.get("message_thread_id") == 77:
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="Bad Request: TOPIC_CLOSED")
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = closed_77
    app.dispatch.now.advance(seconds=30)
    await app.dispatch.tick()
    assert [(c[2], c[3]["message_thread_id"]) for c in app.bot.of("send", GROUP)] == [("hey", None)]
    assert await app.store.get_unread(GROUP) == (0, None)


@pytest.mark.parametrize("mid_multi_bubble", [False, True])
async def test_supergroup_upgrade_mid_reply_delivers_remaining_reply_in_new_chat(make_app, mid_multi_bubble):
    old_id, new_id = -556, -10556
    payload = (
        json.dumps({"bubbles": [{"text": "bubble one", "reply_to": 1}, {"text": "bubble two"}]})
        if mid_multi_bubble
        else json.dumps({"bubbles": [{"text": "hello migrated", "reply_to": 1}]})
    )
    app = make_app(contents=(payload,))
    await online(app, old_id)
    real_send = app.bot.send_message

    async def migrate_on_send(chat_id, text, **kw):
        if chat_id == old_id and (not mid_multi_bubble or text == "bubble two"):
            raise TelegramMigrateToChat(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Bad Request: group chat was upgraded to a supergroup chat",
                migrate_to_chat_id=new_id,
            )
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = migrate_on_send
    await deliver(app, tg(old_id, FRIEND, text="hello before upgrade", msg_id=1))

    assert await app.store.resolve_chat_id(old_id) == str(new_id)
    assert await app.store.is_chat_approved(new_id) is True
    assert not app.locks[old_id].locked() and not app.locks[new_id].locked()
    if mid_multi_bubble:
        assert [c[2] for c in app.bot.of("send", old_id)] == ["bubble one"]
        assert [c[2] for c in app.bot.of("send", new_id)] == ["bubble two"]
    else:
        assert app.bot.of("send", old_id) == []
        sent_new = app.bot.of("send", new_id)
        assert [c[2] for c in sent_new] == ["hello migrated"]
        assert sent_new[0][3].get("reply_parameters") is None
    new_window = await app.store.get_recent_chat_window(new_id)
    expected_text = "bubble two" if mid_multi_bubble else "hello migrated"
    assert any(m.role == "assistant" and m.text == expected_text for m in new_window)


async def test_supergroup_upgrade_mid_override_wake_delivers_reply_in_new_chat(make_app):
    old_id, new_id = -558, -10558
    app = make_app(contents=(json.dumps({"bubbles": [{"text": "woke up in new chat"}]}),))
    await app.store.set_chat_auth_status(old_id, "approved", title="Wake Crew")
    await handlers.handle_message(app, tg(old_id, FRIEND, text="wake up!", msg_id=1))
    real_send = app.bot.send_message

    async def migrate_on_old(chat_id, text, **kw):
        if chat_id == old_id:
            raise TelegramMigrateToChat(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Bad Request: group chat was upgraded to a supergroup chat",
                migrate_to_chat_id=new_id,
            )
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = migrate_on_old
    assert await app.dispatch.override(old_id, "/wake") is True
    assert [c[2] for c in app.bot.of("send", new_id)] == ["woke up in new chat"]


@pytest.mark.parametrize("cancel_mid_send", [False, True])
async def test_critical_reminder_does_not_resend_chunk_one_after_restart_mid_send(make_app, cancel_mid_send):
    app = make_app()
    part = "x" * 2750
    text = f"Details follow.\n\n```text\n{(part + chr(10)) * 3}```"
    await app.dispatch.reminders.create("2026-01-01T12:01", text, "critical", "")
    real_send = app.bot.send_message
    attempts: list[str] = []
    chunk2_started = asyncio.Event()

    async def fail_or_hang_second(chat_id, msg_text, **kw):
        attempts.append(msg_text)
        if len(attempts) == 2:
            if cancel_mid_send:
                chunk2_started.set()
                await asyncio.Future()
            raise OSError("network dropped after chunk 1")
        return await real_send(chat_id, msg_text, **kw)

    app.bot.send_message = fail_or_hang_second
    app.dispatch.now.advance(minutes=1)
    if cancel_mid_send:
        task = asyncio.create_task(app.dispatch.reminders.tick())
        await asyncio.wait_for(chunk2_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await app.dispatch.tick()
    assert len(app.bot.of("send", OWNER)) == 1
    assert len(attempts) == 2

    # Process restarts mid-send (fresh Reminders instance with empty memory).
    app.dispatch.reminders = Reminders(app)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()

    sent = app.bot.of("send", OWNER)
    assert [call[2] for call in sent] == ["hey\n⏰ Details follow.", part, part, part]
    assert all(call[3]["entities"][0].type == "pre" and call[3]["entities"][0].language == "text" for call in sent[1:])
    assert len(attempts) == 5  # chunk 1 once, chunk 2 failed/cancelled then succeeded, chunks 3 and 4 once each
    assert len(app.script.requests) == 1  # never re-styled after restart
    assert await app.dispatch.reminders.active() == []


async def test_group_no_answer_cooldown_survives_restart_and_migration(make_app):
    old_id, new_id = -557, -10557
    app = make_app(contents=("prose", "prose") * 6)
    await online(app, old_id)

    await deliver(app, tg(old_id, FRIEND, text="@dombot help?", msg_id=10))
    assert [c[2] for c in app.bot.of("send", old_id)] == [FRIEND_NO_ANSWER_LINE]

    # Simulate process restart with a fresh Dispatcher.
    app.bot.calls.clear()
    clock = Clock(app.dispatch.now() + timedelta(minutes=5))
    presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence, None, no_sleep, clock)
    await deliver(app, tg(old_id, FRIEND, text="@dombot still there?", msg_id=11))
    assert app.bot.of("send", old_id) == []

    # Migrate to supergroup and restart again: cooldown still applies within 30 minutes.
    assert await app.dispatch.migrate_chat(old_id, new_id, title="Crew") == "migrated"
    clock = Clock(clock() + timedelta(minutes=5))
    presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence, None, no_sleep, clock)
    await deliver(app, tg(new_id, FRIEND, text="@dombot after upgrade?", msg_id=12, chat_type="supergroup"))
    assert app.bot.of("send", new_id) == []

    # Once 30 minutes pass from the original fallback, directly addressed failure replies again.
    clock.advance(minutes=25)
    await online(app, new_id)
    await deliver(app, tg(new_id, FRIEND, text="@dombot now?", msg_id=13, chat_type="supergroup"))
    assert [c[2] for c in app.bot.of("send", new_id)] == [FRIEND_NO_ANSWER_LINE]


async def test_flood_limited_catchup_sending_nothing_retries_as_catchup_not_online_turn(make_app):
    jev = FakeJev("ignore", confidence=1.0)
    app = make_app()
    clock = Clock(NOW.replace(hour=2))
    presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence, jev, no_sleep, clock)
    await app.store.set_chat_auth_status(GROUP, "approved")

    for msg_id in (1, 2):
        await handlers.handle_message(
            app, tg(GROUP, FRIEND, text=f"offline msg {msg_id}", msg_id=msg_id).model_copy(update={"date": clock()}),
        )
    await drain_dispatch(app.dispatch)
    assert not app.bot.of("send", GROUP)

    real_send = app.bot.send_message
    flood = {"on": True}

    async def maybe_flood(chat_id, text, **kw):
        if flood["on"]:
            raise _retry_after(60)
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = maybe_flood
    clock.advance(hours=6)
    await app.dispatch.tick()

    # Dom is now online, nothing went out, messages remain unread.
    assert (await app.dispatch.presence.state(GROUP, parse_habits("")))[0] == "online"
    assert app.bot.of("send", GROUP) == []
    assert (await app.store.get_unread(GROUP))[0] == 2

    # Even after a restart while still online, the retry runs as a catch-up so Jev cannot ignore the backlog.
    flood["on"] = False
    clock.advance(minutes=1)
    presence2 = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
    app.dispatch = Dispatcher(app, presence2, jev, no_sleep, clock)
    await app.dispatch.tick()

    assert jev.calls == []  # Jev online triage was skipped!
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["hey"]
    assert (await app.store.get_unread(GROUP))[0] == 0
    note = turn_requests(app)[-1]["messages"][-1]["content"]
    assert "messages that arrived while away: 2" in note and "you just came online" in note


async def test_reaction_with_flood_limited_first_bubble_does_not_mark_read(make_app):
    payload = json.dumps({"react": "🔥", "bubbles": [{"text": "hello after react"}]})
    app = make_app(contents=(payload, payload))
    await online(app, GROUP)
    real_send = app.bot.send_message
    flood = {"on": True}

    async def maybe_flood(chat_id, text, **kw):
        if flood["on"]:
            raise _retry_after(60)
        return await real_send(chat_id, text, **kw)

    app.bot.send_message = maybe_flood
    await deliver(
        app, tg(GROUP, FRIEND, text="great news!", msg_id=10).model_copy(update={"date": app.dispatch.now()}),
    )

    # Reaction went out, first bubble hit flood limit -> NOT marked read!
    assert len(app.bot.of("react", GROUP)) == 1
    assert app.bot.of("send", GROUP) == []
    assert (await app.store.get_unread(GROUP))[0] == 1
    [turn] = await app.store.recent_turns(str(GROUP))
    assert turn["outcome"] == "Flood limited"

    # Once flood clears, next tick delivers the reply and marks read.
    flood["on"] = False
    app.dispatch.now.advance(seconds=30)
    await app.dispatch.tick()
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["hello after react"]
    assert (await app.store.get_unread(GROUP))[0] == 0


async def test_model_reaction_runs_after_send_boundary_so_restart_cannot_react_twice(make_app, monkeypatch):
    app = make_app(contents=(
        json.dumps({"react": "🔥", "bubbles": [{"text": "obsolete"}]}),
        json.dumps({"react": "🔥", "bubbles": [{"text": "updated"}]}),
    ))
    app.dispatch.jev = FakeJev(done=1)
    await online(app, OWNER)
    typing_started, cancelled = asyncio.Event(), asyncio.Event()

    async def pause_typing(_):
        if not typing_started.is_set():
            typing_started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    monkeypatch.setattr(app, "sleep", pause_typing)
    await handlers.handle_message(app, tg(OWNER, OWNER, text="first", msg_id=1))
    await asyncio.wait_for(typing_started.wait(), timeout=2)
    # No reaction has been sent yet while paused before the send boundary!
    assert app.bot.of("react", OWNER) == []

    await handlers.handle_message(app, tg(OWNER, OWNER, text="second", msg_id=2))
    await asyncio.wait_for(drain_dispatch(app.dispatch), timeout=2)

    assert cancelled.is_set()
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["updated"]
    # Reacted exactly once (on the final non-restarted attempt, to msg 2).
    assert [(c[1], c[2], c[3][0].emoji) for c in app.bot.of("react", OWNER)] == [(OWNER, 2, "🔥")]

    # Reaction-only turn: send boundary commits before set_message_reaction so an in-flight reaction cannot restart.
    app.bot.calls.clear()
    app.script.contents[:] = [json.dumps({"react": "👍"}), json.dumps({"bubbles": [{"text": "got msg 4"}]})]
    reacting, release_react = asyncio.Event(), asyncio.Event()
    real_react = app.bot.set_message_reaction

    async def slow_react(chat_id, message_id, **kw):
        reacting.set()
        await release_react.wait()
        return await real_react(chat_id, message_id, **kw)

    monkeypatch.setattr(app, "sleep", no_sleep)
    monkeypatch.setattr(app.bot, "set_message_reaction", slow_react)
    await handlers.handle_message(app, tg(OWNER, OWNER, text="third", msg_id=3))
    await asyncio.wait_for(reacting.wait(), timeout=2)
    await handlers.handle_message(app, tg(OWNER, OWNER, text="fourth", msg_id=4))
    release_react.set()
    await asyncio.wait_for(drain_dispatch(app.dispatch), timeout=2)
    assert [(c[1], c[2], c[3][0].emoji) for c in app.bot.of("react", OWNER)] == [(OWNER, 3, "👍")]
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["got msg 4"]

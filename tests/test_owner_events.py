"""Owner commands and button presses are recorded as event rows in the owner's DM."""

import asyncio

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import LeaveChat
from aiogram.types import CallbackQuery, User
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg

from dom.models import StoredMessage
from dom.store.store import format_chat_window_xml
from dom.telegram import handlers

BOOK_CLUB = -2002  # approved group


async def say(app, chat_id, sender, text, msg_id):
    await handlers.handle_message(app, tg(chat_id=chat_id, sender=sender, text=text, msg_id=msg_id))
    await drain_dispatch(app.dispatch)


async def press(app, data, sender=OWNER, markup=None):
    message = tg(chat_id=OWNER, sender=OWNER, msg_id=77)
    if markup is not None:  # the keyboard of the message the button is on
        message = message.model_copy(update={"reply_markup": markup})
    query = CallbackQuery(
        id="cb1", from_user=User(id=sender, is_bot=False, first_name="Fran"),
        chat_instance="ci", data=data, message=message,
    )
    await handlers.handle_callback(app, query)
    await drain_dispatch(app.dispatch)


async def events(app, chat_id=OWNER):
    rows = await asyncio.to_thread(
        lambda: app.store._conn.execute(
            "SELECT role, visibility_scope, msg_id, text FROM messages WHERE chat_id = ? AND role = 'event' ORDER BY id",
            (str(chat_id),),
        ).fetchall()
    )
    return [dict(r) for r in rows]


async def texts(app):
    return [e["text"] for e in await events(app)]


@pytest_asyncio.fixture
async def app(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await app.store.set_chat_auth_status(str(BOOK_CLUB), "approved", title="Book Club")
    await app.store.set_chat_auth_status(str(FRIEND), "approved", title="Sam")
    await say(app, GROUP, FRIEND, "yo", 1)  # pending group "The Group"
    app.bot.calls.clear()
    return app


# ---- storage and rendering --------------------------------------------------------------

async def test_event_row_is_owner_private_and_not_a_user_message(app):
    await app.store.record_owner_event("Fran ran /chats: nothing")
    (row,) = await events(app)
    assert row == {"role": "event", "visibility_scope": "owner_private", "msg_id": None, "text": "Fran ran /chats: nothing"}


async def test_window_renders_events_as_event_elements_and_they_count_toward_the_size(app):
    await app.store.record_owner_event("Fran ran /status <b>")
    window = await app.store.get_recent_chat_window(OWNER, 35)
    xml = format_chat_window_xml(str(OWNER), window)
    assert '<event time="' in xml and ">Fran ran /status &lt;b&gt;</event>" in xml
    assert xml.count("<msg ") == 1  # only "hi"
    assert [m.role for m in await app.store.get_recent_chat_window(OWNER, 1)] == ["event"]


async def test_events_never_count_as_unread_or_start_a_turn(app):
    await app.store.mark_chat_read(str(OWNER), 1)
    await app.store.record_owner_event("Fran ran /status")
    await app.store.record_owner_event("Fran ran /chats")
    assert await app.store.get_unread(OWNER) == (0, None)
    assert not await app.store.has_unread_human(OWNER)
    assert app.dispatch._tasks == set() and app.script.requests == []


async def test_events_are_not_search_results_for_other_chats(app):
    await app.store.record_owner_event("Fran ran /chats: pineapple")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(FRIEND), msg_id=5, sender_name="Sam", visibility_scope=f"chat:{FRIEND}",
        text="pineapple", created_at="2026-01-01T00:00:00+00:00",
    ))
    assert [h["chat_id"] for h in await app.store.search_chat_history("pineapple", str(FRIEND))] == [str(FRIEND)]
    assert [h["chat_id"] for h in await app.store.search_chat_history("pineapple", str(BOOK_CLUB))] == []
    assert str(OWNER) in {h["chat_id"] for h in await app.store.search_chat_history("pineapple", str(OWNER))}


async def test_events_are_not_sent_to_jev(app):
    await app.store.record_owner_event("Fran ran /chats: secret list")
    window = await app.store.get_recent_chat_window(OWNER, 35)
    assert "secret list" in app.store.format_chat_window_xml(OWNER, window)
    assert "secret list" not in app.dispatch._triage_xml(OWNER, window)


# ---- commands ---------------------------------------------------------------------------------

async def test_chats_command_records_what_was_listed(app):
    await say(app, OWNER, OWNER, "/chats", 10)
    assert await texts(app) == ["Fran ran /chats: 2 groups (The Group, Book Club), DM with Sam, your DM"]


async def test_status_command_records_each_chats_state(app):
    await say(app, OWNER, OWNER, "/status", 10)
    (text,) = await texts(app)
    assert text.startswith("Fran ran /status: ") and "Book Club " in text and "your DM " in text


async def test_pending_command_records_the_waiting_chats(app):
    await say(app, OWNER, OWNER, "/pending", 10)
    assert await texts(app) == ["Fran ran /pending: 1 waiting (The Group)"]


async def test_wake_and_sleep_commands(app):
    await say(app, OWNER, OWNER, f"/sleep {BOOK_CLUB}", 10)
    await say(app, OWNER, OWNER, "/wake", 11)
    assert await texts(app) == ["Fran sent Dom to sleep in Book Club", "Fran woke Dom in your DM"]


async def test_failed_override_records_nothing(app):
    await say(app, OWNER, OWNER, "/wake -42", 10)
    assert await events(app) == []


async def test_commands_from_other_people_record_nothing(app):
    await say(app, FRIEND, FRIEND, "/chats", 10)
    await say(app, BOOK_CLUB, FRIEND, "/status", 11)
    assert await events(app) == [] and await events(app, FRIEND) == [] and await events(app, BOOK_CLUB) == []


async def test_events_stay_in_the_owner_dm_even_for_group_targets(app):
    await say(app, OWNER, OWNER, f"/wake {BOOK_CLUB}", 10)
    assert len(await events(app)) == 1 and await events(app, BOOK_CLUB) == []


# ---- callbacks --------------------------------------------------------------------------------

async def test_wake_and_sleep_buttons(app):
    await press(app, f"sleep:{BOOK_CLUB}")
    await press(app, f"wake:{BOOK_CLUB}")
    assert await texts(app) == ["Fran sent Dom to sleep in Book Club", "Fran woke Dom in Book Club"]


async def test_leave_buttons(app):
    await press(app, f"leave:{BOOK_CLUB}")
    await press(app, f"leaveyes:{BOOK_CLUB}")
    assert await texts(app) == [
        "Fran tapped Leave on Book Club (asked to confirm)",
        "Fran tapped Leave on Book Club: Dom left Book Club",
    ]


async def test_leave_that_telegram_refuses_is_recorded_as_already_gone(app):
    app.bot.leave_error = TelegramBadRequest(method=LeaveChat(chat_id=1), message="Bad Request: chat not found")
    await press(app, f"leaveyes:{BOOK_CLUB}")
    assert await texts(app) == ["Fran tapped Leave on Book Club: Dom wasn't in Book Club anymore"]


async def test_approve_and_ignore_buttons(app):
    await app.store.note_pending_chat("-3003", "Chess", "@x")
    await press(app, f"approve:{GROUP}")
    await press(app, "ignore:-3003")
    assert await texts(app) == ["Fran approved The Group", "Fran ignored Chess"]


async def test_status_refresh_button(app):
    await press(app, "status:refresh")
    assert await texts(app) == ["Fran refreshed the /status list"]


async def test_callbacks_from_others_or_for_unknown_chats_record_nothing(app):
    await press(app, f"wake:{BOOK_CLUB}", sender=FRIEND)
    await press(app, f"leaveyes:{BOOK_CLUB}", sender=FRIEND)
    await press(app, "wake:-777")
    await press(app, f"approve:{BOOK_CLUB}")  # not pending
    assert await events(app) == []


async def test_recording_failure_does_not_break_the_command(app, monkeypatch):
    async def boom(text):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app.store, "record_owner_event", boom)
    await say(app, OWNER, OWNER, "/chats", 10)
    assert len(app.bot.of("rich", OWNER)) == 1


async def test_events_do_not_wake_dom_or_count_toward_wake_after_messages(app):
    for _ in range(10):
        await app.store.record_owner_event("Fran ran /status")
    await drain_dispatch(app.dispatch)
    assert app.script.requests == []
    assert (await app.store.get_unread(OWNER))[0] == 1  # just "hi"

"""Telegram menu cards delete themselves after `menu_card_minutes`, and a restart forgets none."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import DeleteMessage

from dom.store import Store
from dom.telegram import cards, handlers, owner
from test_owner_controls import approve, callback, say
from test_traits import put
from tghelpers import FRIEND, OWNER, tg

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def rows(app):
    with sqlite3.connect(app.store.db_path) as conn:
        return conn.execute("SELECT chat_id, message_id, sent_at, expires_at FROM menu_cards ORDER BY message_id").fetchall()


def bad_request(text="Bad Request: message can't be deleted"):
    return TelegramBadRequest(method=DeleteMessage(chat_id=1, message_id=1), message=text)


@pytest.fixture
def app(make_app):
    app = make_app()
    app.dispatch.now.at = NOW
    return app


@pytest.mark.parametrize("command", ["/status", "/chats", "/preset"])
async def test_each_menu_card_is_recorded_with_its_expiry(app, command):
    await approve(app, FRIEND, "Fran")
    await say(app, OWNER, OWNER, command, 900)
    ((chat_id, message_id, sent_at, expires_at),) = rows(app)
    assert chat_id == OWNER and message_id > 0
    assert datetime.fromisoformat(sent_at) == NOW
    assert datetime.fromisoformat(expires_at) == NOW + timedelta(minutes=45)


async def test_reminder_and_proposal_cards_expire_too(app):
    await app.dispatch.reminders.create("2026-01-20T09:00", "Water the plants", "normal", "")
    await say(app, OWNER, OWNER, "/reminders", 901)
    await say(app, OWNER, OWNER, "/reminders", 902)
    put(app.home, "SOUL.md", "A curious friend.")
    await app.proposals.create("soul_edit", "A gentler voice", {"old": "curious", "new": "thoughtful"}, "example")
    assert len(rows(app)) == 2  # the proposal's own notice has approve buttons, not a Close button: it never expires
    await say(app, OWNER, OWNER, "/proposals", 903)
    assert len(rows(app)) == 3


async def test_notices_and_prompts_without_a_close_button_are_not_cards(app):
    await owner._send(app, "No plugins are installed.")
    await say(app, OWNER, OWNER, "/reminders", 901)  # nothing to list: a plain message
    assert rows(app) == []


async def test_the_configured_minutes_set_the_expiry_and_zero_keeps_cards(app):
    app.config = app.config.model_copy(update={"menu_card_minutes": 5.5})
    await say(app, OWNER, OWNER, "/status", 900)
    ((_, _, _, expires_at),) = rows(app)
    assert datetime.fromisoformat(expires_at) == NOW + timedelta(minutes=5.5)
    app.config = app.config.model_copy(update={"menu_card_minutes": 0})
    await say(app, OWNER, OWNER, "/chats", 901)
    assert len(rows(app)) == 1  # the second card was never recorded
    app.dispatch.now.advance(minutes=10)
    app.bot.calls.clear()
    assert await cards.sweep(app) == 1 and len(app.bot.of("delete")) == 1  # only the recorded card ever expires


async def test_a_card_is_deleted_once_its_time_is_up_and_not_before(app):
    await say(app, OWNER, OWNER, "/status", 900)
    ((_, message_id, *_),) = rows(app)
    app.bot.calls.clear()
    app.dispatch.now.advance(minutes=44, seconds=59)
    assert await cards.sweep(app) == 0 and app.bot.of("delete") == [] and len(rows(app)) == 1
    app.dispatch.now.advance(seconds=1)
    assert await cards.sweep(app) == 1
    assert app.bot.of("delete") == [("delete", OWNER, message_id)] and rows(app) == []
    assert await cards.sweep(app) == 0 and len(app.bot.of("delete")) == 1  # nothing is deleted twice


async def test_the_dispatcher_sweeps_in_the_background_and_at_startup(app):
    await say(app, OWNER, OWNER, "/status", 900)
    app.dispatch.now.advance(minutes=46)
    app.bot.calls.clear()
    await app.dispatch._background_jobs()
    assert len(app.bot.of("delete")) == 1 and rows(app) == []


async def test_a_restart_forgets_no_card(app, tmp_path):
    await say(app, OWNER, OWNER, "/status", 900)
    await say(app, OWNER, OWNER, "/chats", 901)
    ids = [row[1] for row in rows(app)]
    app.store.close()
    app.store = Store(tmp_path, owner_id=OWNER)  # a new process opens the same database
    app.dispatch.now.advance(hours=3)  # Dom was down past both expiries
    app.bot.calls.clear()
    await app.dispatch._sweep_cards()  # what the dispatcher does when it starts
    assert sorted(call[2] for call in app.bot.of("delete")) == sorted(ids)
    assert rows(app) == []


async def test_a_card_older_than_48_hours_only_loses_its_buttons(app):
    await say(app, OWNER, OWNER, "/status", 900)
    ((_, message_id, *_),) = rows(app)
    app.bot.calls.clear()
    app.dispatch.now.advance(hours=48)
    assert await cards.sweep(app) == 1
    assert app.bot.of("delete") == []  # Telegram would refuse: it is not even tried
    assert app.bot.of("markup") == [("markup", OWNER, message_id, None)] and rows(app) == []


async def test_a_refused_delete_falls_back_to_clearing_the_buttons(app):
    await say(app, OWNER, OWNER, "/status", 900)
    ((_, message_id, *_),) = rows(app)
    app.dispatch.now.advance(hours=1)
    app.bot.delete_error = bad_request()
    app.bot.calls.clear()
    assert await cards.sweep(app) == 1
    assert app.bot.of("markup") == [("markup", OWNER, message_id, None)] and rows(app) == []


async def test_a_card_the_owner_already_deleted_is_just_forgotten(app):
    await say(app, OWNER, OWNER, "/status", 900)
    app.dispatch.now.advance(hours=1)
    app.bot.delete_error = bad_request("Bad Request: message to delete not found")

    async def gone(**_):
        raise bad_request("Bad Request: message to edit not found")

    app.bot.edit_message_reply_markup = gone
    assert await cards.sweep(app) == 1 and rows(app) == []


@pytest.mark.parametrize("error", [
    TelegramRetryAfter(method=DeleteMessage(chat_id=1, message_id=1), message="flood", retry_after=500),
    TelegramNetworkError(method=DeleteMessage(chat_id=1, message_id=1), message="offline"),
    OSError("offline"),
])
async def test_a_flood_wait_or_network_failure_keeps_the_card_for_the_next_sweep(app, error):
    await say(app, OWNER, OWNER, "/status", 900)
    app.dispatch.now.advance(hours=1)
    app.bot.delete_error = error
    assert await cards.sweep(app) == 0 and len(rows(app)) == 1
    app.bot.delete_error = None
    assert await cards.sweep(app) == 1 and rows(app) == []


async def test_one_failing_card_does_not_hold_back_the_others(app):
    for msg_id in (900, 901, 902):
        await say(app, OWNER, OWNER, "/status", msg_id)
    first, second, third = (row[1] for row in rows(app))
    real = app.bot.delete_message

    async def flaky(chat_id, message_id, **kw):
        if message_id == second:
            raise OSError("offline")
        return await real(chat_id, message_id, **kw)

    app.bot.delete_message = flaky
    app.dispatch.now.advance(hours=1)
    assert await cards.sweep(app) == 2
    assert [row[1] for row in rows(app)] == [second]


async def test_a_sweep_handles_a_bounded_batch(app):
    for number in range(cards.SWEEP_BATCH + 5):
        await app.store.add_menu_card(OWNER, 1000 + number, NOW.isoformat(), NOW.isoformat())
    assert await cards.sweep(app) == cards.SWEEP_BATCH and len(rows(app)) == 5
    assert await cards.sweep(app) == 5 and rows(app) == []


async def test_closing_a_card_removes_it_at_once_and_leaves_nothing_to_expire(app):
    await say(app, OWNER, OWNER, "/status", 900)
    ((_, message_id, *_),) = rows(app)
    app.bot.calls.clear()
    message = tg(chat_id=OWNER, sender=OWNER, msg_id=message_id)
    await handlers.handle_callback(app, callback(app, OWNER, "card:close", message))
    assert app.bot.of("delete") == [("delete", OWNER, message_id)] and rows(app) == []


async def test_a_refreshed_status_card_replaces_the_old_one_and_expires_on_its_own_clock(app):
    await say(app, OWNER, OWNER, "/status", 900)
    app.dispatch.now.advance(minutes=30)
    await handlers.handle_callback(app, callback(app, OWNER, f"wake:{OWNER}"))
    new_cards = rows(app)
    assert len(new_cards) == 2  # the old one stays on its own clock until it is gone
    assert datetime.fromisoformat(new_cards[-1][3]) == NOW + timedelta(minutes=75)
    app.dispatch.now.advance(minutes=20)  # past the first card's expiry only
    app.bot.calls.clear()
    assert await cards.sweep(app) == 1 and len(rows(app)) == 1


async def test_a_failure_to_record_a_card_never_breaks_the_command(app, caplog):
    async def broken(*_):
        raise sqlite3.OperationalError("disk full")

    app.store.add_menu_card = broken
    await say(app, OWNER, OWNER, "/status", 900)
    assert len(app.bot.of("rich")) == 1 and "could not record a menu card" in caplog.text


async def test_the_migration_adds_the_table_and_keeps_old_data(tmp_path):
    from dom.store.store import TARGET_VERSION, migrate_database

    store = Store(tmp_path, owner_id=OWNER)
    await store.record_usage(NOW.isoformat(), "chat", "m", "h", None, 1, 1, 0.5, 0)
    store.close()
    path = tmp_path / "state.db"
    assert migrate_database(path) == TARGET_VERSION  # running it again changes nothing
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone() == (1,)
        assert [row[1] for row in conn.execute("PRAGMA table_info(menu_cards)")] == [
            "chat_id", "message_id", "sent_at", "expires_at"]

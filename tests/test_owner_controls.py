"""Owner controls: the "/" command menu, /status and its wake/sleep buttons."""

import asyncio

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, User
from tghelpers import FRIEND, GROUP, OWNER, FakeBot, drain_dispatch, tg

from dom import cli
from dom.models import StoredMessage
from dom.telegram import handlers
from dom.telegram.owner import OWNER_COMMANDS, publish_menu

FRIEND_GROUP = -2002


async def approve(app, chat_id, title=None):
    await app.store.set_chat_auth_status(str(chat_id), "approved", title=title)


async def say(app, chat_id, sender, text, msg_id):
    await handlers.handle_message(app, tg(chat_id=chat_id, sender=sender, text=text, msg_id=msg_id))
    await drain_dispatch(app.dispatch)


async def status_message(app):
    """Ask for /status and return the sent (text, reply_markup), clearing the bot's call log."""
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/status", msg_id=900))
    (_, chat_id, text, kw), = app.bot.of("send")
    assert chat_id == OWNER
    return text, kw["reply_markup"]


def callback(app, sender, data, message=None):
    return CallbackQuery(
        id="cb1", from_user=User(id=sender, is_bot=False, first_name="X"),
        chat_instance="ci", data=data, message=message or tg(chat_id=OWNER, sender=OWNER, msg_id=77),
    )


def buttons(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


async def chat_rows(app):
    return await asyncio.to_thread(
        lambda: app.store._conn.execute("SELECT chat_id FROM chats ORDER BY chat_id").fetchall()
    )


@pytest_asyncio.fixture
async def two_approved_one_pending(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await say(app, FRIEND, FRIEND, "hey", 1)
    await say(app, GROUP, FRIEND, "yo", 1)            # pending group
    await approve(app, FRIEND)
    await approve(app, FRIEND_GROUP, title="Book Club")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(FRIEND_GROUP), msg_id=1, sender_user_id=333, sender_name="Bo", is_bot=False,
        visibility_scope=f"chat:{FRIEND_GROUP}", text="newer", created_at="2099-01-01T00:00:00+00:00",
    ))
    return app


# ---- the command menu ----------------------------------------------------------------

def test_owner_commands():
    assert [(c.command, c.description) for c in OWNER_COMMANDS] == [
        ("status", "Where Dom is and what he's doing"),
        ("wake", "Wake Dom up"),
        ("sleep", "Send Dom to sleep"),
        ("pending", "Chats waiting for your approval"),
    ]


async def test_publish_menu_scopes_commands_to_the_owner_and_clears_the_default():
    bot = FakeBot()
    await publish_menu(bot, OWNER)
    (_, commands, scope), (_, default_scope) = bot.calls
    assert commands == OWNER_COMMANDS
    assert scope == BotCommandScopeChat(chat_id=OWNER)
    assert isinstance(default_scope, BotCommandScopeDefault)


async def test_publish_menu_failures_are_logged_not_raised(caplog):
    class Failing(FakeBot):
        async def set_my_commands(self, commands, scope=None, **kw):
            raise RuntimeError("telegram is down")

    bot = Failing()
    await publish_menu(bot, OWNER)
    assert "could not set the owner's command menu" in caplog.text
    assert [c[0] for c in bot.calls] == ["delete_commands"]  # the second call still ran


async def test_menu_is_published_after_a_successful_claim(make_app, tmp_path):
    app = make_app(owner_id=None)
    (tmp_path / "config.toml").write_text('[models.chat]\nbase_url = "https://x/v1"\nmodel = "m"\n')
    (tmp_path / ".claim").write_text("AB12CD34")
    await say(app, 222, 222, "/claim WRONG000", 1)
    assert app.bot.of("set_commands") == []
    await say(app, OWNER, OWNER, "/claim AB12CD34", 2)
    assert app.bot.of("set_commands") == [("set_commands", OWNER_COMMANDS, BotCommandScopeChat(chat_id=OWNER))]
    assert len(app.bot.of("delete_commands")) == 1


async def test_menu_is_published_on_startup_once_the_owner_is_known(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dom.config import load_config
    from dom.home import init_home
    from dom.claim import try_claim

    home = tmp_path / "home"
    init_home(home)
    (home / ".claim").write_text("AB12CD34")
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=t\nOPENROUTER_API_KEY=k\n")
    owned = try_claim(home, load_config(home), OWNER, "private", "AB12CD34")
    assert owned
    bots = []

    class StartupBot(FakeBot):
        def __init__(self, token):
            super().__init__()
            self.session = SimpleNamespace(close=self._close)
            bots.append(self)

        async def _close(self):
            pass

        async def get_me(self):
            return SimpleNamespace(id=999, username="dombot")

    class FakeDispatcher:
        def include_router(self, router):
            pass

        async def start_polling(self, bot, **kwargs):
            assert bot.of("set_commands")  # already published by the time polling starts

    monkeypatch.setattr(cli, "Bot", StartupBot)
    monkeypatch.setattr(cli, "Dispatcher", FakeDispatcher)
    await cli.run(home)
    bot, = bots
    assert bot.of("set_commands") == [("set_commands", OWNER_COMMANDS, BotCommandScopeChat(chat_id=OWNER))]
    assert len(bot.of("delete_commands")) == 1


# ---- /status ---------------------------------------------------------------------------

async def test_status_lists_approved_chats_with_buttons_and_a_footer(two_approved_one_pending):
    app = two_approved_one_pending
    text, markup = await status_message(app)
    lines = text.splitlines()
    body = [l for l in lines if "unread" in l]
    assert "your DM" in body[0] and "Book Club" in body[1] and "DM with Fran" in body[2]  # owner, then newest
    assert len(body) == 3 and "The Group" not in text  # pending chats are not listed
    assert "1 unread" in body[2]
    assert lines[-1] == "Pending approval: 1 · Chat model: m · Triage model: n/a"
    assert buttons(markup) == [
        [("☀️ Wake", f"wake:{chat}"), ("🌙 Sleep", f"sleep:{chat}")] for chat in (OWNER, FRIEND_GROUP, FRIEND)
    ]
    assert app.script.requests == []


async def test_status_shows_presence_with_until_in_the_configured_timezone(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "/wake", 1)          # Clock() starts 12:00 UTC; wake lasts 30 minutes
    text, _ = await status_message(app)
    assert "online until 12:30" in text


async def test_status_is_owner_only(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/status", msg_id=5))
    await handlers.handle_message(app, tg(chat_id=GROUP, sender=OWNER, text="/status", msg_id=6))
    assert not [c for c in app.bot.of("send") if c[2].startswith("Dom's chats")]
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/status", msg_id=7))
    assert len([c for c in app.bot.of("send", OWNER) if c[2].startswith("Dom's chats")]) == 1


async def test_status_never_creates_rows(make_app):
    app = make_app()  # the owner has not written yet: their DM is listed without a row
    await approve(app, FRIEND)
    before = await chat_rows(app)
    text, _ = await status_message(app)
    assert "your DM" in text and "DM with" in text
    assert await chat_rows(app) == before


async def test_status_truncates_a_long_list(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    for n in range(300):
        await approve(app, -5000 - n, title=f"Chat number {n} " + "x" * 40)
    text, markup = await status_message(app)
    assert len(text) <= 4096
    import re
    more = re.search(r"…and (\d+) more", text)
    assert more
    shown = len(markup.inline_keyboard)
    assert shown + int(more.group(1)) == 301
    assert len(markup.inline_keyboard) * 2 <= 100  # Telegram's button limit


# ---- wake / sleep buttons --------------------------------------------------------------

async def test_wake_and_sleep_buttons_change_presence_and_refresh_the_status(two_approved_one_pending):
    app = two_approved_one_pending
    await status_message(app)
    prompt = tg(chat_id=OWNER, sender=OWNER, msg_id=77)
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"wake:{FRIEND}", prompt))
    assert app.bot.of("answer") == [("answer", "cb1", "Dom is up in DM with Fran")]
    (_, chat_id, message_id, text), = app.bot.of("edit")
    assert (chat_id, message_id) == (OWNER, 77)
    line = next(l for l in text.splitlines() if "DM with Fran" in l)
    assert "online until 12:30" in line
    assert buttons(app.bot.edit_markups[-1])[0][0] == ("☀️ Wake", f"wake:{OWNER}")
    assert await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())

    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"sleep:{FRIEND}", prompt))
    assert app.bot.of("answer") == [("answer", "cb1", "Dom is asleep in DM with Fran")]
    line = next(l for l in app.bot.of("edit")[0][3].splitlines() if "DM with Fran" in l)
    assert "asleep" in line and "until" in line
    assert not await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())


async def test_buttons_from_a_non_owner_change_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    before = await app.store.get_presence(FRIEND)
    app.bot.calls.clear()
    for sender in (FRIEND, 555):
        await handlers.handle_callback(app, callback(app, sender, f"wake:{FRIEND}"))
        await handlers.handle_callback(app, callback(app, sender, f"sleep:{FRIEND}"))
    assert [c[2] for c in app.bot.of("answer")] == ["not allowed"] * 4
    assert app.bot.of("edit") == []
    assert await app.store.get_presence(FRIEND) == before


async def test_buttons_for_unknown_or_unapproved_chats_change_nothing_and_insert_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    before = await chat_rows(app)
    app.bot.calls.clear()
    for data in (f"wake:{GROUP}", f"sleep:{GROUP}", "wake:-123456", "sleep:424242", "wake:nope"):
        await handlers.handle_callback(app, callback(app, OWNER, data))
    assert [c[2] for c in app.bot.of("answer")] == ["unknown chat"] * 5
    assert app.bot.of("edit") == []
    assert await chat_rows(app) == before
    assert not await app.dispatch.presence.is_online(GROUP, app.dispatch._habits())


async def test_an_unchanged_status_message_is_not_an_error(two_approved_one_pending):
    app = two_approved_one_pending

    async def not_modified(*args, **kwargs):
        raise TelegramBadRequest(
            method=EditMessageText(text="x"), message="Bad Request: message is not modified"
        )

    app.bot.edit_message_text = not_modified
    await handlers.handle_callback(app, callback(app, OWNER, f"sleep:{FRIEND}"))
    assert app.bot.of("answer")[-1][2] == "Dom is asleep in DM with Fran"


# ---- /wake and /sleep without an argument ------------------------------------------------

@pytest.mark.parametrize("command,reply", [("/wake", "Dom is up."), ("/sleep", "Dom is asleep.")])
async def test_wake_and_sleep_without_an_argument_confirm(make_app, command, reply):
    app = make_app()
    await say(app, OWNER, OWNER, command, 1)
    assert [c[2] for c in app.bot.of("send", OWNER)] == [reply]
    assert (await app.dispatch.presence.is_online(OWNER, app.dispatch._habits())) == (command == "/wake")


async def test_wake_with_a_target_stays_silent(make_app):
    app = make_app()
    await approve(app, FRIEND)
    await say(app, OWNER, OWNER, f"/wake {FRIEND}", 1)
    assert app.bot.of("send", OWNER) == []
    assert await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())

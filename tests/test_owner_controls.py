"""Owner controls: the "/" command menu, /status and /chats (rich-message tables) and their buttons."""

import asyncio

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import LeaveChat
from aiogram.methods import DeleteMessage, SendRichMessage
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, InputRichMessage, User
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
    """Ask for /status and return the sent (markdown, reply_markup), clearing the bot's call log."""
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/status", msg_id=900))
    (_, chat_id, text, kw), = app.bot.of("rich")
    assert chat_id == OWNER and app.bot.of("send") == []
    return text, kw["reply_markup"]


def rows_of(markdown):
    """The body rows of every markdown table in a rich message, as lists of cell texts (header and separator skipped)."""
    rows = []
    for line in markdown.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            rows.append([cell.strip() for cell in line.strip("|").split(" | ")])
    return [r for r in rows if r[0] != "Chat"]


def only_replacement(app, old_message_id=77):
    """The one fresh rich message sent after the old /status or /chats message was deleted."""
    assert app.bot.of("delete") == [("delete", OWNER, old_message_id)]
    assert app.bot.of("edit") == []
    (_, chat_id, text, kw), = app.bot.of("rich")
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
        ("chats", "Every group and DM Dom knows about"),
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
    assert lines[0] == "**Dom's chats**"
    assert "| Chat | State | Until | Unread |" in lines and "|---|---|---|---|" in lines
    body = rows_of(text)
    assert [r[0] for r in body] == ["your DM", "Book Club", "DM with Fran"]  # owner, then newest
    assert "The Group" not in text  # pending chats are not listed
    assert body[2][3] == "1" and all(r[1].endswith(("asleep", "offline", "online")) for r in body)
    assert lines[-3:] == [
        "Pending approval: 1 · Chat model: m · Triage model: n/a",
        "Today: 0 calls · $0.00",
        "Last reply: —",
    ]
    assert buttons(markup) == [
        [("☀️ your DM", f"wake:{OWNER}"), ("🌙 Sleep", f"sleep:{OWNER}")],  # DMs have no Leave button
        [("☀️ Book Club", f"wake:{FRIEND_GROUP}"), ("🌙 Sleep", f"sleep:{FRIEND_GROUP}"),
         ("🚪 Leave", f"leave:{FRIEND_GROUP}")],
        [("☀️ DM with Fran", f"wake:{FRIEND}"), ("🌙 Sleep", f"sleep:{FRIEND}")],
    ]
    assert app.script.requests == []


async def test_status_shows_presence_with_until_in_the_configured_timezone(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "/wake", 1)          # Clock() starts 12:00 UTC; wake lasts 30 minutes
    text, _ = await status_message(app)
    row = next(r for r in rows_of(text) if r[0] == "your DM")
    assert row[1].endswith("online") and row[2] == "12:30"


async def test_status_is_owner_only(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/status", msg_id=5))
    await handlers.handle_message(app, tg(chat_id=GROUP, sender=OWNER, text="/status", msg_id=6))
    assert app.bot.of("rich") == []
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/status", msg_id=7))
    assert len(app.bot.of("rich", OWNER)) == 1


async def test_status_never_creates_rows(make_app):
    app = make_app()  # the owner has not written yet: their DM is listed without a row
    await approve(app, FRIEND)
    before = await chat_rows(app)
    text, _ = await status_message(app)
    assert "your DM" in text and "DM with" in text
    # /status leaves a note in the owner's DM history, so that one chat may appear; no other row may.
    assert {r["chat_id"] for r in await chat_rows(app)} - {r["chat_id"] for r in before} <= {str(OWNER)}


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
    assert shown == len(rows_of(text)) and shown + int(more.group(1)) == 301
    assert sum(len(row) for row in markup.inline_keyboard) <= 100  # Telegram's button limit


async def test_status_titles_cannot_break_the_table(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await approve(app, -5001, title="a | b *c* _d_ `e` \\f")
    text, _ = await status_message(app)
    row = next(l for l in text.splitlines() if l.startswith("| a "))
    assert row.startswith("| a \\| b \\*c\\* \\_d\\_ \\`e\\` \\\\f | ")
    assert row.count(" | ") == 3  # the escaped pipe is not a column separator


async def test_status_falls_back_to_plain_text_when_rich_messages_are_refused(two_approved_one_pending, caplog):
    app = two_approved_one_pending
    app.bot.rich_error = TelegramBadRequest(method=SendRichMessage(chat_id=1, rich_message=InputRichMessage(markdown="x")),
                                            message="Bad Request: method not found")
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/status", msg_id=900))
    (_, chat_id, text, kw), = app.bot.of("send")
    assert chat_id == OWNER and text.startswith("Dom's chats\n")
    assert any("Book Club: " in l and "unread" in l for l in text.splitlines())
    assert buttons(kw["reply_markup"])[1][0] == ("☀️ Book Club", f"wake:{FRIEND_GROUP}")
    assert sum("rich" in r.getMessage().lower() for r in caplog.records) == 1  # logged once


# ---- wake / sleep buttons --------------------------------------------------------------

async def test_wake_and_sleep_buttons_change_presence_and_refresh_the_status(two_approved_one_pending):
    app = two_approved_one_pending
    await status_message(app)
    prompt = tg(chat_id=OWNER, sender=OWNER, msg_id=77)
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"wake:{FRIEND}", prompt))
    assert app.bot.of("answer") == [("answer", "cb1", "Dom is up in DM with Fran")]
    text, markup = only_replacement(app)
    row = next(r for r in rows_of(text) if r[0] == "DM with Fran")
    assert row[1].endswith("online") and row[2] == "12:30"
    assert buttons(markup)[0][0] == ("☀️ your DM", f"wake:{OWNER}")
    assert await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())

    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"sleep:{FRIEND}", prompt))
    assert app.bot.of("answer") == [("answer", "cb1", "Dom is asleep in DM with Fran")]
    text, _ = only_replacement(app)
    row = next(r for r in rows_of(text) if r[0] == "DM with Fran")
    assert row[1].endswith("asleep") and row[2] != "—"
    assert not await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())


async def test_buttons_from_a_non_owner_change_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    before = await app.store.get_presence(FRIEND)
    app.bot.calls.clear()
    for sender in (FRIEND, 555):
        await handlers.handle_callback(app, callback(app, sender, f"wake:{FRIEND}"))
        await handlers.handle_callback(app, callback(app, sender, f"sleep:{FRIEND}"))
    assert [c[2] for c in app.bot.of("answer")] == ["not allowed"] * 4
    assert app.bot.of("rich") == [] and app.bot.of("delete") == []
    assert await app.store.get_presence(FRIEND) == before


async def test_buttons_for_unknown_or_unapproved_chats_change_nothing_and_insert_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    before = await chat_rows(app)
    app.bot.calls.clear()
    for data in (f"wake:{GROUP}", f"sleep:{GROUP}", "wake:-123456", "sleep:424242", "wake:nope"):
        await handlers.handle_callback(app, callback(app, OWNER, data))
    assert [c[2] for c in app.bot.of("answer")] == ["unknown chat"] * 5
    assert app.bot.of("rich") == [] and app.bot.of("delete") == []
    assert await chat_rows(app) == before
    assert not await app.dispatch.presence.is_online(GROUP, app.dispatch._habits())


async def test_a_status_message_that_is_already_gone_is_not_an_error(two_approved_one_pending):
    app = two_approved_one_pending
    app.bot.delete_error = TelegramBadRequest(
        method=DeleteMessage(chat_id=1, message_id=1), message="Bad Request: message to delete not found"
    )
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"sleep:{FRIEND}"))
    assert app.bot.of("answer")[-1][2] == "Dom is asleep in DM with Fran"
    assert len(app.bot.of("rich")) == 1  # the fresh message still goes out


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


# ---- waking a chat that the spend cap has paused ---------------------------------------------

PAUSED = "Dom is up in {}, but friends' chats are paused by today's spend cap"


async def spend_over_the_cap(app):
    await app.dispatch.usage.record("chat", "mock-chat", "example.org", {"cost": 99})


async def test_wake_command_for_a_friend_chat_over_the_cap_still_wakes_but_says_so(make_app):
    app = make_app()
    await approve(app, FRIEND, title="Fran")
    await spend_over_the_cap(app)
    await say(app, OWNER, OWNER, f"/wake {FRIEND}", 1)
    assert [c[2] for c in app.bot.of("send", OWNER) if c[2].startswith("Dom is up")] == [PAUSED.format("DM with Fran")]
    assert await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())


async def test_wake_button_for_a_friend_chat_over_the_cap_still_wakes_but_says_so(two_approved_one_pending):
    app = two_approved_one_pending
    await spend_over_the_cap(app)
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"wake:{FRIEND}"))
    assert app.bot.of("answer") == [("answer", "cb1", PAUSED.format("DM with Fran"))]
    assert await app.dispatch.presence.is_online(FRIEND, app.dispatch._habits())


async def test_waking_the_owners_own_chat_is_never_reported_as_paused(make_app):
    app = make_app()
    await spend_over_the_cap(app)
    await say(app, OWNER, OWNER, "/wake", 1)
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["Dom is up."]


# ---- leaving a group from /status ----------------------------------------------------------

def not_a_member(error):
    return error(method=LeaveChat(chat_id=1), message="Bad Request: bot is not a member of the group chat")


async def auth_status(app, chat_id):
    chats = {c["chat_id"]: c["authorization_status"] for c in await app.store.list_chats()}
    return chats[str(chat_id)]


async def test_leave_button_asks_for_confirmation_without_leaving(two_approved_one_pending):
    app = two_approved_one_pending
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"leave:{FRIEND_GROUP}"))
    assert app.bot.of("delete") == [("delete", OWNER, 77)] and app.bot.of("edit") == []
    (_, chat_id, text, kw), = app.bot.of("send")
    assert chat_id == OWNER
    assert text == "Leave Book Club? Friends will see Dom leave."
    assert buttons(kw["reply_markup"]) == [
        [("Yes, leave", f"leaveyes:{FRIEND_GROUP}"), ("Cancel", "status:refresh")]
    ]
    assert app.bot.of("leave") == []
    assert await auth_status(app, FRIEND_GROUP) == "approved"


async def test_confirming_leaves_the_group_and_refreshes_the_status(two_approved_one_pending):
    app = two_approved_one_pending
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"leaveyes:{FRIEND_GROUP}"))
    assert app.bot.of("leave") == [("leave", FRIEND_GROUP)]
    assert await auth_status(app, FRIEND_GROUP) == "left"
    assert app.bot.of("answer") == [("answer", "cb1", "Dom left Book Club")]
    text, _ = only_replacement(app)
    assert text.startswith("**Dom's chats**") and "Book Club" not in text


async def test_a_pending_or_ignored_group_can_be_left_too(two_approved_one_pending):
    app = two_approved_one_pending
    await app.store.set_chat_auth_status("-3003", "ignored", title="Old Chat")
    for chat_id in (GROUP, -3003):
        await handlers.handle_callback(app, callback(app, OWNER, f"leaveyes:{chat_id}"))
        assert await auth_status(app, chat_id) == "left"
    assert app.bot.of("leave") == [("leave", GROUP), ("leave", -3003)]


@pytest.mark.parametrize("data", [f"leave:{FRIEND_GROUP}", f"leaveyes:{FRIEND_GROUP}", "status:refresh"])
@pytest.mark.parametrize("sender", [FRIEND, 555])
async def test_leave_controls_from_a_non_owner_do_nothing(two_approved_one_pending, data, sender):
    app = two_approved_one_pending
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, sender, data))
    assert [c[2] for c in app.bot.of("answer")] == ["not allowed"]
    assert app.bot.of("leave") == [] and app.bot.of("delete") == []
    assert app.bot.of("rich") == [] and app.bot.of("send") == []
    assert await auth_status(app, FRIEND_GROUP) == "approved"


async def test_cancel_refreshes_the_status_without_leaving(two_approved_one_pending):
    app = two_approved_one_pending
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, "status:refresh"))
    text, _ = only_replacement(app)
    assert text.startswith("**Dom's chats**") and "Book Club" in text
    assert app.bot.of("leave") == []
    assert await auth_status(app, FRIEND_GROUP) == "approved"


@pytest.mark.parametrize("error", [TelegramBadRequest, TelegramForbiddenError])
async def test_already_not_a_member_still_marks_the_chat_left(two_approved_one_pending, error):
    app = two_approved_one_pending
    app.bot.leave_error = not_a_member(error)
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"leaveyes:{FRIEND_GROUP}"))
    assert await auth_status(app, FRIEND_GROUP) == "left"
    assert app.bot.of("answer") == [("answer", "cb1", "Dom wasn't in Book Club anymore")]
    assert len(app.bot.of("rich")) == 1 and len(app.bot.of("delete")) == 1


async def test_any_other_error_changes_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    app.bot.leave_error = RuntimeError("telegram is down")
    app.bot.calls.clear()
    await handlers.handle_callback(app, callback(app, OWNER, f"leaveyes:{FRIEND_GROUP}"))
    assert await auth_status(app, FRIEND_GROUP) == "approved"
    assert app.bot.of("answer") == [("answer", "cb1", "Couldn't leave: telegram is down")]
    assert app.bot.of("rich") == [] and app.bot.of("delete") == []


async def test_leaving_only_applies_to_known_non_private_chats_and_inserts_nothing(two_approved_one_pending):
    app = two_approved_one_pending
    await app.store.set_chat_auth_status("-3003", "left")
    before = await chat_rows(app)
    app.bot.calls.clear()
    for data in (f"leave:{FRIEND}", f"leaveyes:{FRIEND}", f"leaveyes:{OWNER}", "leaveyes:-424242",
                 "leave:-424242", "leaveyes:-3003", "leaveyes:nope"):
        await handlers.handle_callback(app, callback(app, OWNER, data))
    assert [c[2] for c in app.bot.of("answer")] == ["unknown chat"] * 7
    assert app.bot.of("leave") == [] and app.bot.of("rich") == [] and app.bot.of("delete") == []
    assert await chat_rows(app) == before


# ---- /chats --------------------------------------------------------------------------------

async def chats_message(app):
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/chats", msg_id=901))
    (_, chat_id, text, kw), = app.bot.of("rich")
    assert chat_id == OWNER and app.bot.of("send") == []
    return text, kw["reply_markup"]


async def test_chats_groups_every_known_chat_by_status_with_member_counts(two_approved_one_pending):
    app = two_approved_one_pending
    await app.store.set_chat_auth_status("-3003", "ignored", title="Ignored Club")
    await app.store.set_chat_auth_status("-4004", "left", title="Old Club")
    app.bot.member_counts = {FRIEND_GROUP: 5, GROUP: TelegramBadRequest(
        method=LeaveChat(chat_id=1), message="Bad Request: chat not found")}
    slept = []

    async def record_sleep(seconds):
        slept.append(seconds)

    app.sleep = record_sleep
    text, markup = await chats_message(app)
    lines = text.splitlines()
    assert lines[0] == "**Dom's chats**"
    headings = [l for l in lines if l.startswith("**") and l.endswith("**") and l.strip("*") in ("Approved", "Pending", "Ignored", "Left")]
    assert headings == ["**Approved**", "**Pending**", "**Ignored**", "**Left**"]
    assert lines.count("| Chat | Type | Members |") == 4 and lines.count("|---|---|---|") == 4

    def section(name):
        start = lines.index(f"**{name}**") + 1
        end = next((i for i in range(start, len(lines)) if lines[i].strip("*") in ("Approved", "Pending", "Ignored", "Left")), len(lines))
        return rows_of("\n".join(lines[start:end]))

    assert sorted(section("Approved")) == [["Book Club", "Group", "5"], ["DM with Fran", "DM", "—"], ["your DM", "DM", "—"]]
    assert section("Pending") == [["The Group", "Group", "?"]]
    assert section("Ignored") == [["Ignored Club", "Group", "3"]]
    assert section("Left") == [["Old Club", "Group", "—"]]
    assert [c[1] for c in app.bot.of("count")] == [FRIEND_GROUP, GROUP, -3003]  # groups only, never left or DMs
    assert slept == [0.2, 0.2]
    assert buttons(markup) == [
        [("🚪 Leave Book Club", f"leave:{FRIEND_GROUP}")],
        [("🚪 Leave The Group", f"leave:{GROUP}")],
    ]


async def test_chats_is_owner_only_and_never_creates_rows(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/chats", msg_id=5))
    await handlers.handle_message(app, tg(chat_id=GROUP, sender=OWNER, text="/chats", msg_id=6))
    assert app.bot.of("send", FRIEND) == [] and app.bot.of("count") == []
    assert app.bot.of("rich") == [] and app.bot.of("send", FRIEND) == []
    before = await chat_rows(app)  # ordinary messages from others may create rows; the owner's /chats must not
    await chats_message(app)
    assert await chat_rows(app) == before


async def test_chats_looks_up_at_most_thirty_groups_and_truncates(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    for n in range(300):
        await approve(app, -5000 - n, title=f"Group number {n} " + "x" * 40)
    text, markup = await chats_message(app)
    assert len(app.bot.of("count")) == 30
    assert len(text) <= 4096
    import re
    more = re.search(r"…and (\d+) more", text)
    assert more
    shown = len(rows_of(text))
    assert shown + int(more.group(1)) == 301
    assert sum(len(row) for row in markup.inline_keyboard) <= 100


async def test_chats_titles_cannot_break_the_table(make_app):
    app = make_app()
    await say(app, OWNER, OWNER, "hi", 1)
    await approve(app, -5001, title="x | *y*")
    text, _ = await chats_message(app)
    assert "| x \\| \\*y\\* | Group | 3 |" in text.splitlines()


async def test_chats_falls_back_to_plain_text_when_rich_messages_are_refused(two_approved_one_pending, caplog):
    app = two_approved_one_pending
    app.bot.rich_error = TelegramBadRequest(method=SendRichMessage(chat_id=1, rich_message=InputRichMessage(markdown="x")),
                                            message="Bad Request: method not found")
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/chats", msg_id=901))
    (_, chat_id, text, kw), = app.bot.of("send")
    assert chat_id == OWNER and text.startswith("Dom's chats\n") and "• Book Club · 3 members" in text
    assert buttons(kw["reply_markup"])[0] == [("🚪 Leave Book Club", f"leave:{FRIEND_GROUP}")]
    assert sum("rich" in r.getMessage().lower() for r in caplog.records) == 1

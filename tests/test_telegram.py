import asyncio
import json
from types import SimpleNamespace  # noqa: F401

import httpx
import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    PhotoSize,
    User,
)
from tghelpers import BOT_USER, FRIEND, GROUP, NOW, OWNER, FakeBot, no_sleep, tg

from dom.actions import Actions, Bubble
from dom.config import Config, ModelEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.models import IncomingMessage
from dom.store import Store
from dom.telegram import executor, handlers
from dom.telegram.convert import to_incoming
from dom.telegram.gate import decide


async def approve(app, chat_id):
    await app.store.set_chat_auth_status(str(chat_id), "approved")


# ---- convert -------------------------------------------------------------------

def test_convert_caption_and_reply():
    original = tg(msg_id=7, text="the original")
    photo = tg(
        msg_id=8, text=None, caption="look at this", reply_to=original,
        photo=[PhotoSize(file_id="small", file_unique_id="u1", width=1, height=1),
               PhotoSize(file_id="big", file_unique_id="u2", width=2, height=2)],
    )
    inc = to_incoming(photo)
    assert isinstance(inc, IncomingMessage)
    assert (inc.chat_id, inc.chat_type, inc.chat_title, inc.msg_id) == (GROUP, "group", "The Group", 8)
    assert (inc.sender_id, inc.sender_handle, inc.sender_name, inc.is_bot) == (FRIEND, "@fran", "Fran", False)
    assert inc.text == "look at this"
    assert inc.reply_to == 7
    assert inc.media == {"type": "photo", "file_id": "big", "file_unique_id": "u2"}
    assert inc.date == NOW
    plain = to_incoming(tg(text="just text"))
    assert plain.media is None and plain.reply_to is None


# ---- gate + handlers -------------------------------------------------------------

async def test_pending_chat_prompts_owner_once(make_app):
    app = make_app()
    for i in range(1, 6):
        await handlers.handle_message(app, tg(msg_id=i, text=f"hi {i}"))
    prompts = app.bot.of("send", OWNER)
    assert len(prompts) == 1
    buttons = prompts[0][3]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == [f"approve:{GROUP}", f"ignore:{GROUP}"]
    assert "The Group" in prompts[0][2]
    assert app.script.requests == []
    assert app.bot.of("send", GROUP) == []
    assert len(await app.store.get_recent_chat_window(GROUP)) == 5  # recorded, never answered
    assert (await app.store.get_chat_auth_status(GROUP))["approval_prompt_sent"] is True


async def test_pending_friend_dm_prompts_owner_once(make_app):
    app = make_app()
    await handlers.handle_message(app, tg(chat_id=FRIEND, text="hi"))
    await handlers.handle_message(app, tg(chat_id=FRIEND, text="hello?", msg_id=2))
    assert len(app.bot.of("send", OWNER)) == 1
    assert app.script.requests == []


async def test_ignored_and_unowned_chats_are_dropped(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(str(GROUP), "ignored")
    await handlers.handle_message(app, tg())
    assert app.bot.calls == [] and app.script.requests == []
    assert await app.store.get_recent_chat_window(GROUP) == []
    unowned = make_app(owner_id=None)
    await handlers.handle_message(unowned, tg(chat_id=FRIEND))
    assert unowned.bot.calls == [] and unowned.script.requests == []
    assert await decide(unowned.store, to_incoming(tg()), None) == "drop"


async def test_decide_by_status(make_app):
    app = make_app()
    inc = to_incoming(tg())
    assert await decide(app.store, inc, OWNER) == "hold"
    await approve(app, GROUP)
    assert await decide(app.store, inc, OWNER) == "process"
    await app.store.set_chat_auth_status(str(GROUP), "blocked")
    assert await decide(app.store, inc, OWNER) == "drop"


async def test_owner_dm_and_approved_friend_dm_always_run_a_turn(make_app):
    app = make_app()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi dom"))
    assert len(app.script.requests) == 1
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["hey"]
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, text="yo"))
    assert len(app.script.requests) == 2
    assert [c[2] for c in app.bot.of("send", FRIEND)] == ["hey"]
    window = await app.store.get_recent_chat_window(FRIEND)
    assert [m.role for m in window] == ["user", "assistant"]
    # the conversation is in the model request
    assert "yo" in app.script.requests[1]["messages"][-1]["content"]


async def test_group_turn_only_on_mention_or_reply(make_app):
    app = make_app()
    await approve(app, GROUP)
    dom_message = tg(sender=999, text="earlier dom line", msg_id=50)
    dom_message = dom_message.model_copy(update={"from_user": BOT_USER})
    quiet = [
        tg(msg_id=1, text="hello all"),
        tg(msg_id=2, text="@dombotty not me"),
        tg(msg_id=3, text="mail me at a@dombot.com"),
        tg(msg_id=4, text="/help@otherbot"),
        tg(msg_id=5, text="reply to a human", reply_to=tg(msg_id=40, text="x")),
    ]
    for m in quiet:
        await handlers.handle_message(app, m)
    assert app.script.requests == []
    loud = [
        tg(msg_id=10, text="hey @DomBot what do you think"),
        tg(msg_id=11, text="replying to dom", reply_to=dom_message),
        tg(msg_id=12, text="/help"),
        tg(msg_id=13, text="/help@dombot"),
    ]
    for m in loud:
        await handlers.handle_message(app, m)
    assert len(app.script.requests) == 4
    assert len(app.bot.of("send", GROUP)) == 4
    assert len(await app.store.get_recent_chat_window(GROUP, 100)) == len(quiet) + len(loud) + 4


async def test_owner_in_group_never_leaks_private_files(make_app, tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "NOTES.md").write_text("OWNER-ONLY-SECRET")
    app = make_app()
    await approve(app, GROUP)
    await handlers.handle_message(app, tg(sender=OWNER, text="@dombot what do you know about me?"))
    assert len(app.script.requests) == 1
    assert "OWNER-ONLY-SECRET" not in json.dumps(app.script.requests[0])
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi", msg_id=2))
    assert "OWNER-ONLY-SECRET" in json.dumps(app.script.requests[1])


async def test_callback_from_non_owner_ignored(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())
    prompt = tg(chat_id=OWNER, sender=999, text="prompt", msg_id=60)

    def cb(sender, data):
        return CallbackQuery(
            id="cb1", from_user=User(id=sender, is_bot=False, first_name="X"),
            chat_instance="ci", data=data, message=prompt,
        )

    await handlers.handle_callback(app, cb(FRIEND, f"approve:{GROUP}"))
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "pending_approval"
    assert app.bot.of("edit") == []
    await handlers.handle_callback(app, cb(OWNER, "approve:not-a-number"))
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "pending_approval"
    await handlers.handle_callback(app, cb(OWNER, f"approve:{GROUP}"))
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "approved"
    assert len(app.bot.of("edit")) == 1
    await handlers.handle_callback(app, cb(OWNER, f"ignore:{GROUP}"))  # already decided: no change
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "approved"


async def test_bot_added_to_group_creates_pending_chat_and_prompts_once(make_app):
    app = make_app()

    def update(new):
        return ChatMemberUpdated(
            chat=Chat(id=GROUP, type="supergroup", title="New Group"),
            from_user=User(id=FRIEND, is_bot=False, first_name="Fran", username="fran"),
            date=NOW,
            old_chat_member=ChatMemberLeft(user=BOT_USER),
            new_chat_member=ChatMemberMember(user=BOT_USER) if new else ChatMemberLeft(user=BOT_USER),
        )

    await handlers.handle_my_chat_member(app, update(new=True))
    await handlers.handle_my_chat_member(app, update(new=True))
    status = await app.store.get_chat_auth_status(GROUP)
    assert status["authorization_status"] == "pending_approval"
    assert (status["title"], status["invited_by_handle"]) == ("New Group", "@fran")
    prompts = app.bot.of("send", OWNER)
    assert len(prompts) == 1 and "New Group" in prompts[0][2]
    other = make_app()
    await handlers.handle_my_chat_member(other, update(new=False))
    assert other.bot.calls == []


async def test_edited_message_updates_store(make_app):
    app = make_app()
    await approve(app, GROUP)
    await handlers.handle_message(app, tg(text="first draft"))
    edited = tg(text="second draft").model_copy(update={"edit_date": 1})
    await handlers.handle_edited_message(app, edited)
    window = await app.store.get_recent_chat_window(GROUP)
    assert window[0].text == "second draft" and window[0].is_edited


async def test_model_failure_does_not_crash_handler(make_app):
    app = make_app()

    def boom(request):
        return httpx.Response(401)

    app.client.http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi"))
    assert app.bot.of("send") == []


# ---- executor ----------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path, owner_id=OWNER)
    yield s
    s.close()


async def test_executor_typing_scales_with_length(store):
    bot, sleeps = FakeBot(), []

    async def sleep(s):
        sleeps.append(s)

    actions = Actions(bubbles=[Bubble(text="a" * 16), Bubble(text="b" * 400, reply_to=9)])
    ids = await executor.perform(bot, store, GROUP, 9, actions, typing_cps=8.0, sleep=sleep)
    assert sleeps == [2.0, 8]
    assert [c[0] for c in bot.calls] == ["action", "send", "action", "send"]
    assert bot.calls[0][2] == "typing"
    assert bot.calls[1][3].get("reply_parameters") is None
    assert bot.calls[3][3]["reply_parameters"].message_id == 9
    assert ids == [5001, 5002]
    window = await store.get_recent_chat_window(GROUP)
    assert [(m.msg_id, m.role, m.is_bot) for m in window] == [(5001, "assistant", True), (5002, "assistant", True)]


async def test_executor_schedules_delete_after(store):
    bot, sleeps = FakeBot(), []

    async def sleep(s):
        sleeps.append(s)

    ids = await executor.perform(
        bot, store, GROUP, 9, Actions(bubbles=[Bubble(text="oops", delete_after=5), Bubble(text="keep")]), sleep=sleep,
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert bot.of("delete") == [("delete", GROUP, ids[0])]
    assert 5 in sleeps
    assert [m.text for m in await store.get_recent_chat_window(GROUP)] == ["keep"]


async def test_executor_react_and_no_response(store):
    bot = FakeBot()
    await executor.perform(bot, store, GROUP, 9, Actions(react="🔥"), sleep=no_sleep)
    (call,) = bot.of("react")
    assert call[2] == 9 and call[3][0].emoji == "🔥"
    assert bot.of("send") == []
    quiet = FakeBot()
    ids = await executor.perform(quiet, store, GROUP, 9, Actions(no_response=True, react="🔥", bubbles=[Bubble(text="x")]), sleep=no_sleep)
    assert ids == [] and quiet.calls == []


# ---- stale approval decisions -----------------------------------------------------

async def test_turn_rechecks_authorization_after_waiting_for_lock(make_app):
    app = make_app()
    await approve(app, GROUP)
    async with app.locks[GROUP]:
        task = asyncio.create_task(handlers.handle_message(app, tg(text="@dombot hello")))
        await asyncio.sleep(0.05)  # decided "process", now waiting for the turn lock
        await app.store.set_chat_auth_status(str(GROUP), "ignored")
    await task
    assert app.script.requests == []
    assert app.bot.of("send", GROUP) == []


async def test_callback_does_not_override_an_already_decided_chat(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())
    prompt = tg(chat_id=OWNER, sender=999, text="prompt", msg_id=60)

    def cb(data):
        return CallbackQuery(
            id="cb1", from_user=User(id=OWNER, is_bot=False, first_name="X"),
            chat_instance="ci", data=data, message=prompt,
        )

    await handlers.handle_callback(app, cb(f"approve:{GROUP}"))
    await handlers.handle_callback(app, cb(f"ignore:{GROUP}"))  # stale second tap
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "approved"
    assert app.bot.of("answer")[-1][2] == "already handled"
    assert len(app.bot.of("edit")) == 1

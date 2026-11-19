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
from tghelpers import BOT_USER, FRIEND, GROUP, NOW, OWNER, FakeBot, drain_dispatch, no_sleep, tg

from dom.actions import Actions, Bubble
from dom.config import Config, ModelEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.models import IncomingMessage
from dom.presence import parse_habits
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


async def go_online(app, chat_id):
    await app.dispatch.presence.go_online(chat_id, parse_habits(""))


async def test_online_owner_and_approved_friend_dm_run_a_turn(make_app):
    app = make_app()
    await go_online(app, OWNER)
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi dom"))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["hey"]
    await approve(app, FRIEND)
    await go_online(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, text="yo"))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 2
    assert [c[2] for c in app.bot.of("send", FRIEND)] == ["hey"]
    window = await app.store.get_recent_chat_window(FRIEND)
    assert [m.role for m in window] == ["user", "assistant"]
    assert "yo" in app.script.requests[1]["messages"][-1]["content"]


async def test_online_group_considers_messages_without_mentions(make_app):
    app = make_app()
    await approve(app, GROUP)
    await go_online(app, GROUP)
    await handlers.handle_message(app, tg(text="hello all"))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == len(app.bot.of("send", GROUP)) == 1
    assert "hello all" in app.script.requests[0]["messages"][-1]["content"]


async def test_owner_in_group_never_leaks_private_files(make_app, tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "NOTES.md").write_text("OWNER-ONLY-SECRET")
    app = make_app()
    await approve(app, GROUP)
    await go_online(app, GROUP)
    await handlers.handle_message(app, tg(sender=OWNER, text="@dombot what do you know about me?"))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1
    assert "OWNER-ONLY-SECRET" not in json.dumps(app.script.requests[0])
    await go_online(app, OWNER)
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi", msg_id=2))
    await drain_dispatch(app.dispatch)
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

    await go_online(app, OWNER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        app.client.http = http
        await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hi"))
        await drain_dispatch(app.dispatch)
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
    await go_online(app, GROUP)
    async with app.locks[GROUP]:
        await handlers.handle_message(app, tg(text="@dombot hello"))
        await asyncio.sleep(0)  # recorded and dispatched, now waiting for the turn lock
        await app.store.set_chat_auth_status(str(GROUP), "ignored")
    await drain_dispatch(app.dispatch)
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


# ---- approval delivery -----------------------------------------------------------

def _method_error(cls):
    from aiogram.methods import SendMessage
    return cls(SendMessage(chat_id=OWNER, text="x"), "boom")


async def _pending_group(app):
    await handlers.handle_message(app, tg())  # records, prompts the owner
    return app


async def test_prompt_stays_reserved_on_network_failure(make_app):
    from aiogram.exceptions import TelegramNetworkError
    app = make_app()

    async def failing(chat_id, text, **kw):
        raise _method_error(TelegramNetworkError)

    app.bot.send_message = failing
    await handlers.handle_message(app, tg())
    assert (await app.store.get_chat_auth_status(GROUP))["approval_prompt_sent"] is True


@pytest.mark.parametrize("name", ["TelegramBadRequest", "TelegramForbiddenError"])
async def test_prompt_released_on_definite_failure(make_app, name):
    from aiogram import exceptions
    app = make_app()

    async def failing(chat_id, text, **kw):
        raise _method_error(getattr(exceptions, name))

    app.bot.send_message = failing
    await handlers.handle_message(app, tg())
    assert (await app.store.get_chat_auth_status(GROUP))["approval_prompt_sent"] is False


async def test_failed_prompt_never_overwrites_a_decision(make_app):
    from aiogram.exceptions import TelegramBadRequest
    app = make_app()

    async def decided_then_failing(chat_id, text, **kw):
        await app.store.set_chat_auth_status(str(GROUP), "approved")  # owner decided meanwhile
        raise _method_error(TelegramBadRequest)

    app.bot.send_message = decided_then_failing
    await handlers.handle_message(app, tg())
    assert (await app.store.get_chat_auth_status(GROUP))["authorization_status"] == "approved"


async def test_request_approval_records_metadata_only(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())
    chat = await app.store.get_chat_auth_status(GROUP)
    assert chat["authorization_status"] == "pending_approval"
    assert chat["title"] == "The Group" and chat["invited_by_handle"] == "@fran"


async def test_pending_command_resends_prompts_for_pending_chats(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())                      # pending group, prompt sent
    await handlers.handle_message(app, tg(chat_id=FRIEND))        # pending friend DM, prompt sent
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=-2002, sender=333, text="hey"))  # another pending group
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/pending", msg_id=9))
    prompts = app.bot.of("send", OWNER)
    callbacks = sorted(b.callback_data for p in prompts for b in p[3]["reply_markup"].inline_keyboard[0])
    assert callbacks == sorted([f"approve:{GROUP}", f"ignore:{GROUP}", "approve:-2002", "ignore:-2002"])
    assert app.script.requests == []  # a command, not a model turn


async def test_pending_command_is_owner_only(make_app):
    app = make_app()
    await handlers.handle_message(app, tg())
    app.bot.calls.clear()
    await approve(app, FRIEND)
    await handlers.handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/pending", msg_id=3))
    assert app.bot.of("send", OWNER) == []


# ---- edit ordering ----------------------------------------------------------------

async def test_edit_racing_the_original_is_not_lost(make_app):
    app = make_app()
    await approve(app, GROUP)
    real_record = app.store.record_new_message

    async def slow_record(msg):
        await asyncio.sleep(0.1)
        return await real_record(msg)

    app.store.record_new_message = slow_record
    original = asyncio.create_task(handlers.handle_message(app, tg(text="first draft")))
    await asyncio.sleep(0.02)  # the original is mid-flight
    edited = tg(text="second draft").model_copy(update={"edit_date": 1})
    await handlers.handle_edited_message(app, edited)
    await original
    window = await app.store.get_recent_chat_window(GROUP)
    assert [(m.text, m.is_edited) for m in window] == [("second draft", True)]


async def test_edit_of_unseen_message_is_recorded_as_new(make_app):
    app = make_app()
    await approve(app, GROUP)
    edited = tg(text="only the edited text", msg_id=9).model_copy(update={"edit_date": 1})
    await handlers.handle_edited_message(app, edited)
    window = await app.store.get_recent_chat_window(GROUP)
    assert [(m.msg_id, m.text) for m in window] == [(9, "only the edited text")]
    assert app.script.requests == []  # recording only, no turn


async def test_edit_in_unapproved_chat_is_not_recorded(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(str(GROUP), "ignored")
    await handlers.handle_edited_message(app, tg(text="x", msg_id=9).model_copy(update={"edit_date": 1}))
    assert await app.store.get_recent_chat_window(GROUP) == []


# ---- anonymous admins --------------------------------------------------------------

def anonymous_admin(text="@dombot hi", msg_id=1):
    sender_chat = Chat(id=GROUP, type="supergroup", title="The Group")
    anon = User(id=888, is_bot=True, first_name="Test Group Proxy", username="test_group_proxy_bot")
    return tg(text=text, msg_id=msg_id).model_copy(update={"from_user": anon, "sender_chat": sender_chat})


def test_convert_anonymous_admin_uses_sender_chat():
    inc = to_incoming(anonymous_admin())
    assert (inc.sender_id, inc.sender_name, inc.is_bot, inc.sender_handle) == (GROUP, "The Group", False, None)


async def test_anonymous_admin_message_is_not_dropped_as_bot(make_app):
    app = make_app()
    await approve(app, GROUP)
    await go_online(app, GROUP)
    await handlers.handle_message(app, anonymous_admin())
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1
    assert [m.sender_user_id for m in await app.store.get_recent_chat_window(GROUP)][0] == GROUP


# ---- forum topics ------------------------------------------------------------------

def topic_message(text="@dombot hi", thread=77):
    return tg(text=text).model_copy(update={"message_thread_id": thread, "is_topic_message": True})


def test_convert_carries_topic_thread_id():
    assert to_incoming(topic_message()).thread_id == 77
    assert to_incoming(tg()).thread_id is None
    # a plain reply thread in a non-forum group is not a topic: sending into it would fail
    assert to_incoming(tg().model_copy(update={"message_thread_id": 5})).thread_id is None


async def test_executor_sends_typing_and_bubbles_into_the_topic(store):
    bot = FakeBot()
    await executor.perform(bot, store, GROUP, 9, Actions(bubbles=[Bubble(text="hi")]), sleep=no_sleep, thread_id=77)
    assert bot.of("action")[0][3]["message_thread_id"] == 77
    assert bot.of("send")[0][3]["message_thread_id"] == 77


async def test_turn_replies_in_the_incoming_topic(make_app):
    app = make_app()
    await approve(app, GROUP)
    await go_online(app, GROUP)
    await handlers.handle_message(app, topic_message())
    await drain_dispatch(app.dispatch)
    assert app.bot.of("send", GROUP)[0][3]["message_thread_id"] == 77
    assert app.bot.of("action", GROUP)[0][3]["message_thread_id"] == 77

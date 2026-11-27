import asyncio

import pytest
from aiogram.types import User

from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from tghelpers import BOT_USER, GROUP, drain_dispatch, tg, turn_requests

EXIT_NOTE = "Leave this bot conversation now, in character, in one short message."
OTHER_BOT = User(id=333, first_name="Test Bot", is_bot=True)
ANOTHER_BOT = User(id=444, first_name="Second Test Bot", is_bot=True)


@pytest.fixture
def bot_chat(make_app):
    async def setup():
        app = make_app()
        await app.store.set_chat_auth_status(GROUP, "approved")
        await app.dispatch.presence.go_online(GROUP, parse_habits(""))
        return app, app.dispatch.now

    return setup


async def bot_line(app, clock, msg_id, text, user=OTHER_BOT, chat_id=GROUP):
    message = tg(chat_id=chat_id, msg_id=msg_id, text=text).model_copy(update={
        "from_user": user, "date": clock(),
    })
    await handle_message(app, message)
    await drain_dispatch(app.dispatch)


async def test_bot_loop_stops_after_depth(bot_chat):
    app, clock = await bot_chat()
    for msg_id in range(6001, 6009):  # above the fake bot's own ids (it numbers its sends from 5000)
        await bot_line(app, clock, msg_id, f"bot message {msg_id}")
        clock.advance(seconds=11)
    contents = [request["messages"][-1]["content"] for request in turn_requests(app)]
    assert len(contents) == len(app.bot.of("send", GROUP)) == 7
    assert all(EXIT_NOTE not in content for content in contents[:6])
    assert EXIT_NOTE in contents[-1]
    assert await app.store.get_bot_depth(GROUP) == 7
    window = await app.store.get_recent_chat_window(GROUP)
    assert [row.msg_id for row in window if row.role == "user" and row.is_bot] == list(range(6001, 6009))


async def test_human_message_resets_depth(bot_chat):
    app, clock = await bot_chat()
    for msg_id in range(1, 8):
        await bot_line(app, clock, msg_id, f"bot message {msg_id}")
        clock.advance(seconds=11)
    await handle_message(app, tg(msg_id=8, text="a human here").model_copy(update={"date": clock()}))
    await drain_dispatch(app.dispatch)
    assert await app.store.get_bot_depth(GROUP) == 0
    await bot_line(app, clock, 9, "another topic")
    assert await app.store.get_bot_depth(GROUP) == 1
    assert len(turn_requests(app)) == 9
    assert EXIT_NOTE not in turn_requests(app)[-1]["messages"][-1]["content"]


async def test_bot_rate_limit(bot_chat):
    app, clock = await bot_chat()
    await bot_line(app, clock, 1, "first")
    clock.advance(seconds=9)
    await bot_line(app, clock, 2, "too soon")
    await bot_line(app, clock, 3, "different sender", ANOTHER_BOT)
    assert len(app.script.requests) == 2
    clock.advance(seconds=1)
    await bot_line(app, clock, 4, "enough time passed")
    assert len(app.script.requests) == 3


async def test_bot_duplicate_ignored(bot_chat):
    app, clock = await bot_chat()
    await bot_line(app, clock, 1, "repeat")
    clock.advance(seconds=11)
    await bot_line(app, clock, 2, "repeat")
    assert len(app.script.requests) == 1
    clock.advance(seconds=60)
    await bot_line(app, clock, 3, "repeat")
    assert len(app.script.requests) == 2


async def test_own_bot_message_is_not_dispatched(bot_chat):
    app, clock = await bot_chat()
    await bot_line(app, clock, 1, "own message", BOT_USER)
    assert not app.script.requests
    assert not await app.store.get_recent_chat_window(GROUP)


async def test_human_question_is_not_swallowed_by_a_bot_message_in_the_same_debounce(bot_chat):
    app, clock = await bot_chat()
    await bot_line(app, clock, 1, "hello from a bot")
    assert len(app.script.requests) == 1
    clock.advance(seconds=3)
    gate = asyncio.Event()

    async def sleep(_):
        await gate.wait()

    app.dispatch.sleep = sleep
    await handle_message(app, tg(msg_id=2, text="a human question?").model_copy(update={"date": clock()}))
    clock.advance(seconds=1)
    message = tg(msg_id=3, text="bot again").model_copy(update={"from_user": OTHER_BOT, "date": clock()})
    await handle_message(app, message)
    gate.set()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 2
    assert "a human question?" in app.script.requests[-1]["messages"][-1]["content"]


async def test_bot_duplicate_check_is_scoped_to_the_chat(bot_chat):
    app, clock = await bot_chat()
    other = -2002
    await app.store.set_chat_auth_status(other, "approved")
    await app.dispatch.presence.go_online(other, parse_habits(""))
    await bot_line(app, clock, 1, "repeat")
    clock.advance(seconds=11)
    await bot_line(app, clock, 1, "repeat", chat_id=other)
    assert len(app.script.requests) == 2


async def test_bot_rate_limit_is_per_chat(bot_chat):
    app, clock = await bot_chat()
    other = -2002
    await app.store.set_chat_auth_status(other, "approved")
    await app.dispatch.presence.go_online(other, parse_habits(""))
    await bot_line(app, clock, 1, "first")
    clock.advance(seconds=2)
    await bot_line(app, clock, 1, "same bot, another chat", chat_id=other)
    assert len(app.script.requests) == 2
    clock.advance(seconds=1)
    await bot_line(app, clock, 2, "too soon", chat_id=other)
    assert len(app.script.requests) == 2


async def test_old_bot_reply_times_are_pruned(bot_chat):
    app, clock = await bot_chat()
    await bot_line(app, clock, 1, "first")
    clock.advance(seconds=61)
    await bot_line(app, clock, 2, "much later", ANOTHER_BOT)
    assert set(app.dispatch._bot_replies) == {(GROUP, ANOTHER_BOT.id)}

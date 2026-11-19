import asyncio
from datetime import timedelta

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from dom.dispatch import Dispatcher
from dom.jev import JevUnavailable
from dom.models import StoredMessage
from dom.presence import Presence, parse_habits
from dom.telegram.handlers import handle_message
from tghelpers import FRIEND, GROUP, OWNER, Clock, FixedRandom, drain_dispatch, no_sleep, tg


class FakeJev:
    def __init__(self, action="reply", confidence=1, kapoh=0, failure=None):
        self.action, self.confidence, self.kapoh = action, confidence, kapoh
        self.failure = failure
        self.calls = []

    async def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.failure:
            raise self.failure
        if "kapoh" in questions:
            return {"kapoh": {"type": "score", "score": self.kapoh}}
        return {
            "action": {"type": "choice", "choice": self.action, "confidence": self.confidence},
            "emoji": {"type": "choice", "choice": "👍"},
        }


@pytest.fixture
def setup_dispatch(make_app):
    async def setup(jev=None, chat_id=GROUP, sleep=no_sleep):
        app = make_app()
        clock = Clock()
        presence = Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock)
        dispatch = app.dispatch = Dispatcher(app, presence, jev, sleep, clock)
        await app.store.set_chat_auth_status(chat_id, "approved")
        return app, dispatch, clock

    return setup


async def deliver(app, msg_id=1, **kwargs):
    await handle_message(app, tg(msg_id=msg_id, **kwargs))
    await drain_dispatch(app.dispatch)


async def test_asleep_flood_no_calls_single_catchup(setup_dispatch):
    jev = FakeJev()
    app, dispatch, clock = await setup_dispatch(jev)
    clock.at = clock.at.replace(hour=2)
    for msg_id in range(1, 201):
        await handle_message(app, tg(msg_id=msg_id))
    await drain_dispatch(dispatch)
    await dispatch.tick()
    assert not jev.calls and not app.script.requests
    assert len(await app.store.get_recent_chat_window(GROUP, 200)) == 200
    clock.advance(hours=6)
    await dispatch.tick()
    await dispatch.tick()
    assert not jev.calls
    assert len(app.script.requests) == 1
    note = app.script.requests[0]["messages"][-1]["content"]
    assert "state: online" in note and "online minutes left: 15" in note
    assert "messages that arrived while away: 200" in note
    assert "you just came online" in note and "desktop: off" in note
    assert (await app.store.list_chats("approved"))[0]["last_read_msg_id"] == 200


async def test_offline_wakes_after_n_messages(setup_dispatch):
    jev = FakeJev(kapoh=3)
    app, _, _ = await setup_dispatch(jev, chat_id=FRIEND)
    for msg_id in range(1, 5):
        await deliver(app, msg_id, chat_id=FRIEND)
    assert not app.script.requests and not jev.calls
    await deliver(app, 5, chat_id=FRIEND)
    assert len(app.script.requests) == 1 and not jev.calls


async def test_offline_kapoh_wakes_group(setup_dispatch):
    jev = FakeJev(kapoh=2.0)
    app, dispatch, _ = await setup_dispatch(jev)
    await deliver(app)
    assert list(jev.calls[0][1]) == ["kapoh"]
    assert len(app.script.requests) == 1
    assert (await dispatch.presence.state(GROUP, parse_habits("")))[0] == "online"


async def test_online_ignore_confident_no_model_call(setup_dispatch):
    ready = asyncio.Event()

    async def sleep(_):
        await ready.wait()

    jev = FakeJev("ignore", confidence=0.70)
    app, dispatch, _ = await setup_dispatch(jev, sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id in range(1, 21):
        await handle_message(app, tg(msg_id=msg_id))
    ready.set()
    await drain_dispatch(dispatch)
    assert len(jev.calls) == 1
    assert jev.calls[0][0].count("<msg ") == 15
    assert 'id="6"' in jev.calls[0][0] and 'id="5"' not in jev.calls[0][0]
    assert not app.script.requests and not app.bot.calls


async def test_online_react_sets_reaction(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(FakeJev("react"))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app, 42)
    assert not app.script.requests
    _, chat_id, msg_id, reaction = app.bot.of("react")[0]
    assert (chat_id, msg_id, reaction[0].emoji) == (GROUP, 42, "👍")


async def test_low_confidence_goes_to_model(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(FakeJev("ignore", confidence=0.69))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    assert len(app.script.requests) == 1


@pytest.mark.parametrize("unavailable", [None, JevUnavailable("unavailable"), "refusal"])
async def test_jev_unavailable_falls_back(setup_dispatch, unavailable):
    jev = FakeJev(action=None) if unavailable == "refusal" else FakeJev(failure=unavailable)
    app, dispatch, _ = await setup_dispatch(jev)
    await deliver(app)
    assert not app.script.requests
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app, 2)
    assert len(app.script.requests) == 1


async def test_debounce_coalesces_burst(setup_dispatch):
    waits = []

    async def sleep(seconds):
        assert seconds == 4
        gate = asyncio.Event()
        waits.append(gate)
        await gate.wait()

    app, dispatch, _ = await setup_dispatch(sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id in range(1, 4):
        await handle_message(app, tg(msg_id=msg_id, text=f"line {msg_id}"))
        await asyncio.sleep(0)
    assert len(waits) == 3 and not app.script.requests
    waits[-1].set()
    await drain_dispatch(dispatch)
    assert len(app.script.requests) == 1
    assert "line 3" in app.script.requests[0]["messages"][-1]["content"]


async def test_owner_wake_command_only_in_owner_dm(setup_dispatch):
    app, dispatch, clock = await setup_dispatch()
    clock.at = clock.at.replace(hour=2)
    await deliver(app, sender=OWNER, text="/wake")
    assert (await dispatch.presence.state(GROUP, parse_habits("")))[0] == "asleep"
    await deliver(app, chat_id=OWNER, sender=OWNER, text=f"/wake {GROUP}")
    assert (await dispatch.presence.state(GROUP, parse_habits("")))[0] == "online"
    assert len(app.script.requests) == 1
    await deliver(app, 2, chat_id=OWNER, sender=OWNER, text=f"/sleep {GROUP}")
    assert (await dispatch.presence.state(GROUP, parse_habits("")))[0] == "asleep"
    await deliver(app, 3, chat_id=OWNER, sender=OWNER, text="/wake")
    assert (await dispatch.presence.state(OWNER, parse_habits("")))[0] == "online"
    await deliver(app, 4, chat_id=OWNER, sender=OWNER, text=f"/wake {FRIEND}")
    assert (await dispatch.presence.state(FRIEND, parse_habits("")))[0] == "asleep"


async def test_extend_online_tool(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    habits = parse_habits("")
    until = await dispatch.presence.go_online(GROUP, habits, minutes=5)
    calls = []

    def model(request):
        calls.append(request)
        if len(calls) == 1:
            message = {"role": "assistant", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "extend_online", "arguments": '{"minutes": 30}'},
            }]}
        else:
            message = {"role": "assistant", "content": '{"no_response": true}'}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        app.client.http = http
        await deliver(app)
    assert len(calls) == 2
    assert (await dispatch.presence.state(GROUP, habits))[1] == until.replace(hour=12, minute=35)


async def test_expired_offline_session_is_caught_up_on_tick(setup_dispatch):
    app, dispatch, clock = await setup_dispatch()
    habits = parse_habits("")
    await dispatch.presence.go_online(GROUP, habits, minutes=5)
    clock.advance(minutes=5)
    await dispatch.tick()
    assert (await dispatch.presence.state(GROUP, habits))[0] == "offline"
    await deliver(app)
    assert not app.script.requests
    clock.advance(hours=2)
    await dispatch.tick()
    await dispatch.tick()
    assert len(app.script.requests) == 1


@pytest.fixture
def gated_sleep():
    waits = []

    async def sleep(seconds):
        gate = asyncio.Event()
        waits.append((seconds, gate))
        await gate.wait()

    return sleep, waits


async def test_debounce_stops_restarting_after_fifteen_seconds(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id in range(1, 5):
        await handle_message(app, tg(msg_id=msg_id, text=f"line {msg_id}"))
        await asyncio.sleep(0)
        clock.advance(seconds=5)
    assert len(waits) == 3  # the fourth message arrived at the 15 s cap and did not restart the timer
    waits[-1][1].set()
    await drain_dispatch(dispatch)
    assert len(app.script.requests) == 1
    assert "line 4" in app.script.requests[0]["messages"][-1]["content"]


async def test_debounce_delay_is_shortened_to_the_cap(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await handle_message(app, tg(msg_id=1))
    await asyncio.sleep(0)
    clock.advance(seconds=13)
    await handle_message(app, tg(msg_id=2))
    await asyncio.sleep(0)
    assert [seconds for seconds, _ in waits] == [4, 2]
    waits[-1][1].set()
    await drain_dispatch(dispatch)


async def test_background_poll_does_not_skip_a_chat_with_an_overdue_debounce(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    await handle_message(app, tg())
    await asyncio.sleep(0)
    await dispatch.tick()
    assert not app.script.requests  # the debounce is still within its cap
    clock.advance(seconds=20)
    await dispatch.tick()
    assert len(app.script.requests) == 1
    waits[-1][1].set()
    await drain_dispatch(dispatch)


def send_fails(app, *errors):
    """Make the next sends raise each error in turn (None lets a send through), then keep raising the last."""
    sends, send = [], app.bot.send_message

    async def failing(*args, **kwargs):
        error = errors[min(len(sends), len(errors) - 1)]
        sends.append(error)
        if error is not None:
            raise error
        return await send(*args, **kwargs)

    app.bot.send_message = failing
    return sends


def telegram_error(kind, message):
    return kind(method=SendMessage(chat_id=GROUP, text="x"), message=message)


@pytest.mark.parametrize("error", [
    telegram_error(TelegramForbiddenError, "Forbidden: bot was kicked from the group chat"),
    telegram_error(TelegramBadRequest, "Bad Request: chat not found"),
])
async def test_unreachable_chat_is_marked_read(setup_dispatch, error, caplog):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    sends = send_fails(app, error)
    await deliver(app)
    assert len(sends) == 1
    assert (await app.store.get_unread(GROUP))[0] == 0
    assert "giving up" in caplog.text


async def test_other_bad_requests_are_retried_like_any_failure(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    send_fails(app, telegram_error(TelegramBadRequest, "Bad Request: message is too long"))
    await deliver(app)
    assert (await app.store.get_unread(GROUP))[0] == 1


async def test_chat_is_marked_read_after_three_consecutive_failures(setup_dispatch, caplog):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    sends = send_fails(app, RuntimeError("network down"))
    for msg_id in (1, 2):
        await deliver(app, msg_id)
        assert (await app.store.get_unread(GROUP))[0] == msg_id
    await deliver(app, 3)
    assert (await app.store.get_unread(GROUP))[0] == 0
    assert len(sends) == 3 and "giving up" in caplog.text
    await deliver(app, 4)
    assert (await app.store.get_unread(GROUP))[0] == 1  # the count starts again


async def test_a_successful_turn_resets_the_failure_count(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    oops = RuntimeError("network down")
    send_fails(app, oops, oops, None, oops, oops)
    for msg_id in range(1, 6):
        await deliver(app, msg_id)
    assert (await app.store.get_unread(GROUP))[0] == 2


async def test_online_chat_with_an_orphaned_unread_message_is_answered_on_tick(setup_dispatch):
    app, dispatch, clock = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    # Recorded before a restart: its debounce died with the process.
    await app.store.record_new_message(StoredMessage(
        chat_id=str(GROUP), msg_id=1, sender_user_id=FRIEND, text="did you see this?",
        visibility_scope=app.store.visibility_for(GROUP), created_at=(clock.at - timedelta(seconds=5)).isoformat(),
    ))
    await dispatch.tick()
    assert not app.script.requests  # too fresh: a live debounce would normally own it
    clock.advance(seconds=6)
    await dispatch.tick()
    assert len(app.script.requests) == 1
    await dispatch.tick()
    assert len(app.script.requests) == 1


@pytest.mark.parametrize("command", ["/wake", "/sleep"])
async def test_owner_command_for_an_unknown_chat_creates_nothing(setup_dispatch, command):
    app, _, _ = await setup_dispatch()
    await deliver(app, chat_id=OWNER, sender=OWNER, text=f"{command} 424242")
    assert app.bot.of("send", OWNER)[-1][2] == "unknown chat"
    assert "424242" not in [chat["chat_id"] for chat in await app.store.list_chats()]
    assert not app.script.requests


@pytest.mark.parametrize("command", ["/wake", "/sleep"])
async def test_owner_command_for_a_chat_that_is_not_approved_is_unknown(setup_dispatch, command):
    app, dispatch, _ = await setup_dispatch()
    await app.store.set_chat_auth_status(FRIEND, "pending_approval")
    await deliver(app, chat_id=OWNER, sender=OWNER, text=f"{command} {FRIEND}")
    assert app.bot.of("send", OWNER)[-1][2] == "unknown chat"
    assert (await dispatch.presence.state(FRIEND, parse_habits("")))[0] == "offline"
    assert not app.script.requests

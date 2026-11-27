import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from aiogram.types import PhotoSize
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from dom import turn_log
from dom.actions import Actions, Bubble
from dom.config import PresenceConfig
from dom.dispatch import NO_ANSWER_NOTICE, Arrival, Dispatcher
from dom.jev import JevClient, JevUnavailable
from dom.models import StoredMessage
from dom.telegram import executor
from dom.presence import Presence, parse_habits, read_habits
from dom.telegram.handlers import handle_message, to_incoming
from dom.usage import ModelCallSkipped
from tghelpers import FRIEND, GROUP, OWNER, Clock, FixedRandom, drain_dispatch, no_sleep, tg, turn_requests


class FakeJev:
    def __init__(self, action="reply", confidence=1, kapoh=0, failure=None, urgency=None, done=None, long=False):
        self.action, self.confidence, self.kapoh = action, confidence, kapoh
        self.failure, self.urgency = failure, urgency
        self.done, self.long = done, long
        self.calls = []

    async def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.failure:
            raise self.failure
        if "urgency" in questions:
            return {"urgency": {"type": "score", "score": self.urgency} if self.urgency is not None else None}
        if "kapoh" in questions:
            return {"kapoh": {"type": "score", "score": self.kapoh}}
        return {
            "action": {"type": "choice", "choice": self.action, "confidence": self.confidence},
            "emoji": {"type": "choice", "choice": "👍"},
            "done": {"type": "choice", "choice": "yes", "confidence": self.done} if self.done is not None else None,
            "long": {"type": "choice", "choice": "yes", "confidence": 1} if self.long else None,
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


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_migration_reserves_both_turn_slots_and_clears_the_old_chat(setup_dispatch, monkeypatch, cancel_caller):
    old, new = GROUP, -10666
    app, dispatch, clock = await setup_dispatch(chat_id=old)
    release_old, release_new, release_migration = (asyncio.Event() for _ in range(3))
    saving, old_entered, new_entered = (asyncio.Event() for _ in range(3))
    stopped = asyncio.Event()

    class Typing:
        stop = stopped.set

    msg = to_incoming(tg())
    dispatch._arrivals[old] = {msg.msg_id: Arrival(msg, typing=Typing())}
    dispatch._latest[old] = msg
    dispatch._first_notify[old] = clock()
    debounce = dispatch._debounces[old] = dispatch._spawn(asyncio.Event().wait())
    dispatch._interrupted[old] = {None: []}
    dispatch._followups[old] = {None: [msg.msg_id]}
    dispatch._trim_seq[old] = 2
    dispatch._stay_marks[old] = msg.msg_id
    dispatch._stay_pending[old] = None
    stay = dispatch._stay_running[old] = dispatch._spawn(asyncio.Event().wait())
    dispatch._group_no_answer_at[old] = clock()
    dispatch.note_admin(old, True)
    dispatch.prompt_cache.snapshot(app.home, "group", old, [], clock())

    real_migrate = app.store.migrate_chat

    async def save(*args, **kwargs):
        saving.set()
        await release_migration.wait()
        return await real_migrate(*args, **kwargs)

    async def turn(chat_id, entered, release):
        async with dispatch._dispatch_slot(chat_id, wait_media=False):
            entered.set()
            await release.wait()

    monkeypatch.setattr(app.store, "migrate_chat", save)
    turns = [asyncio.create_task(turn(old, old_entered, release_old)),
             asyncio.create_task(turn(new, new_entered, release_new))]
    await asyncio.wait_for(asyncio.gather(old_entered.wait(), new_entered.wait()), timeout=2)
    migration = asyncio.create_task(dispatch.migrate_chat(old, new))
    try:
        await asyncio.sleep(0)
        assert not saving.is_set()
        release_new.set()
        await turns[1]
        assert not saving.is_set()
        release_old.set()
        await asyncio.wait_for(saving.wait(), timeout=2)

        if cancel_caller:
            migration.cancel()
            await asyncio.sleep(0)
        old_entered.clear()
        new_entered.clear()
        turns.extend([asyncio.create_task(turn(old, old_entered, release_old)),
                      asyncio.create_task(turn(new, new_entered, release_new))])
        await asyncio.sleep(0)
        assert not old_entered.is_set() and not new_entered.is_set()
    finally:
        release_old.set()
        release_new.set()
        release_migration.set()
        await asyncio.wait_for(asyncio.gather(migration, *turns, return_exceptions=True), timeout=2)

    if cancel_caller:
        assert migration.cancelled()
    else:
        assert migration.result() == "migrated"
    assert await app.store.resolve_chat_id(old) == str(new)
    assert stopped.is_set() and debounce.cancelled() and stay.cancelled()
    for state in (dispatch._arrivals, dispatch._latest, dispatch._first_notify, dispatch._debounces,
                  dispatch._interrupted, dispatch._followups, dispatch._trim_seq, dispatch._stay_marks,
                  dispatch._stay_pending, dispatch._stay_running, dispatch._group_no_answer_at, dispatch._admin):
        assert old not in state
    assert (old, "group") not in dispatch.prompt_cache._notes
    assert dispatch._admin[new] is True


async def test_migration_flushes_pending_coding_notice_to_new_chat_id(setup_dispatch):
    from dom.dispatch import TurnAttempt

    old, new = GROUP, -10667
    app, dispatch, clock = await setup_dispatch(chat_id=old)
    interrupted = TurnAttempt(
        asyncio.current_task(), 1, turn_log.TurnTrace(), clock(), notices=["checked progress"],
    )
    dispatch._interrupted[old] = {None: [interrupted]}
    assert await dispatch.migrate_chat(old, new) == "migrated"
    await drain_dispatch(dispatch)
    assert app.bot.of("send", old) == []
    assert [c[2] for c in app.bot.of("send", new)] == ["🔎 Checked your coding work: checked progress"]


async def test_close_bounds_coding_notice_flood_wait(setup_dispatch):
    from aiogram.exceptions import TelegramRetryAfter

    app, dispatch, _ = await setup_dispatch(chat_id=OWNER)
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def long_flood(chat_id, text, **kw):
        raise TelegramRetryAfter(
            method=SendMessage(chat_id=chat_id, text=text),
            message="Flood control exceeded. Retry in 60 seconds",
            retry_after=60,
        )

    app.sleep = record_sleep
    app.bot.send_message = long_flood
    dispatch._spawn(dispatch._send_coding_notice(OWNER, ["checked progress"]), committed=True)
    await asyncio.wait_for(dispatch.close(), timeout=2)
    assert slept == []


def test_protect_send_is_public(setup_dispatch):
    assert hasattr(Dispatcher, "protect_send")
    assert not hasattr(Dispatcher, "_protect_send")


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
    assert len(turn_requests(app)) == 1
    note = turn_requests(app)[0]["messages"][-1]["content"]
    assert "state: online" in note and "availability: you're around for a bit longer" in note
    assert "online minutes left" not in note
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


@pytest.mark.parametrize(("hour", "score", "wakes"), [
    (12, 0, False),   # offline, casual
    (12, 1.5, True),  # offline, needs an answer
    (2, 2, False),    # asleep, task can wait
    (2, 2.5, True),   # asleep, urgent enough to wake
    (2, 3, True),     # asleep, emergency
    (2, None, False), # refused
])
async def test_owner_urgency_wakes_only_above_the_state_threshold(setup_dispatch, hour, score, wakes):
    jev = FakeJev(urgency=score)
    app, dispatch, clock = await setup_dispatch(jev, chat_id=OWNER)
    clock.at = clock.at.replace(hour=hour)
    await deliver(app, chat_id=OWNER, sender=OWNER, text="Please check this.")

    (state, questions), = jev.calls
    assert "Please check this." in state
    assert list(questions) == ["urgency"]
    assert questions["urgency"]["type"] == "score"
    assert questions["urgency"]["criteria"] == [
        "casual chat, can wait", "would like an answer soon",
        "needs Dom now: a task, a deadline, a problem", "emergency",
    ]
    assert len(turn_requests(app)) == int(wakes)
    assert (await app.store.get_unread(OWNER))[0] == int(not wakes)
    presence, until = await dispatch.presence.state(OWNER, parse_habits(""))
    assert presence == ("online" if wakes else "asleep" if hour == 2 else "offline")
    if hour == 2 and wakes:
        assert until == clock() + timedelta(minutes=15)
    if hour == 2 and not wakes:
        await dispatch.tick()
        assert len(jev.calls) == 1


async def test_owner_urgency_is_not_asked_when_the_message_count_already_wakes_dom(setup_dispatch):
    jev = FakeJev(urgency=0)
    app, _, _ = await setup_dispatch(jev, chat_id=OWNER)
    for msg_id in range(1, 6):
        await deliver(app, msg_id, chat_id=OWNER, sender=OWNER)
    assert len(jev.calls) == 4
    assert len(turn_requests(app)) == 1


async def test_jev_down_does_not_wake_owner_early_but_count_rule_still_works(setup_dispatch):
    jev = FakeJev(failure=JevUnavailable("unavailable"))
    app, dispatch, _ = await setup_dispatch(jev, chat_id=OWNER)
    for msg_id in range(1, 5):
        await deliver(app, msg_id, chat_id=OWNER, sender=OWNER)
    assert len(jev.calls) == 4 and not turn_requests(app)
    assert (await dispatch.presence.state(OWNER, parse_habits("")))[0] == "offline"

    await deliver(app, 5, chat_id=OWNER, sender=OWNER)
    assert len(jev.calls) == 4  # the count rule already wakes Dom: no urgency call
    assert len(turn_requests(app)) == 1
    assert (await app.store.get_unread(OWNER))[0] == 0


@pytest.mark.parametrize("hour", [2, 12])
async def test_friend_dm_never_runs_urgency_triage(setup_dispatch, hour):
    jev = FakeJev(urgency=3)
    app, _, clock = await setup_dispatch(jev, chat_id=FRIEND)
    clock.at = clock.at.replace(hour=hour)
    await deliver(app, chat_id=FRIEND)
    assert jev.calls == [] and turn_requests(app) == []


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
    assert jev.calls[-1][0].count("<msg ") == 15
    assert 'id="6"' in jev.calls[-1][0] and 'id="5"' not in jev.calls[-1][0]
    assert not app.script.requests
    assert not app.bot.calls  # a group gets no seen reaction


async def test_online_react_sets_reaction(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(FakeJev("react"))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app, 42)
    assert not app.script.requests
    (_, chat_id, msg_id, reaction), = app.bot.of("react")  # the chosen reaction only, no seen reaction on top
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


async def test_debounce_coalesces_burst(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, _ = await setup_dispatch(sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id in range(1, 4):
        await handle_message(app, tg(msg_id=msg_id, text=f"line {msg_id}"))
        await wait_for_sleeps(waits, msg_id)
    assert [seconds for seconds, _ in waits] == [3, 3, 3]
    assert not app.script.requests
    waits[-1][1].set()
    await drain_dispatch(dispatch)
    assert len(app.script.requests) == 1
    assert "line 3" in app.script.requests[0]["messages"][-1]["content"]


async def test_a_reply_that_promises_an_action_no_tool_backs_is_put_back_once(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    promise = json.dumps({"bubbles": [{"text": "fine, i'll add it in again"}]})
    app.script.contents[:] = [promise, promise]
    await deliver(app)
    assert len(app.script.requests) == 2  # put back once, then the second answer is sent as it stands
    assert "no tool was called" in app.script.requests[1]["messages"][-1]["content"]
    assert app.script.requests[1]["tools"]


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


async def test_reply_records_debounce_lock_triage_guard_typing_and_each_send(setup_dispatch, monkeypatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    start = clock()
    elapsed = lambda: (clock() - start).total_seconds()
    monkeypatch.setattr("dom.dispatch.monotonic", elapsed)
    monkeypatch.setattr("dom.turn_log.monotonic", elapsed)
    app.config = app.config.model_copy(update={"leak_check": True})
    app.script.contents[:] = [json.dumps({"bubbles": [{"text": "one"}, {"text": "and two"}]})]

    def jev_reply(request):
        questions = json.loads(request.content)["questions"]
        if "leak" in questions:
            clock.advance(milliseconds=350)
            answers = {"leak": {"type": "score", "score": 0}}
        else:
            clock.advance(milliseconds=250)
            answers = {"action": {"type": "choice", "choice": "reply", "confidence": 1}}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 1}})

    async def type_for(seconds):
        clock.advance(seconds=seconds)

    send = app.bot.send_message

    async def send_message(*args, **kwargs):
        clock.advance(milliseconds=100)
        return await send(*args, **kwargs)

    app.sleep = type_for
    monkeypatch.setattr(app.bot, "send_message", send_message)
    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_reply)) as http:
        dispatch.jev = JevClient(http, "test", "test-jev", "http://localhost")
        await dispatch.presence.go_online(GROUP, parse_habits(""))
        async with app.locks[GROUP]:
            await handle_message(app, tg().model_copy(update={"date": start}))
            await dispatch._arrivals[GROUP][1].task
            await wait_for_sleeps(waits, 1)
            clock.advance(seconds=1)
            await handle_message(app, tg(msg_id=2).model_copy(update={"date": clock()}))
            await dispatch._arrivals[GROUP][2].task
            await wait_for_sleeps(waits, 2)
            clock.advance(seconds=3)
            waits[-1][1].set()
            await asyncio.sleep(0)
            clock.advance(seconds=2)
        await drain_dispatch(dispatch)

    [row] = await app.store.recent_turns()
    timings = [item for item in json.loads(row["trace"]) if "@" in item]
    assert timings == [
        "debounce@3250", "lock_wait@2000", "model@0", "guard@350", "send@100", "typing@1000", "send@100",
    ]
    assert (row["first_bubble_ms"], row["last_bubble_ms"]) == (5700, 6800)
    calls = json.loads(row["model_calls"])
    assert [(call["kind"], call["duration_ms"]) for call in calls] == [
        ("triage", 250), ("triage", 250), ("chat", 0), ("guard", 350),
    ]
    assert calls[0]["input_tokens"] == 10 and calls[-1]["cached_tokens"] is None


async def test_catch_up_records_unavailable_time_for_its_own_chat(setup_dispatch):
    app, dispatch, clock = await setup_dispatch()
    clock.at = clock.at.replace(hour=2)
    await dispatch.presence.wake(OWNER, parse_habits(""))
    await handle_message(app, tg().model_copy(update={"date": clock()}))
    await drain_dispatch(dispatch)
    assert not turn_requests(app)

    hours_away = 6
    clock.advance(hours=hours_away)
    await dispatch.tick()
    [row] = await app.store.recent_turns(str(GROUP))
    away_ms = hours_away * 60 * 60 * 1000
    assert f"away_wait@{away_ms}" in json.loads(row["trace"])
    assert row["first_bubble_ms"] == row["last_bubble_ms"] == away_ms


@pytest.mark.parametrize(("age", "late"), [(59, False), (60, False), (61, True)])
async def test_turn_note_is_qualitative_and_only_added_after_sixty_seconds(setup_dispatch, age, late):
    app, dispatch, clock = await setup_dispatch()
    await dispatch.presence.wake(GROUP, parse_habits(""))
    message = tg().model_copy(update={"date": clock() - timedelta(seconds=age)})
    await handle_message(app, message)
    await drain_dispatch(dispatch)
    [request] = turn_requests(app)
    prompt = request["messages"][-1]["content"]
    late_lines = [line for line in prompt.splitlines() if "you are answering late" in line]
    assert late_lines == ([
        "you are answering late. If it fits, you can give a reason from your life in your own words."
    ] if late else [])


@pytest.fixture
def gated_sleep():
    waits = []

    async def sleep(seconds):
        gate = asyncio.Event()
        waits.append((seconds, gate))
        await gate.wait()

    return sleep, waits


async def wait_for_sleeps(waits, count):
    async with asyncio.timeout(2):
        while len(waits) < count:
            await asyncio.sleep(0)


@pytest.mark.parametrize(("state", "expected_high"), [("online", 4), ("offline", 6), ("asleep", 6)])
async def test_sender_wait_uses_presence_range(setup_dispatch, gated_sleep, monkeypatch, state, expected_high):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    if state == "online":
        await dispatch.presence.go_online(GROUP, parse_habits(""))
    elif state == "asleep":
        clock.at = clock.at.replace(hour=2)
    draws = []

    def uniform(lo, hi):
        draws.append((lo, hi))
        return hi

    monkeypatch.setattr(dispatch.presence.rng, "uniform", uniform)
    await handle_message(app, tg())
    await wait_for_sleeps(waits, 1)
    assert draws == [(2, expected_high)]
    assert waits[0][0] == expected_high
    waits[0][1].set()
    await drain_dispatch(dispatch)


async def test_debounce_stops_restarting_after_fifteen_seconds(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id in range(1, 4):
        await handle_message(app, tg(msg_id=msg_id, text=f"line {msg_id}"))
        await wait_for_sleeps(waits, msg_id)
        clock.advance(seconds=5)
    timer = dispatch._debounces[GROUP]
    await handle_message(app, tg(msg_id=4, text="line 4"))
    assert dispatch._debounces[GROUP] is timer
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
    await wait_for_sleeps(waits, 1)
    clock.advance(seconds=13)
    await handle_message(app, tg(msg_id=2))
    await wait_for_sleeps(waits, 2)
    assert [seconds for seconds, _ in waits] == [3, 2]
    waits[-1][1].set()
    await drain_dispatch(dispatch)


@pytest.mark.parametrize("confidence", [0.69, 0.70])
async def test_sender_done_can_skip_the_debounce(setup_dispatch, gated_sleep, confidence):
    sleep, waits = gated_sleep
    jev = FakeJev(done=confidence)
    app, dispatch, clock = await setup_dispatch(jev, sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await handle_message(app, tg().model_copy(update={"date": clock()}))
    await dispatch._arrivals[GROUP][1].task

    if confidence < 0.70:
        await wait_for_sleeps(waits, 1)
        assert len(waits) == 1 and not turn_requests(app)
        assert app.bot.of("action", GROUP)  # typing starts during the debounce
        clock.advance(seconds=3)
        waits[0][1].set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert len(turn_requests(app)) == 1
    [row] = await app.store.recent_turns()
    trace = json.loads(row["trace"])
    assert f"Jev done=yes confidence={confidence}" in trace
    assert row["first_bubble_ms"] == (0 if confidence >= 0.70 else 3000)
    assert turn_log.visits_in(trace)[0]["reason"] == "next"
    assert not any(step.startswith("queue_wait@") for step in trace)
    timings = {item.partition("@")[0] for item in trace if "@" in item}
    # Waiting on Jev's verdict is triage time; only a debounce that really ran out is debounce time.
    assert ("triage" in timings, "debounce" in timings) == (confidence >= 0.70, confidence < 0.70)


async def test_incomplete_burst_uses_original_15_second_cap_even_with_jev_in_flight(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    blocked = asyncio.Event()

    class SlowJev(FakeJev):
        async def decide(self, state, questions):
            if 'id="4"' in state:
                await blocked.wait()
            return await super().decide(state, questions)

    app, dispatch, clock = await setup_dispatch(SlowJev(), sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    for msg_id, advance in enumerate([0, 4, 4, 5], start=1):
        clock.advance(seconds=advance)
        await handle_message(app, tg(msg_id=msg_id, text=f"fragment {msg_id}").model_copy(update={"date": clock()}))
        await wait_for_sleeps(waits, msg_id)
    assert [delay for delay, _ in waits] == [3, 3, 3, 2]
    timer = dispatch._debounces[GROUP]
    clock.advance(seconds=2)
    waits[-1][1].set()
    try:
        await asyncio.wait_for(timer, timeout=2)
        [request] = turn_requests(app)
        assert "fragment 4" in request["messages"][-1]["content"]
        [row] = await app.store.recent_turns()
        assert row["first_bubble_ms"] == 2000
    finally:
        blocked.set()
        await drain_dispatch(dispatch)


async def test_online_triage_slower_than_the_debounce_still_decides_the_turn(setup_dispatch, gated_sleep, monkeypatch):
    sleep, waits = gated_sleep
    release, parked = asyncio.Event(), asyncio.Event()

    class SlowJev(FakeJev):
        async def decide(self, state, questions):
            await release.wait()
            return await super().decide(state, questions)

    real_wait = asyncio.wait

    async def wait(tasks, **kwargs):  # the turn parks here until the arrival classification is in
        if list(tasks) == [dispatch._arrivals[GROUP][1].task]:
            parked.set()
        return await real_wait(tasks, **kwargs)

    app, dispatch, clock = await setup_dispatch(SlowJev(action="ignore", done=0.5), sleep=sleep)
    monkeypatch.setattr(asyncio, "wait", wait)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await handle_message(app, tg().model_copy(update={"date": clock()}))
    await wait_for_sleeps(waits, 1)
    waits[0][1].set()  # the debounce ends while Jev is still thinking
    await asyncio.wait_for(parked.wait(), timeout=5)
    assert not turn_requests(app)  # the turn waits on Jev instead of generating
    clock.advance(seconds=3)
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=5)

    assert not turn_requests(app) and not app.bot.of("send", GROUP)  # a confident "ignore" is honoured
    assert (await app.store.get_unread(GROUP))[0] == 0


async def test_new_message_cancels_generation_before_any_bubble(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    entered, cancelled = asyncio.Event(), asyncio.Event()
    complete = app.client.complete

    async def generate(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert not app.locks[GROUP].locked()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(app.client, "complete", generate)
    await handle_message(app, tg(text="first thought"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=2, text="and a detail"))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert cancelled.is_set() and not dispatch._slots
    [request] = turn_requests(app)
    note = request["messages"][-1]["content"]
    assert note.index("first thought") < note.index("and a detail")
    [bubble] = app.bot.of("send", GROUP)
    assert bubble[2] == "hey"
    [row] = await app.store.recent_turns()
    assert "Restarted before sending" in json.loads(row["trace"])
    assert (await app.store.get_unread(GROUP))[0] == 0
    assert [m.reply_to_msg_id for m in await app.store.get_recent_chat_window(GROUP) if m.role == "assistant"] == [2]


async def test_restarted_reply_keeps_the_earlier_attempts_stage_times(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    entered = asyncio.Event()
    complete = app.client.complete

    async def generate(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            with turn_log.model_call("slow-model", "chat", [{"role": "user", "content": "x"}], []):
                await asyncio.Future()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(app.client, "complete", generate)
    await handle_message(app, tg(text="first thought"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=2, text="and a detail"))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    [row] = await app.store.recent_turns()
    trace = json.loads(row["trace"])
    models = [call for call in json.loads(row["model_calls"]) if call["kind"] == "chat"]
    # One model stage per model call, so the stage bar and the call list agree.
    assert models[0]["model"] == "slow-model"
    assert len([item for item in trace if item.startswith("model@")]) == len(models) >= 1


@pytest.mark.parametrize(("reply_time", "first_pause"), [(0.5, [1.5]), (2.0, []), (3.0, [])])
async def test_first_bubble_uses_elapsed_reply_time_when_indicator_fails(
    setup_dispatch, monkeypatch, reply_time, first_pause,
):
    app, dispatch, clock = await setup_dispatch(chat_id=OWNER)
    await dispatch.presence.go_online(OWNER, parse_habits(""))
    app.script.contents[:] = [json.dumps({"bubbles": [{"text": "x" * 16}, {"text": "y" * 16}]})]
    started = clock()
    monkeypatch.setattr("dom.dispatch.monotonic", lambda: (clock() - started).total_seconds())
    complete = app.client.complete
    pauses = []

    async def finish_reply(*args, **kwargs):
        clock.advance(seconds=reply_time)
        return await complete(*args, **kwargs)

    async def fail_typing(*args, **kwargs):
        raise OSError("typing is temporarily unavailable")

    async def sleep(seconds):
        pauses.append((len(app.bot.of("send", OWNER)), seconds))
        clock.advance(seconds=seconds)

    monkeypatch.setattr(app.client, "complete", finish_reply)
    monkeypatch.setattr(app.bot, "send_chat_action", fail_typing)
    monkeypatch.setattr(app, "sleep", sleep)
    await deliver(app, chat_id=OWNER, sender=OWNER)

    assert pauses == [(0, seconds) for seconds in first_pause] + [(1, 2.0)]
    assert [call[2] for call in app.bot.of("send", OWNER)] == ["x" * 16, "y" * 16]


async def test_incoming_during_typing_pause_replaces_unsent_reply(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1), chat_id=OWNER)
    await dispatch.presence.go_online(OWNER, parse_habits(""))
    app.script.contents[:] = ['{"bubbles":[{"text":"obsolete"}]}', '{"bubbles":[{"text":"updated"}]}']
    typing, stopped = asyncio.Event(), asyncio.Event()

    async def sleep(_):
        if not typing.is_set():
            typing.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    monkeypatch.setattr(app, "sleep", sleep)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER))
    await asyncio.wait_for(typing.wait(), timeout=2)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, msg_id=2))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert stopped.is_set()
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["updated"]
    assert len(await app.store.recent_turns()) == 1
    assert not dispatch._active and not dispatch._arrivals


@pytest.mark.parametrize("in_flight", [False, True])
async def test_message_during_delivery_gets_a_linked_followup(setup_dispatch, monkeypatch, in_flight):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    app.script.contents[:] = [
        '{"bubbles":[{"text":"first bubble"},{"text":"second bubble","reply_to":1}]}',
        '{"bubbles":[{"text":"followup bubble"}]}',
    ]
    delivering, release = asyncio.Event(), asyncio.Event()
    if in_flight:
        send = app.bot.send_message

        async def hold_send(chat, text, **options):
            if text == "first bubble":
                delivering.set()
                await release.wait()
            return await send(chat, text, **options)

        monkeypatch.setattr(app.bot, "send_message", hold_send)
    else:
        async def hold_gap(seconds):
            if app.bot.of("send", GROUP):  # the first bubble is out: this is the pause before the next one
                delivering.set()
                await release.wait()

        monkeypatch.setattr(app, "sleep", hold_gap)

    await handle_message(app, tg(text="before Dom answered").model_copy(update={"date": clock()}))
    await asyncio.wait_for(delivering.wait(), timeout=2)
    # This timestamp predates the first message to prove the prompt follows arrival order.
    await handle_message(app, tg(msg_id=2, text="new information").model_copy(update={"date": clock() - timedelta(seconds=1)}))
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    before, after = (request["messages"][-1]["content"] for request in turn_requests(app))
    assert "new information" not in before
    assert "Messages with IDs 2 came in while you were answering" in after
    assert "Your earlier bubbles did not see them" in after
    assert after.index("before Dom answered") < after.index("new information") < after.index("second bubble")
    assert 'role="assistant" reply_to="1"' in after
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["first bubble", "second bubble", "followup bubble"]
    rows = await app.store.get_recent_chat_window(GROUP)
    assert [m.reply_to_msg_id for m in rows if m.role == "assistant"] == [1, 1, 2]
    assert (await app.store.get_unread(GROUP))[0] == 0
    assert len(await app.store.recent_turns()) == 2


def hold_first_generation(app, monkeypatch):
    """Make the first model call wait to be cancelled; later calls go through."""
    entered, cancelled = asyncio.Event(), asyncio.Event()
    complete = app.client.complete

    async def generate(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(app.client, "complete", generate)
    return entered, cancelled


class JudgesEachMessage(FakeJev):
    """Replies to the first message, ignores the second ("thanks!")."""

    async def decide(self, state, questions):
        self.action = "ignore" if 'id="2"' in state else "reply"
        self.done = 1 if 'id="2"' not in state else None
        return await super().decide(state, questions)


async def test_restart_keeps_an_answer_jev_already_chose_when_a_thank_you_arrives(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(JudgesEachMessage())
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    entered, _ = hold_first_generation(app, monkeypatch)
    await handle_message(app, tg(text="can you send me the address?"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=2, text="thanks!"))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    [bubble] = app.bot.of("send", GROUP)  # the question is answered once, not marked read unanswered
    assert bubble[2] == "hey"
    note = turn_requests(app)[-1]["messages"][-1]["content"]
    assert note.index("can you send me the address?") < note.index("thanks!")
    assert not app.bot.of("react")
    assert (await app.store.get_unread(GROUP))[0] == 0


async def test_a_second_restart_before_the_model_still_keeps_the_chosen_answer(setup_dispatch, monkeypatch):
    """Re-check of PR #12: a restart cancelled again before reaching the model used to forget the answer was chosen."""
    app, dispatch, _ = await setup_dispatch(JudgesEachMessage())
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    entered, _ = hold_first_generation(app, monkeypatch)
    window_calls, in_restart, release = [], asyncio.Event(), asyncio.Event()
    real_window = dispatch._window

    async def window(*args, **kwargs):
        window_calls.append(1)
        if len(window_calls) == 2:  # the first restart: hold it before the model so a third message lands
            in_restart.set()
            await release.wait()
        return await real_window(*args, **kwargs)

    monkeypatch.setattr(dispatch, "_window", window)
    await handle_message(app, tg(text="can you send me the address?"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=2, text="thanks!"))
    await asyncio.wait_for(in_restart.wait(), timeout=2)
    await handle_message(app, tg(msg_id=3, text="oh and sorry"))
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert app.bot.of("send", GROUP)  # still answered, not marked read in silence
    assert (await app.store.get_unread(GROUP))[0] == 0


@pytest.mark.parametrize("during_media", [False, True])
async def test_restart_of_a_catch_up_turn_stays_a_catch_up_turn(setup_dispatch, gated_sleep, monkeypatch, during_media):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(FakeJev(action="ignore"), sleep=sleep)
    if during_media:
        entered = asyncio.Event()

        async def describe_unread_media(chat_id):
            if not entered.is_set():
                entered.set()
                await asyncio.Future()

        monkeypatch.setattr(dispatch, "_describe_unread_media", describe_unread_media)
    else:
        entered, _ = hold_first_generation(app, monkeypatch)
    for msg_id in range(1, 6):  # the fifth unread message wakes Dom
        await handle_message(app, tg(msg_id=msg_id, text=f"while you were out {msg_id}").model_copy(update={"date": clock()}))
        await wait_for_sleeps(waits, msg_id)
    waits[-1][1].set()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=6, text="and one more").model_copy(update={"date": clock()}))
    await wait_for_sleeps(waits, 6)
    waits[-1][1].set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    [bubble] = app.bot.of("send", GROUP)  # online Jev would ignore "and one more"; the backlog is still answered
    assert bubble[2] == "hey"
    note = turn_requests(app)[-1]["messages"][-1]["content"]
    assert "messages that arrived while away: 5" in note and "you just came online" in note
    assert (await app.store.get_unread(GROUP))[0] == 0


@pytest.mark.parametrize("error", [RuntimeError, ModelCallSkipped, asyncio.CancelledError])
async def test_coding_notice_survives_a_failed_reply(setup_dispatch, monkeypatch, error):
    app, dispatch, _ = await setup_dispatch(chat_id=OWNER)
    await dispatch.presence.go_online(OWNER, parse_habits(""))

    async def compose(client, messages, tools, ctx, **kwargs):
        ctx.t3_notes.append("checked progress")
        raise error()

    monkeypatch.setattr("dom.dispatch.run_turn", compose)
    await deliver(app, chat_id=OWNER, sender=OWNER)

    assert [call[2] for call in app.bot.of("send", OWNER)] == ["🔎 Checked your coding work: checked progress"]


@pytest.mark.parametrize("finishes", [False, True])
async def test_restart_delivers_each_coding_notice_once(setup_dispatch, monkeypatch, finishes):
    app, dispatch, _ = await setup_dispatch(chat_id=OWNER)
    await dispatch.presence.go_online(OWNER, parse_habits(""))
    entered, successor = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()

    async def compose(client, messages, tools, ctx, **kwargs):
        if not entered.is_set():
            ctx.t3_notes.append("checked progress")
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if not finishes:
                    raise
                return Actions(no_response=True)
        successor.set()
        await release.wait()
        ctx.t3_notes.append("checked logs")
        return Actions(bubbles=[Bubble(text="hey")])

    monkeypatch.setattr("dom.dispatch.run_turn", compose)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, msg_id=2))
    await asyncio.wait_for(successor.wait(), timeout=2)
    already_sent = [call[2] for call in app.bot.of("send", OWNER)]
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    if finishes:
        assert already_sent == ["🔎 Checked your coding work: checked progress"]
        assert [call[2] for call in app.bot.of("send", OWNER)][1:] == ["hey", "🔎 Checked your coding work: checked logs"]
    else:
        assert not already_sent
        assert [call[2] for call in app.bot.of("send", OWNER)] == ["hey", "🔎 Checked your coding work: checked progress; checked logs"]


async def test_typing_already_shown_before_a_restart_still_counts(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    now = [100.0]
    monkeypatch.setattr("dom.dispatch.monotonic", lambda: now[0])
    entered, _ = hold_first_generation(app, monkeypatch)
    typed = []
    real_perform = executor.perform

    async def perform(*args, **kwargs):
        typed.append(kwargs["typed_already"])
        return await real_perform(*args, **kwargs)

    monkeypatch.setattr(executor, "perform", perform)
    await handle_message(app, tg(text="first thought"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    for _ in range(20):  # the indicator's first refresh
        await asyncio.sleep(0)
    now[0] += 3  # "typing…" showed for three seconds while Dom thought
    await handle_message(app, tg(msg_id=2, text="and a detail"))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert len(typed) == 1 and typed[0] >= 3


async def test_a_restart_dispatch_that_gives_up_leaves_no_stale_attempt_behind(setup_dispatch, gated_sleep, monkeypatch):
    sleep, waits = gated_sleep
    app, dispatch, _ = await setup_dispatch(JudgesEachMessage(), sleep=sleep)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    entered, cancelled = hold_first_generation(app, monkeypatch)
    await handle_message(app, tg(text="a thought"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await handle_message(app, tg(msg_id=2, text="another"))
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    while GROUP not in dispatch._interrupted or len(waits) < 2:  # the cancelled attempt is filed; msg 2's timer runs
        await asyncio.sleep(0)
    await app.store.set_chat_auth_status(GROUP, "ignored")  # the owner dropped the chat before the restart ran
    waits[-1][1].set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert GROUP not in dispatch._interrupted and not app.bot.of("send", GROUP)


async def test_arrival_triage_judges_a_photo_by_its_description(setup_dispatch):
    jev = FakeJev(action="reply", done=1)
    app, dispatch, _ = await setup_dispatch(jev)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    release = asyncio.Event()

    class Describer:
        async def describe(self, media, *, request):
            await release.wait()
            return "a street sign pointing to Elm Road"

    app.media_describer = Describer()
    photo = tg(text=None, photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)])
    await handle_message(app, photo)
    for _ in range(50):
        await asyncio.sleep(0)
    assert not jev.calls  # Jev waits for the description instead of reading "[photo]"
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert jev.calls and "Elm Road" in jev.calls[0][0]


async def test_restart_cap_finishes_the_third_attempt_and_queues_a_followup(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    attempts, release = asyncio.Queue(), asyncio.Event()
    complete = app.client.complete
    cancelled = []

    async def generate(messages, *args, **kwargs):
        note = messages[-1]["content"]
        await attempts.put(note)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(note)
            raise
        return await complete(messages, *args, **kwargs)

    monkeypatch.setattr(app.client, "complete", generate)
    for msg_id in (1, 2, 3):
        await handle_message(app, tg(msg_id=msg_id, text=f"message {msg_id}"))
        assert f"message {msg_id}" in await asyncio.wait_for(attempts.get(), timeout=2)
    await handle_message(app, tg(msg_id=4, text="message 4"))
    assert len(cancelled) == 2
    release.set()
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    first, followup = (request["messages"][-1]["content"] for request in turn_requests(app))
    assert "message 3" in first and "message 4" not in first
    assert "message 4" in followup and "Messages with IDs 4 came in while you were answering" in followup
    assert len(app.bot.of("send", GROUP)) == 2
    assert not (dispatch._active or dispatch._interrupted or dispatch._followups)
    rows = await app.store.recent_turns(str(GROUP))
    reasons_by_turn = [[visit["reason"] for visit in turn_log.visits_in(json.loads(row["trace"]))] for row in rows]
    assert sorted(reasons_by_turn) == [["next"], ["next", "resume", "resume"]]



async def test_only_long_owner_dm_requests_get_a_rotating_ack(setup_dispatch):
    jev = FakeJev(done=1, long=True)
    app, dispatch, clock = await setup_dispatch(jev, chat_id=OWNER)
    await app.store.set_chat_auth_status(GROUP, "approved")
    for chat in (OWNER, GROUP):
        await dispatch.presence.go_online(chat, parse_habits(""))
    await deliver(app, 1, chat_id=OWNER, sender=OWNER)
    await deliver(app, 2, chat_id=OWNER, sender=OWNER)
    clock.advance(minutes=1)
    await deliver(app, 3, chat_id=OWNER, sender=OWNER)
    jev.long = False
    clock.advance(minutes=1)
    await deliver(app, 4, chat_id=OWNER, sender=OWNER)
    jev.long = True
    await deliver(app, 1, chat_id=GROUP)
    assert [c[2] for c in app.bot.of("send", OWNER)] == ["On it.", "hey", "hey", "Give me a moment.", "hey", "hey"]
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["hey"]


async def test_background_poll_does_not_skip_a_chat_with_an_overdue_debounce(setup_dispatch, gated_sleep):
    sleep, waits = gated_sleep
    app, dispatch, clock = await setup_dispatch(sleep=sleep)
    await handle_message(app, tg())
    await wait_for_sleeps(waits, 1)
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


@pytest.mark.parametrize("message", [
    "Bad Request: not enough rights to send text messages to the chat", "Bad Request: TOPIC_CLOSED",
    "Bad Request: message thread not found",
])
async def test_a_chat_dom_may_not_write_in_is_given_up_at_once_and_the_owner_told(setup_dispatch, message):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    sends = send_fails(app, telegram_error(TelegramBadRequest, message))
    await deliver(app)
    assert len(sends) == 1 and (await app.store.get_unread(GROUP))[0] == 0
    events = [row.text for row in await app.store.get_recent_chat_window(OWNER) if row.role == "event"]
    assert any("Gave up on the unread messages" in text and "won't let me write there" in text for text in events)


async def test_giving_up_in_the_owners_dm_tells_him(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(OWNER, parse_habits(""))
    sends = send_fails(app, RuntimeError("network down"), RuntimeError("network down"), RuntimeError("network down"), None)
    for msg_id in (1, 2, 3):
        await deliver(app, msg_id, chat_id=OWNER, sender=OWNER)
    assert (await app.store.get_unread(OWNER))[0] == 0
    assert len(sends) == 4  # three failed turns, then the notice
    assert "Couldn't answer your last messages\nmy turns kept failing (RuntimeError)." in app.bot.of("send", OWNER)[-1][2]


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


async def test_decide_logs_a_failing_gate_instead_of_raising(setup_dispatch, caplog):
    app, dispatch, _ = await setup_dispatch()

    async def broken(chat_id):
        raise RuntimeError("database is locked")

    app.store.get_chat_auth_status = broken
    await dispatch._decide(GROUP)
    assert "database is locked" in caplog.text
    await dispatch.tick()  # the background poll path must not leak a task exception either
    assert all(task.exception() is None for task in list(dispatch._tasks) if task.done())


async def test_decide_logs_a_failing_media_wait_instead_of_raising(setup_dispatch, caplog):
    app, dispatch, _ = await setup_dispatch()

    async def broken(chat_id):
        raise RuntimeError("media wait broke")

    dispatch._wait_media = broken
    await dispatch._decide(GROUP)
    assert "media wait broke" in caplog.text


async def test_media_check_failure_does_not_stop_the_message_being_dispatched(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, parse_habits(""))

    async def broken(chat_id, habits):
        raise RuntimeError("presence lookup failed")

    dispatch.presence.is_online = broken
    photo = tg(text=None, photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)])
    app.media_describer = object()
    await handle_message(app, photo)
    await drain_dispatch(dispatch)
    assert len(app.script.requests) == 1


# ---- a turn that ends in silence ---------------------------------------------------------------

QUIET = json.dumps({"no_response": True})


def seen(app):
    return [(call[2], call[3][0].emoji) for call in app.bot.of("react")]


async def online_with(setup_dispatch, *replies, chat_id=GROUP, jev=None, sleep=no_sleep):
    app, dispatch, clock = await setup_dispatch(jev, chat_id=chat_id, sleep=sleep)
    app.script.contents[:] = list(replies)
    await dispatch.presence.go_online(chat_id, parse_habits(""))
    return app, dispatch, clock


async def test_online_turn_that_stays_quiet_marks_the_newest_message_seen(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, QUIET, chat_id=FRIEND)
    await deliver(app, 42, chat_id=FRIEND, sender=FRIEND)
    assert seen(app) == [(42, "👀")]
    assert not app.bot.of("send")


async def test_triage_ignore_marks_the_newest_message_seen_without_a_model_call(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, jev=FakeJev("ignore", confidence=0.9), chat_id=FRIEND)
    await deliver(app, 7, chat_id=FRIEND, sender=FRIEND)
    assert seen(app) == [(7, "👀")]
    assert not app.script.requests


async def test_a_reply_needs_no_seen_reaction(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, chat_id=FRIEND)  # the scripted model answers "hey"
    await deliver(app, 3, chat_id=FRIEND, sender=FRIEND)
    assert [call[2] for call in app.bot.of("send")] == ["hey"] and not app.bot.of("react")


async def test_a_chosen_reaction_needs_no_seen_reaction(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, json.dumps({"react": "🔥"}), chat_id=FRIEND)
    await deliver(app, 3, chat_id=FRIEND, sender=FRIEND)
    assert seen(app) == [(3, "🔥")]


async def test_an_unsupported_chosen_reaction_leaves_the_message_seen_not_unanswered(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, json.dumps({"react": "😏"}), chat_id=FRIEND)
    await deliver(app, 3, chat_id=FRIEND, sender=FRIEND)
    assert seen(app) == [(3, "👀")]


async def test_offline_and_asleep_chats_get_no_seen_reaction(setup_dispatch):
    app, _, clock = await setup_dispatch(chat_id=FRIEND)
    await deliver(app, 1, chat_id=FRIEND, sender=FRIEND)  # offline
    clock.at = clock.at.replace(hour=2)
    await deliver(app, 2, chat_id=FRIEND, sender=FRIEND)  # asleep
    assert not app.bot.calls and not app.script.requests


async def test_the_switch_turns_the_seen_reaction_off(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, QUIET, chat_id=FRIEND)
    app.config = app.config.model_copy(update={"presence": PresenceConfig(seen_reaction=False)})
    await deliver(app, 3, chat_id=FRIEND, sender=FRIEND)
    assert [call for call in app.bot.calls if call[0] != "action"] == []  # only "typing" while he thought


async def test_a_batch_gets_one_seen_reaction_on_its_newest_message(setup_dispatch):
    ready = asyncio.Event()

    async def sleep(_):
        await ready.wait()

    app, _, _ = await online_with(setup_dispatch, QUIET, sleep=sleep, chat_id=FRIEND)
    for msg_id in range(1, 6):
        await handle_message(app, tg(chat_id=FRIEND, msg_id=msg_id))
    ready.set()
    await drain_dispatch(app.dispatch)
    assert seen(app) == [(5, "👀")]


async def test_a_chat_that_rejects_reactions_is_not_asked_again(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, QUIET, QUIET, chat_id=FRIEND)
    attempts = []

    async def refuse(chat_id, message_id, reaction=None, **kw):
        attempts.append(message_id)
        raise telegram_error(TelegramBadRequest, "Bad Request: REACTION_INVALID")

    app.bot.set_message_reaction = refuse
    await deliver(app, 1, chat_id=FRIEND, sender=FRIEND)
    await deliver(app, 2, chat_id=FRIEND, sender=FRIEND)
    assert attempts == [1]
    assert len(turn_requests(app)) == 2  # both turns still ran


async def test_a_quiet_group_turn_gets_no_seen_reaction(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, QUIET)  # read receipts are a DM thing; 👀 on every group lull is noise
    await deliver(app, 42)
    assert not app.bot.of("react") and not app.bot.of("send")


async def test_a_transient_reaction_error_does_not_turn_reactions_off(setup_dispatch):
    app, _, _ = await online_with(setup_dispatch, QUIET, QUIET, chat_id=FRIEND)
    attempts = []

    async def flaky(chat_id, message_id, reaction=None, **kw):
        attempts.append(message_id)
        if len(attempts) == 1:
            raise telegram_error(TelegramNetworkError, "connection reset")

    app.bot.set_message_reaction = flaky
    await deliver(app, 1, chat_id=FRIEND, sender=FRIEND)
    await deliver(app, 2, chat_id=FRIEND, sender=FRIEND)
    assert attempts == [1, 2]


async def test_a_message_from_another_bot_is_not_marked_seen(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    other_bot = StoredMessage(
        chat_id=str(GROUP), msg_id=9, is_bot=True, sender_user_id=777, visibility_scope=f"chat:{GROUP}",
        text="beep", created_at="2026-01-01T12:00:00+00:00",
    )
    await dispatch._mark_seen(GROUP, other_bot)
    assert not app.bot.calls


# ---- a reply that could not be produced is never silent ----------------------------------------

async def test_the_owner_is_told_when_no_model_could_answer(setup_dispatch, caplog):
    app, _, _ = await online_with(setup_dispatch, "prose", "more prose", chat_id=OWNER)
    with caplog.at_level("WARNING", logger="dom.agent"):
        await deliver(app, 5, chat_id=OWNER, sender=OWNER)
    assert [call[2] for call in app.bot.of("send", OWNER)] == [NO_ANSWER_NOTICE]
    assert not app.bot.of("react")
    assert "no usable final answer" in caplog.text and "[malformed malformed]" in caplog.text


async def test_a_friend_gets_an_in_character_line_and_owner_gets_an_event(setup_dispatch):
    from dom.dispatch import FRIEND_NO_ANSWER_LINE

    app, _, _ = await online_with(setup_dispatch, "prose", "more prose", chat_id=FRIEND)
    await deliver(app, 5, chat_id=FRIEND, sender=FRIEND)
    assert [call[2] for call in app.bot.of("send", FRIEND)] == [FRIEND_NO_ANSWER_LINE]
    assert not app.bot.of("react")
    events = [row.text for row in await app.store.get_recent_chat_window(OWNER) if row.role == "event"]
    assert any("no usable final answer" in text for text in events)


async def test_a_proactive_turn_that_cannot_answer_stays_quiet(setup_dispatch):
    app, dispatch, _ = await online_with(setup_dispatch, "prose", "more prose", chat_id=OWNER)
    until = dispatch.now() + timedelta(minutes=5)
    actions = await dispatch._compose_turn(OWNER, "owner_dm", None, None, [], until, 0, False, read_habits(app.home))
    assert actions == Actions(no_response=True)


async def test_provider_budget_notice_is_owner_only_deduplicated_and_visible_in_status(setup_dispatch, browser, caplog):
    from dom.config import ModelEndpoint
    from dom.llm import ChatClient, FallbackClient
    from dom.provider_budget import ProviderBudgetNotices
    from zoneinfo import ZoneInfo

    app, dispatch, clock = await setup_dispatch(FakeJev(), chat_id=FRIEND)
    app.config = app.config.model_copy(update={"timezone": "Asia/Singapore"})
    await app.store.set_chat_auth_status(GROUP, "approved")
    for chat in (FRIEND, GROUP, OWNER):
        await dispatch.presence.go_online(chat, parse_habits(""))
    endpoint = ModelEndpoint(base_url="https://openrouter.ai/api/v1", model="example/model", api_key_env="MODEL_KEY")
    app.config.models["chat"] = endpoint
    key = "fictional-provider-credential"
    tz = ZoneInfo(app.config.timezone)
    reset = clock().replace(hour=0) + timedelta(days=1)
    recovered = False

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {key}"
        if recovered:
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"bubbles":[{"text":"back"}]}'}}]})
        return httpx.Response(403, json={"error": {
            "message": f"Workspace daily budget of $0.50 exceeded. {key}",
            "metadata": {"limit_source": "openrouter_workspace_budget", "scope": "workspace",
                         "limit_usd": 0.50, "spend_usd": 0.505, "resets_at": reset.isoformat()},
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser(app) as dashboard:
        app.client = FallbackClient(ChatClient(endpoint, key, http, sleep=no_sleep), None)
        await deliver(app, chat_id=FRIEND)
        await deliver(app, chat_id=GROUP)
        assert not app.bot.of("send", FRIEND) and not app.bot.of("send", GROUP)
        assert len(app.bot.of("send", OWNER)) == 1
        for msg_id in (1, 2):
            await deliver(app, msg_id, chat_id=OWNER, sender=OWNER)
        # Dedup survives a process restart.
        dispatch.provider_budget = ProviderBudgetNotices(app.store)
        await deliver(app, 3, chat_id=OWNER, sender=OWNER)
        (notice,) = app.bot.of("send", OWNER)
        line = notice[2]
        assert "OpenRouter's workspace budget ($0.50)" in line
        assert f"Resets {reset.astimezone(tz):%d %b at %H:%M}" in line
        home = await dashboard.get("/api/home")
        assert home.json()["provider_budget"] == line.replace("\n", " ")
        await deliver(app, 4, chat_id=OWNER, sender=OWNER, text="/status")
        assert "workspace budget" in app.bot.of("send", OWNER)[-1][2]
        clock.at = reset + timedelta(minutes=1)
        assert (await dashboard.get("/api/home")).json()["provider_budget"] is None
        reset += timedelta(days=1)
        await dispatch.presence.wake(OWNER, parse_habits(""))
        await deliver(app, 5, chat_id=OWNER, sender=OWNER)
        assert len([call for call in app.bot.of("send", OWNER) if call[2].startswith("OpenRouter")]) == 2
        recovered = True
        await deliver(app, 6, chat_id=OWNER, sender=OWNER)
        assert app.bot.of("send", OWNER)[-1][2] == "back"
        assert (await dashboard.get("/api/home")).json()["provider_budget"] is None
        assert key not in caplog.text + home.text + (await app.store.kv_get("chat_provider_budget"))

import asyncio
import json
from datetime import timedelta

import pytest

from dom import turn_log
from dom.attention import Attention
from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from dom.web.pages import _turns, render
from test_dispatch import FakeJev, setup_dispatch  # shared dispatcher fixture
from tghelpers import FRIEND, GROUP, OWNER, Clock, drain_dispatch, tg


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


async def order(loop, clock, entries):
    picked = []
    async def visit(chat, scope, tier, age, resume):
        async with loop.visit(chat, scope, tier, clock() - timedelta(seconds=age), resume=resume) as entry:
            picked.append((chat, entry.reason, entry.capped))
    async with loop.visit(99, "friend_dm", "next", clock()):
        tasks = [asyncio.create_task(visit(*entry)) for entry in entries]
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    return picked


@pytest.mark.parametrize(("entries", "expected"), [
    ([(GROUP, "group", "next", 90, False), (OWNER, "owner_dm", "next", 0, False)], [OWNER, GROUP]),
    ([(OWNER, "owner_dm", "next", 90, False), (GROUP, "group", "later", 0, True)], [GROUP, OWNER]),
    ([(GROUP, "group", "later", 59, False), (FRIEND, "friend_dm", "next", 0, False)], [FRIEND, GROUP]),
    ([(GROUP, "group", "later", 61, False), (FRIEND, "friend_dm", "next", 0, False)], [GROUP, FRIEND]),
])
async def test_priority(entries, expected):
    assert [chat for chat, _, _ in await order(Attention(), Clock(), entries)] == expected


@pytest.mark.parametrize("scope, yields", [("group", True), ("owner_dm", False)])
async def test_cap_only_yields_to_a_waiting_non_owner(scope, yields):
    loop, clock = Attention(), Clock()
    chat = GROUP if yields else OWNER
    for _ in range(3):
        async with loop.visit(chat, scope, "next", clock()):
            pass  # nobody waits, so the chat can exceed the cap
    async with loop.visit(chat, scope, "next", clock()) as entry:
        assert entry.capped is None
        picked = []
        async def enter(target, target_scope, since):
            async with loop.visit(target, target_scope, "next", since) as next_entry:
                picked.append((target, next_entry.capped))
        # Queue a new visit for the same chat after releasing its per-chat slot. The scheduler ordinarily
        # encounters this at a pick following another outstanding reservation.
        again = asyncio.create_task(enter(chat, scope, clock() - timedelta(seconds=10)))
        other = asyncio.create_task(enter(FRIEND, "friend_dm", clock()))
        await asyncio.sleep(0)
    await asyncio.gather(again, other)
    assert picked[0] == ((FRIEND, GROUP) if yields else (OWNER, None))


async def test_two_flooding_chats_get_visits_for_ten_minutes():
    loop, clock = Attention(), Clock()
    counts = {GROUP: 0, FRIEND: 0}
    end = clock() + timedelta(minutes=10)

    async def flood(chat, tier):
        while clock() < end:
            since = clock()
            async with loop.visit(chat, "group", tier, since):
                assert clock() - since <= timedelta(seconds=70)
                counts[chat] += 1
                await asyncio.sleep(0)
                clock.advance(seconds=5)

    await asyncio.gather(flood(GROUP, "next"), flood(FRIEND, "later"))
    assert all(count >= 8 for count in counts.values())


async def test_composing_is_serial_typing_is_deferred_and_sending_is_parallel(setup_dispatch, monkeypatch):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    for chat in (GROUP, FRIEND):
        await app.store.set_chat_auth_status(chat, "approved")
        await dispatch.presence.go_online(chat, parse_habits(""))
    release_compose, release_send, sending, friend_composed = (asyncio.Event() for _ in range(4))
    entered = []
    compose, send = dispatch._compose_turn, app.bot.send_message

    async def compose_gated(chat, *args, **kwargs):
        entered.append(chat)
        if chat == GROUP:
            await release_compose.wait()
        result = await compose(chat, *args, **kwargs)
        if chat == FRIEND:
            friend_composed.set()
        return result

    async def send_gated(chat, *args, **kwargs):
        if chat == GROUP:
            sending.set()
            await release_send.wait()
        return await send(chat, *args, **kwargs)

    monkeypatch.setattr(dispatch, "_compose_turn", compose_gated)
    monkeypatch.setattr(app.bot, "send_message", send_gated)
    start = clock()
    monkeypatch.setattr("dom.dispatch.monotonic", lambda: (clock() - start).total_seconds())
    try:
        await handle_message(app, tg())
        await until(lambda: entered == [GROUP])
        await handle_message(app, tg(chat_id=FRIEND))
        await until(lambda: FRIEND in dispatch.attention.waiting)
        assert entered == [GROUP]
        assert not app.bot.of("action", FRIEND)
        clock.advance(seconds=5)
        release_compose.set()
        await until(lambda: sending.is_set() and friend_composed.is_set())
        assert app.bot.of("action", FRIEND)
    finally:
        release_compose.set()
        release_send.set()
        await asyncio.wait_for(drain_dispatch(dispatch), 2)
    view = await _turns(app)
    assert [visit["chat_id"] for visit in view["visits"]] == [GROUP, FRIEND]
    friend = next(row for row in view["rows"] if row["chat_id"] == str(FRIEND))
    assert ("Waiting for another chat", 5000) in friend["timing"]
    assert friend["visits"][0]["behind"] == GROUP
    html = render("turns.html", context=view).body.decode()
    assert html.count('<svg class="attention-track"') == 2
    assert '<rect x="0" width="1000" height="10">' in html
    assert '<rect x="998" width="2" height="10">' in html


async def test_failed_visit_frees_token_and_spend_cap_skips_compose(setup_dispatch, monkeypatch):
    app, dispatch, _ = await setup_dispatch(FakeJev(done=1))
    for chat in (GROUP, FRIEND, OWNER):
        await app.store.set_chat_auth_status(chat, "approved")
        await dispatch.presence.go_online(chat, parse_habits(""))
    compose = dispatch._compose_turn
    entered = []
    release_failure = asyncio.Event()

    async def fail_group(chat, *args, **kwargs):
        entered.append(chat)
        if chat == GROUP:
            await release_failure.wait()
            raise ValueError("compose failed")
        return await compose(chat, *args, **kwargs)

    async def cap(chat):
        return chat != OWNER

    monkeypatch.setattr(dispatch, "_compose_turn", fail_group)
    monkeypatch.setattr(dispatch, "model_allowed", cap)
    await handle_message(app, tg())
    await until(lambda: entered == [GROUP])
    try:
        await handle_message(app, tg(chat_id=OWNER, sender=OWNER))
        await handle_message(app, tg(chat_id=FRIEND))
        await until(lambda: FRIEND in dispatch.attention.waiting)
        assert OWNER not in dispatch.attention.waiting
    finally:
        release_failure.set()
        await asyncio.wait_for(drain_dispatch(dispatch), 2)
    assert entered == [GROUP, FRIEND]
    assert dispatch.attention.active is None
    assert all(row["chat_id"] != str(OWNER) or not turn_log.visits_in(json.loads(row["trace"]))
               for row in await app.store.recent_turns())


async def test_waiting_supersedes_keep_age_and_do_not_use_restart_budget(setup_dispatch, monkeypatch):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    async with dispatch.attention.visit(FRIEND, "friend_dm", "next", clock()):
        since = clock()
        for msg_id in (1, 2, 3, 4):
            await handle_message(app, tg(msg_id=msg_id))
            await until(lambda: GROUP in dispatch.attention.waiting and dispatch._active[GROUP].msg_id == msg_id)
            assert dispatch.attention.waiting[GROUP].since == since
            assert not dispatch.attention.waiting[GROUP].resume
            clock.advance(seconds=1)
        assert sum(attempt.visited for attempt in dispatch._interrupted[GROUP]) == 0
    await asyncio.wait_for(drain_dispatch(dispatch), 2)
    [row] = await app.store.recent_turns()
    [visit] = turn_log.visits_in(json.loads(row["trace"]))
    assert visit["reason"] == "next"
    assert len(app.bot.of("send", GROUP)) == 1


async def test_return_metrics_count_other_chats_prompt_tokens():
    loop, clock = Attention(), Clock()
    system = [{"role": "system", "content": "fixed"}]

    async def compose(chat, inputs):
        trace = turn_log.TurnTrace()
        async with loop.visit(chat, "group", "next", clock()) as entry:
            with turn_log.track(trace), turn_log.attention_visit(loop, entry, clock(), clock):
                for tokens in inputs:
                    with turn_log.model_call("m", "chat", system, []) as call:
                        call.input_tokens = tokens
        return trace.model_calls

    await compose(GROUP, [10, 12])
    clock.advance(seconds=75)
    await compose(FRIEND, [20, 30])
    clock.advance(seconds=10)
    first, second = await compose(GROUP, [10, 12])
    assert (first.gap_ms, first.between_tokens) == (85_000, 50)
    assert second.gap_ms is None


async def test_real_chat_client_records_reported_tokens_in_cache_on_return_view(make_app, monkeypatch):
    import httpx

    from dom.llm import ChatClient
    from tghelpers import FINAL, no_sleep

    app = make_app()
    clock = app.dispatch.now
    used = iter(((10, 0), (73, 0), (27, 0), (20, 15)))

    def model(_request):
        tokens, cached = next(used)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": FINAL}}],
            "usage": {"prompt_tokens": tokens, "prompt_tokens_details": {"cached_tokens": cached}},
        })

    for chat in (GROUP, FRIEND, OWNER):
        await app.store.set_chat_auth_status(chat, "approved")
        await app.dispatch.presence.go_online(chat, parse_habits(""))
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        monkeypatch.setattr(app, "client", ChatClient(app.config.models["chat"], None, http, sleep=no_sleep))
        for chat, msg_id in ((GROUP, 1), (FRIEND, 1), (OWNER, 1), (GROUP, 2)):
            await handle_message(app, tg(chat_id=chat, msg_id=msg_id, sender=OWNER if chat == OWNER else FRIEND))
            await drain_dispatch(app.dispatch)
            clock.advance(seconds=40)
    view = await _turns(app)
    assert ("1–5 min", 1, 75.0, 100) in view["cache"]
    [call] = view["rows"][0]["model_calls"]
    assert call["gap_ms"] == 120_000 and call["between_tokens"] == 100


async def test_a_held_reply_releases_attention_before_its_owner_notice_completes(make_app, monkeypatch):
    from dom.usage import request_kind

    class Jev(FakeJev):
        async def decide(self, state, questions):
            if "leak" in questions:
                assert request_kind("triage") == "guard"
                return {"leak": {"type": "score", "score": 3}}
            return await super().decide(state, questions)

    app = make_app()
    app.config = app.config.model_copy(update={"leak_check": True})
    app.dispatch.jev = Jev(done=1)
    for chat in (GROUP, FRIEND):
        await app.store.set_chat_auth_status(chat, "approved")
        await app.dispatch.presence.go_online(chat, parse_habits(""))
    notice_pending, release = asyncio.Event(), asyncio.Event()
    sends = app.bot.send_message

    async def hold_notice(chat, text, **kwargs):
        if chat == OWNER and text.startswith("Held back"):
            notice_pending.set()
            await release.wait()
        return await sends(chat, text, **kwargs)

    monkeypatch.setattr(app.bot, "send_message", hold_notice)
    try:
        await handle_message(app, tg())
        await asyncio.wait_for(notice_pending.wait(), 2)
        await handle_message(app, tg(chat_id=FRIEND))
        await until(lambda: len(app.script.requests) == 2)
    finally:
        release.set()
        await asyncio.wait_for(drain_dispatch(app.dispatch), 2)
    assert not app.bot.of("send", GROUP)
    assert len(app.bot.of("send", OWNER)) == 2


@pytest.mark.parametrize("queued, cancels", [(False, False), (True, True)])
async def test_only_supersedes_while_queued_for_the_token_are_free_restarts(setup_dispatch, queued, cancels):
    from dom.dispatch import TurnAttempt
    from dom.telegram.convert import to_incoming
    from tghelpers import tg

    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    sleeper = asyncio.create_task(asyncio.sleep(10))
    dispatch._tasks.add(sleeper)
    try:
        prior = [TurnAttempt(sleeper, msg_id, turn_log.TurnTrace(), clock(), queued=queued) for msg_id in (1, 2)]
        dispatch._interrupted[GROUP] = prior
        active = dispatch._active[GROUP] = TurnAttempt(sleeper, 3, turn_log.TurnTrace(), clock())
        dispatch.notify(to_incoming(tg(msg_id=4)))
        assert active.trace.superseded is cancels
        assert active.later == ([] if cancels else [4])
    finally:
        dispatch._tasks.discard(sleeper)
        sleeper.cancel()
        dispatch._active.pop(GROUP, None)
        dispatch._interrupted.pop(GROUP, None)

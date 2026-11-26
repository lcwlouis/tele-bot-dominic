"""The owner-DM fast lane: the owner's turns compose on a token of their own."""

import asyncio
import json

import httpx
import pytest

from dom.attention import Attention
from dom.presence import parse_habits
from dom.telegram.handlers import handle_callback, handle_message
from test_attention import until
from test_dispatch import FakeJev, setup_dispatch  # noqa: F401  (the shared dispatcher fixture)
from test_escalation import answer, cards, price_list, tap_for, wire
from dom.web.shared import _turns
from tghelpers import FRIEND, GROUP, OWNER, Clock, drain_dispatch, tg


async def ready(app, dispatch, *chats):
    for chat in chats:
        await app.store.set_chat_auth_status(chat, "approved")
        await dispatch.presence.go_online(chat, parse_habits(""))


def owner_message(msg_id=1, text="hello"):
    return tg(chat_id=OWNER, sender=OWNER, msg_id=msg_id, text=text)


class Compose:
    """Wraps `_compose_turn`: records each chat's composes, and holds the ones in `hold` until released."""

    def __init__(self, dispatch, monkeypatch, hold=()):
        self.entered: list[tuple[int, int]] = []  # (chat, newest message id), in the order composes began
        self.finished: list[int] = []
        self.in_flight: dict[int, int] = {}
        self.peak: dict[int, int] = {}
        self.release = asyncio.Event()
        self.hold = set(hold)
        original = dispatch._compose_turn

        async def compose(chat, scope, newest, *args, **kwargs):
            self.entered.append((chat, newest.msg_id))
            self.in_flight[chat] = self.in_flight.get(chat, 0) + 1
            self.peak[chat] = max(self.peak.get(chat, 0), self.in_flight[chat])
            try:
                if chat in self.hold:
                    await self.release.wait()
                result = await original(chat, scope, newest, *args, **kwargs)
                self.finished.append(chat)
                return result
            finally:
                self.in_flight[chat] -= 1

        monkeypatch.setattr(dispatch, "_compose_turn", compose)

    def chats(self):
        return [chat for chat, _ in self.entered]


@pytest.mark.parametrize("fast_lane", [True, False])
async def test_an_owner_turn_does_not_wait_for_a_slow_group_turn(setup_dispatch, monkeypatch, fast_lane):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    dispatch.attention.fast_lane = fast_lane
    await ready(app, dispatch, GROUP, OWNER)
    compose = Compose(dispatch, monkeypatch, hold=[GROUP])
    try:
        await handle_message(app, tg())
        await until(lambda: compose.chats() == [GROUP])
        await handle_message(app, owner_message())
        if fast_lane:
            await until(lambda: OWNER in compose.finished)  # composed while the group's turn is still held
            assert dispatch.attention.main.active.chat_id == GROUP and compose.in_flight[GROUP] == 1
            assert dispatch.attention.fast.active is None
        else:
            await until(lambda: OWNER in dispatch.attention.main.waiting)  # as before: queued behind the group
            assert compose.chats() == [GROUP]
    finally:
        compose.release.set()
        await asyncio.wait_for(drain_dispatch(dispatch), 2)
    assert OWNER in compose.finished and GROUP in compose.finished
    rows = {row["chat_id"]: row for row in (await _turns(app))["rows"]}
    assert rows[str(OWNER)]["lane"] == ("fast" if fast_lane else "main")
    assert rows[str(GROUP)]["lane"] == "main"
    assert rows[str(OWNER)]["queue_wait_ms"] is not None


async def test_two_owner_messages_together_never_compose_at_once(setup_dispatch, monkeypatch):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    await ready(app, dispatch, OWNER)
    compose = Compose(dispatch, monkeypatch, hold=[OWNER])
    try:
        await handle_message(app, owner_message(1, "first"))
        await until(lambda: compose.entered == [(OWNER, 1)])
        await handle_message(app, owner_message(2, "second"))
        await until(lambda: (OWNER, 2) in compose.entered)
        assert compose.peak[OWNER] == 1  # the first attempt ended before the second composed
    finally:
        compose.release.set()
        await asyncio.wait_for(drain_dispatch(dispatch), 2)
    assert compose.peak[OWNER] == 1
    assert compose.entered[-1] == (OWNER, 2)
    assert dispatch.attention.fast.active is None and not dispatch.attention.fast.waiting


async def test_the_lanes_share_the_cache_history_and_facts(setup_dispatch):
    attention = Attention(fast_lane=True)
    clock_at = Clock()()
    assert attention.lane_for("owner_dm") is attention.fast
    for scope in ("group", "friend_dm"):
        assert attention.lane_for(scope) is attention.main
    attention.record_call(GROUP, clock_at, 100)  # a main-lane call
    attention.record_call(OWNER, clock_at, 40)  # a fast-lane call
    attention.record_call(GROUP, clock_at, 10)
    assert attention.previous_call(OWNER) == (clock_at, 10)  # the group's call since is counted for the owner
    attention.finish_conversation(GROUP, "group", clock_at, title="Book Club", fact="picked a book")
    assert "picked a book" in attention.elsewhere(OWNER, "owner_dm", clock_at)  # the owner sees it, not vice versa
    attention.finish_conversation(OWNER, "owner_dm", clock_at, fact="private")
    assert "private" not in attention.elsewhere(GROUP, "group", clock_at)


async def test_the_switch_off_keeps_one_token(setup_dispatch):
    attention = Attention(fast_lane=False)
    assert attention.lane_for("owner_dm") is attention.main
    clock = Clock()
    async with attention.visit(GROUP, "group", "next", clock()):
        waiter = asyncio.create_task(_visit(attention, clock))
        await until(lambda: OWNER in attention.main.waiting)
        assert not waiter.done()
    assert (await waiter).reason == "owner"


async def _visit(attention, clock):
    async with attention.visit(OWNER, "owner_dm", "next", clock()) as entry:
        return entry


async def test_the_fast_lane_never_serves_another_chat():
    attention = Attention(fast_lane=True)
    for scope in ("group", "friend_dm"):
        assert attention.lane_for(scope) is attention.main
        assert attention.can_type(FRIEND, scope) is True
    clock = Clock()
    async with attention.visit(OWNER, "owner_dm", "next", clock()):
        assert attention.fast.active.chat_id == OWNER
        assert attention.main.active is None
        assert attention.can_type(GROUP, "group")  # the owner's visit does not hold back the main lane's typing


async def test_a_card_tap_is_handled_while_a_long_turn_runs_elsewhere(make_app, monkeypatch):
    app = make_app()
    app.dispatch.escalation.clock = lambda: 0
    attempts = []

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        model = json.loads(request.content)["model"]
        attempts.append(model)
        if model == "local":
            return httpx.Response(400, json={"error": {"message": "model not found"}})
        return answer()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http, explicit=True)
        await ready(app, app.dispatch, OWNER, GROUP)
        await handle_message(app, owner_message(text="original task"))
        await drain_dispatch(app.dispatch)
        assert attempts == ["local"]
        [(_, _, text, options)] = [call for call in app.bot.of("send", OWNER) if "Retry on cloud?" in call[2]]
        [buttons] = options["reply_markup"].inline_keyboard

        compose = Compose(app.dispatch, monkeypatch, hold=[GROUP])
        try:
            await handle_message(app, tg(msg_id=2))
            await until(lambda: compose.chats() == [GROUP])
            await handle_callback(app, tap_for(buttons[0], msg_id=app.bot._next_id))
            await until(lambda: attempts == ["local", "cloud"])  # the retry turn ran while the group's was held
            assert compose.in_flight[GROUP] == 1
        finally:
            compose.release.set()
            await asyncio.wait_for(drain_dispatch(app.dispatch), 2)


async def test_the_turn_log_api_shows_lane_and_wait(setup_dispatch, browser):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    await ready(app, dispatch, GROUP, OWNER)
    await handle_message(app, tg())
    await handle_message(app, owner_message())
    await asyncio.wait_for(drain_dispatch(dispatch), 2)
    async with browser(app) as client:
        turns = (await client.get("/api/system/turns")).json()["turns"]
    lanes = {turn["title"]: turn for turn in turns}
    assert {turn["lane"] for turn in turns} == {"main", "fast"}
    assert all(isinstance(turn["queue_wait_ms"], int) for turn in turns)
    assert all(turn["visits"] for turn in lanes.values())


async def test_close_drains_both_lanes(setup_dispatch, monkeypatch):
    app, dispatch, clock = await setup_dispatch(FakeJev(done=1))
    await ready(app, dispatch, GROUP, OWNER)
    compose = Compose(dispatch, monkeypatch, hold=[GROUP, OWNER])
    await handle_message(app, tg())
    await handle_message(app, owner_message())
    await until(lambda: sorted(compose.chats()) == sorted([GROUP, OWNER]))
    assert dispatch.attention.main.active.chat_id == GROUP and dispatch.attention.fast.active.chat_id == OWNER
    await asyncio.wait_for(dispatch.close(), 2)
    assert dispatch.attention.main.active is None and dispatch.attention.fast.active is None
    assert not dispatch._tasks and compose.in_flight == {GROUP: 0, OWNER: 0}

"""Owner consent through a Telegram turn, the model route and real SQLite accounting."""

import asyncio
import json

import httpx
import pytest
from aiogram.types import CallbackQuery

from dom import escalation
from dom.config import ModelEndpoint
from dom.llm import ChatClient, FallbackClient, routed_client
from dom.presence import parse_habits
from dom.telegram.handlers import handle_callback, handle_message
from test_usage import USAGE, usage_rows
from tghelpers import BOT_USER, FINAL, FRIEND, GROUP, OWNER, drain_dispatch, no_sleep, tg

LOCAL = ModelEndpoint(base_url="http://localhost/v1", model="local")
CLOUD = ModelEndpoint(base_url="https://openrouter.ai/api/v1", model="cloud")


def answer(round_number=None):
    message = {"role": "assistant", "content": FINAL if round_number is None else None}
    if round_number is not None:
        message["tool_calls"] = [{
            "id": f"call-{round_number}", "type": "function",
            "function": {"name": "unavailable_tool", "arguments": "{}"},
        }]
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": USAGE})


def price_list(request):
    assert str(request.url) == escalation.PRICE_LIST + "?zdr=true"
    assert request.content == b"" and "authorization" not in request.headers
    return httpx.Response(200, json={"data": [{
        "id": "cloud", "pricing": {"prompt": "0", "completion": "0.000004", "request": "0.001"},
    }]})


def wire(app, http, *, explicit=False):
    models = {"chat": LOCAL, "escalation" if explicit else "drafting": CLOUD}
    app.config = app.config.model_copy(update={"models": models, "daily_spend_cap_usd": 0.01})
    app.client = ChatClient(LOCAL, None, http, sleep=no_sleep, usage=app.dispatch.usage)
    app.escalation_client = routed_client(app.config, "escalation", http, app.dispatch.usage)


def cards(app):
    return [call for call in app.bot.of("send", OWNER) if call[2].startswith("Model switch available")]


def tap_for(button, *, chat_id=OWNER, sender=OWNER, msg_id=5001):
    return CallbackQuery(
        id="tap", from_user=tg(chat_id=sender, sender=sender).from_user, chat_instance="instance",
        message=tg(chat_id=chat_id, sender=BOT_USER.id, msg_id=msg_id), data=button.callback_data,
    )


async def start(app, chat_id=OWNER, msg_id=1, text="hello"):
    await app.store.set_chat_auth_status(chat_id, "approved")
    await app.dispatch.presence.go_online(chat_id, parse_habits(""))
    await handle_message(app, tg(chat_id=chat_id, sender=chat_id if chat_id > 0 else OWNER, msg_id=msg_id, text=text))


@pytest.mark.parametrize("switch", [True, False, None])
async def test_owner_consent_changes_only_later_rounds_and_expires(make_app, monkeypatch, switch):
    app = make_app()
    now = 0.0
    app.dispatch.escalation.clock = lambda: now
    sent = asyncio.Event()
    third_started, finish_third = asyncio.Event(), asyncio.Event()
    requests, pricing, approval_checks = [], [], []
    original_send = app.bot.send_message
    original_allowed = app.dispatch.model_allowed

    async def send(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        if cards(app):
            sent.set()
        return result

    async def allowed(chat_id):
        approval_checks.append(chat_id)
        return await original_allowed(chat_id)

    monkeypatch.setattr(app.bot, "send_message", send)
    monkeypatch.setattr(app.dispatch, "model_allowed", allowed)

    async def respond(request):
        nonlocal now
        if request.method == "GET":
            pricing.append(request.url)
            return price_list(request)
        body = json.loads(request.content)
        requests.append(body["model"])
        round_number = len(requests)
        if body["model"] == "cloud":
            assert body["provider"] == {"zdr": True}
        if round_number == 1:
            now = 61
        if round_number == 2:
            assert cards(app) == []  # one completed round is insufficient, even after 60 seconds
        if round_number == 3:
            third_started.set()
            await finish_third.wait()
        return answer(round_number if round_number < 5 else None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        await start(app)
        try:
            async with asyncio.timeout(3):
                await third_started.wait()
                await sent.wait()
            assert requests == ["local"] * 3  # work continued while consent was pending
            [(kind, chat_id, text, options)] = cards(app)
            card_id = app.bot._next_id
            assert chat_id == OWNER and "cloud" in text and "Estimated $0.0050 for the next round" in text
            [buttons] = options["reply_markup"].inline_keyboard
            assert [button.text for button in buttons] == ["Switch", "Keep local"]
            await handle_callback(app, tap_for(buttons[0], chat_id=GROUP, msg_id=card_id))
            await handle_callback(app, tap_for(buttons[0], sender=FRIEND, msg_id=card_id))
            assert app.bot.of("answer") == []
            if switch is not None:
                await handle_callback(app, tap_for(buttons[0 if switch else 1], msg_id=card_id))
                await handle_callback(app, tap_for(buttons[1 if switch else 0], msg_id=card_id))
                assert app.bot.of("answer")[-1][2] == "Already handled"
        finally:
            finish_third.set()
        await drain_dispatch(app.dispatch)
        expected = ["cloud", "cloud"] if switch else ["local", "local"]
        assert requests == ["local"] * 3 + expected
        assert len(cards(app)) == 1
        await handle_callback(app, tap_for(buttons[0], msg_id=card_id))
        assert app.bot.of("answer")[-1][2] == "That task already finished"
        escalation_rows = [row for row in usage_rows(app) if row[0] == "escalation"]
        if switch:
            assert escalation_rows == [("escalation", "cloud", "openrouter.ai", OWNER, 12, 4, 0.25, 0)] * 2
            assert len(approval_checks) >= len(requests) and set(approval_checks) == {OWNER}
        else:
            assert escalation_rows == []
        await start(app, msg_id=2)
        await drain_dispatch(app.dispatch)
        assert requests[-1] == "local" and len(pricing) == 1


@pytest.mark.parametrize("failure", ["primary", "internal_retry"])
async def test_successful_chat_rounds_never_count_provider_attempts_as_failures(make_app, failure):
    app = make_app()
    app.dispatch.escalation.clock = lambda: 0
    attempts = []

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        model = json.loads(request.content)["model"]
        attempts.append(model)
        if model == "local":
            if failure == "primary":
                raise httpx.ConnectError("unreachable", request=request)
            if len(attempts) <= 2:
                return httpx.Response(503)
        await asyncio.sleep(0.02)  # a wrongly queued card would be sent while the route is still answering
        return answer(1 if len(attempts) in (2, 3) else None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        backup_endpoint = ModelEndpoint(base_url="https://fallback.example.test/v1", model="backup")
        backup = ChatClient(backup_endpoint, None, http, sleep=no_sleep, usage=app.dispatch.usage)
        app.client = FallbackClient(app.client, backup)
        await start(app)
        await drain_dispatch(app.dispatch)

    expected = ["local", "backup"] * 2 if failure == "primary" else ["local"] * 4
    assert attempts == expected and cards(app) == []
    assert any(call[2] == "hey" for call in app.bot.of("send", OWNER))


@pytest.mark.parametrize("decision", ["retry", "dismiss", "expired"])
async def test_failure_card_outlives_the_turn_and_retries_at_most_once(make_app, decision):
    app = make_app()
    now = 0
    app.dispatch.escalation.clock = lambda: now
    attempts = []

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        body = json.loads(request.content)
        attempts.append(body["model"])
        if len(attempts) <= 2:
            raise httpx.ConnectError("unreachable", request=request)
        if body["model"] == "cloud":
            assert body["provider"] == {"zdr": True}
            assert "original task" in json.dumps(body["messages"])
            assert "newer unrelated message" not in json.dumps(body["messages"])
            assert app.dispatch.turn_running(OWNER)
        return answer()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http, explicit=True)
        await start(app, text="original task")
        await drain_dispatch(app.dispatch)
        assert not app.dispatch.turn_running(OWNER)
        assert (await app.store.get_unread(OWNER))[0] == 0
        assert attempts == ["local"] * 2
        [(kind, chat_id, text, options)] = [
            call for call in app.bot.of("send", OWNER) if "Retry on cloud?" in call[2]
        ]
        assert "Dom couldn't answer with local" in text and "$0.0050" in text
        [buttons] = options["reply_markup"].inline_keyboard
        assert [button.text for button in buttons] == ["Retry", "Dismiss"]
        card_id = app.bot._next_id

        await start(app, msg_id=2, text="newer unrelated message")
        await drain_dispatch(app.dispatch)
        assert attempts == ["local"] * 3

        if decision == "expired":
            now += escalation.RETRY_OFFER_SECONDS
        button = buttons[1 if decision == "dismiss" else 0]
        async with app.locks[OWNER]:
            await handle_callback(app, tap_for(button, msg_id=card_id))
            outcome = app.bot.of("answer")[-1][2]
            assert outcome == {
                "retry": "Retry requested", "dismiss": "Retry dismissed", "expired": "That retry offer expired",
            }[decision]
            await asyncio.sleep(0)
            assert attempts == ["local"] * 3
        if decision != "expired":
            await handle_callback(app, tap_for(button, msg_id=card_id))
            assert app.bot.of("answer")[-1][2] == "Already handled"
        await asyncio.sleep(0)  # let the completed card edit leave the dispatch task set
        await drain_dispatch(app.dispatch)

    assert app.bot.of("edit", OWNER)[-1][3] == outcome
    assert app.bot.edit_markups[-1] is None
    assert attempts == ["local"] * 3 + (["cloud"] if decision == "retry" else [])
    escalated = [row for row in usage_rows(app) if row[0] == "escalation"]
    assert escalated == ([("escalation", "cloud", "openrouter.ai", OWNER, 12, 4, 0.25, 0)]
                         if decision == "retry" else [])


@pytest.mark.parametrize("chat_id", [GROUP, FRIEND])
async def test_non_owner_chats_never_offer_escalation(make_app, chat_id):
    app = make_app()
    now = 0
    app.dispatch.escalation.clock = lambda: now
    rounds = 0

    async def respond(request):
        nonlocal now, rounds
        # No price lookup or cloud request should be started for these chats.
        assert request.method == "POST" and json.loads(request.content)["model"] == "local"
        rounds += 1
        now = 61
        return answer(1) if rounds < 3 else answer()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0})
        await start(app, chat_id)
        await drain_dispatch(app.dispatch)

    assert rounds == 3 and app.bot.of("send", OWNER) == []


@pytest.mark.parametrize("failure", ["bad_json", "bad_request", "read_timeout"])
async def test_recovery_stops_on_permanent_errors_and_shortens_the_second_timeout(make_app, failure):
    app = make_app()
    timeouts = []

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        timeouts.append(request.extensions["timeout"]["read"])
        if failure == "bad_json":
            return httpx.Response(200, text="invalid")
        if failure == "bad_request":
            return httpx.Response(400)
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        await start(app)
        await drain_dispatch(app.dispatch)

    assert timeouts == ([90, escalation.RECOVERY_TIMEOUT] if failure == "read_timeout" else [90])


async def test_failed_price_refresh_keeps_last_price(make_app):
    app = make_app()
    now = 0
    app.dispatch.escalation.prices.clock = lambda: now
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return price_list(request) if calls == 1 else httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        prices = app.dispatch.escalation.prices
        cloud = app.escalation_client.primary
        estimate = await prices.estimate(cloud, [], [])
        now += escalation.PRICE_TTL
        assert await prices.estimate(cloud, [], []) == estimate
        assert "$0.0050" in estimate and calls == 2


async def test_timer_offers_during_a_round_already_in_progress(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(escalation, "OFFER_AFTER_SECONDS", 0.05)
    sent = asyncio.Event()
    original_send = app.bot.send_message
    rounds = 0

    async def send(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        if cards(app):
            sent.set()
        return result

    monkeypatch.setattr(app.bot, "send_message", send)

    async def respond(request):
        nonlocal rounds
        if request.method == "GET":
            return price_list(request)
        rounds += 1
        if rounds == 3:
            await asyncio.wait_for(sent.wait(), 3)
            return answer()
        return answer(rounds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        await start(app)
        await drain_dispatch(app.dispatch)

    assert rounds == 3 and len(cards(app)) == 1


async def test_slow_price_fetch_can_outlast_the_local_reply(make_app, monkeypatch):
    app = make_app()
    now = 0.0
    app.dispatch.escalation.clock = lambda: now
    priced, delivered = asyncio.Event(), asyncio.Event()
    original_send = app.bot.send_message
    rounds = 0

    async def send(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        if args[:2] == (OWNER, "hey"):
            delivered.set()
        return result

    monkeypatch.setattr(app.bot, "send_message", send)

    async def respond(request):
        nonlocal now, rounds
        if request.method == "GET":
            await priced.wait()
            return price_list(request)
        assert json.loads(request.content)["model"] == "local"
        now = 61
        rounds += 1
        return answer(rounds if rounds < 3 else None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http)
        await start(app)
        try:
            await asyncio.wait_for(delivered.wait(), 3)
            assert rounds == 3 and cards(app) == []
        finally:
            priced.set()
        await drain_dispatch(app.dispatch)
    assert cards(app) == []


async def test_a_failure_another_try_cannot_fix_offers_the_retry_after_one_round(make_app):
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
        await start(app, text="original task")
        await drain_dispatch(app.dispatch)
        assert attempts == ["local"]
        [(_, _, text, options)] = [call for call in app.bot.of("send", OWNER) if "Retry on cloud?" in call[2]]
        [buttons] = options["reply_markup"].inline_keyboard
        await handle_callback(app, tap_for(buttons[0], msg_id=app.bot._next_id))
        await asyncio.sleep(0)
        await drain_dispatch(app.dispatch)
    assert attempts == ["local", "cloud"]


async def test_a_retry_the_cap_blocks_says_so(make_app, monkeypatch):
    app = make_app()
    app.dispatch.escalation.clock = lambda: 0

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http, explicit=True)
        await start(app, text="original task")
        await drain_dispatch(app.dispatch)
        [turn] = app.dispatch.escalation.pending.values()
        allowed = app.dispatch.turn_allowed

        async def blocked(chat_id, scope):
            return False

        monkeypatch.setattr(app.dispatch, "turn_allowed", blocked)
        await app.dispatch.retry_escalation(turn.retry_task)  # what a Retry tap spawns
        monkeypatch.setattr(app.dispatch, "turn_allowed", allowed)
        await drain_dispatch(app.dispatch)
    assert app.bot.of("send", OWNER)[-1][2] == "The retry did not run: model calls are paused right now."

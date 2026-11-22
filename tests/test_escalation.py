"""Owner consent through a Telegram turn, the model route and real SQLite accounting."""

import asyncio
import json

import httpx
import pytest
from aiogram.types import CallbackQuery

from dom import escalation
from dom.config import ModelEndpoint
from dom.llm import ChatClient, routed_client
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


def tap_for(button, *, chat_id=OWNER, sender=OWNER):
    return CallbackQuery(
        id="tap", from_user=tg(chat_id=sender, sender=sender).from_user, chat_instance="instance",
        message=tg(chat_id=chat_id, sender=BOT_USER.id), data=button.callback_data,
    )


async def start(app, chat_id=OWNER, msg_id=1):
    await app.store.set_chat_auth_status(chat_id, "approved")
    await app.dispatch.presence.go_online(chat_id, parse_habits(""))
    await handle_message(app, tg(chat_id=chat_id, sender=chat_id if chat_id > 0 else OWNER, msg_id=msg_id))


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
            assert chat_id == OWNER and "cloud" in text and "Estimated $0.0050 for the next round" in text
            [buttons] = options["reply_markup"].inline_keyboard
            assert [button.text for button in buttons] == ["Switch", "Keep local"]
            await handle_callback(app, tap_for(buttons[0], chat_id=GROUP))
            await handle_callback(app, tap_for(buttons[0], sender=FRIEND))
            assert app.bot.of("answer") == []
            if switch is not None:
                await handle_callback(app, tap_for(buttons[0 if switch else 1]))
                await handle_callback(app, tap_for(buttons[1 if switch else 0]))
                assert app.bot.of("answer")[-1][2] == "Already handled"
        finally:
            finish_third.set()
        await drain_dispatch(app.dispatch)
        expected = ["cloud", "cloud"] if switch else ["local", "local"]
        assert requests == ["local"] * 3 + expected
        assert len(cards(app)) == 1
        await handle_callback(app, tap_for(buttons[0]))
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


@pytest.mark.parametrize("chat_id", [OWNER, GROUP, FRIEND])
async def test_two_provider_failures_trigger_only_in_owner_dm(make_app, monkeypatch, chat_id):
    app = make_app()
    attempts = []
    sent = asyncio.Event()
    original_send = app.bot.send_message

    async def send(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        if cards(app):
            sent.set()
        return result

    monkeypatch.setattr(app.bot, "send_message", send)

    async def respond(request):
        if request.method == "GET":
            return price_list(request)
        attempts.append(json.loads(request.content)["model"])
        if len(attempts) <= 2:
            assert cards(app) == []
            return httpx.Response(503)
        if len(attempts) == 3 and chat_id == OWNER:
            await asyncio.wait_for(sent.wait(), 3)
            await handle_callback(app, tap_for(cards(app)[0][3]["reply_markup"].inline_keyboard[0][0]))
            return answer(1)
        return answer()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        wire(app, http, explicit=True)
        await start(app, chat_id)
        await drain_dispatch(app.dispatch)

    assert len(cards(app)) == int(chat_id == OWNER)
    assert attempts == ["local"] * 3 + (["cloud"] if chat_id == OWNER else [])


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

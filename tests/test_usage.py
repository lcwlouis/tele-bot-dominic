"""Accounting and spend limits through the normal model and dispatch paths."""

import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from dom.config import ModelEndpoint
from dom.jev import JevClient
from dom.llm import ChatClient, FallbackClient
from dom.media import MediaDescriber
from dom.memory_pass import MemoryPass
from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from dom.telegram.owner import render_status
from test_media import FakeDownload, photo_message
from tghelpers import FINAL, GROUP, OWNER, drain_dispatch, no_sleep, tg

USAGE = {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.25}
EMPTY_MEMORY = '{"ops": [], "summary": ""}'


def usage_rows(app):
    with sqlite3.connect(app.store.db_path) as conn:
        return conn.execute(
            "SELECT kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback "
            "FROM usage ORDER BY id"
        ).fetchall()


def chat(http, app, model="mock-chat"):
    endpoint = ModelEndpoint(base_url=f"https://{model}.example.org/v1", model=model)
    return ChatClient(endpoint, None, http, sleep=no_sleep, usage=app.dispatch.usage)


def completion(content, usage):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage})


def jev(http, app):
    return JevClient(http, "fake", "mock-triage", "https://triage.example.org", usage=app.dispatch.usage)


async def online(app, chat_id=GROUP):
    await app.store.set_chat_auth_status(chat_id, "approved")
    await app.dispatch.presence.go_online(chat_id, parse_habits(""))


async def deliver(app, message):
    await handle_message(app, message)
    await drain_dispatch(app.dispatch)


async def test_usage_labels_chat_triage_media_and_memory_calls(make_app):
    app = make_app()
    await online(app)

    def respond(request):
        payload = json.loads(request.content)
        match payload["model"]:
            case "mock-triage":
                return httpx.Response(200, json={"answers": {}, "usage": USAGE})
            case "mock-media":
                return completion("A book.", USAGE)
            case _:
                return completion(FINAL if payload.get("tools") else EMPTY_MEMORY, USAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        app.dispatch.jev = jev(http, app)
        app.media_describer = MediaDescriber(chat(http, app, "mock-media"), FakeDownload())
        await deliver(app, photo_message())
        assert await MemoryPass(app).run(GROUP)

    assert usage_rows(app) == [
        ("media", "mock-media", "mock-media.example.org", GROUP, 12, 4, 0.25, 0),
        ("triage", "mock-triage", "triage.example.org", GROUP, 12, 4, 0.25, 0),
        ("chat", "mock-chat", "mock-chat.example.org", GROUP, 12, 4, 0.25, 0),
        ("memory", "mock-chat", "mock-chat.example.org", GROUP, 12, 4, 0.25, 0),
    ]


@pytest.mark.parametrize("reported", [None, "bad", [], {"prompt_tokens": "bad", "completion_tokens": True, "cost": -1}])
async def test_missing_or_malformed_usage_records_zeros(make_app, reported):
    app = make_app()

    def respond(request):
        payload = ({"answers": {}} if request.url.path == "/decisions"
                   else {"choices": [{"message": {"content": FINAL}}]})
        if reported is not None:
            payload["usage"] = reported
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert (await chat(http, app).complete([], []))["content"] == FINAL
        assert await jev(http, app).decide("", {}) == {}

    assert [row[4:7] for row in usage_rows(app)] == [(0, 0, 0), (0, 0, 0)]


async def test_cap_blocks_group_turn_triage_media_and_memory_but_not_owner(make_app):
    app = make_app()
    await online(app)
    await online(app, OWNER)
    await app.dispatch.usage.record("chat", "mock-chat", "example.org", {"cost": 1.25})
    requested = []

    def respond(request):
        model = json.loads(request.content)["model"]
        requested.append(model)
        if model == "mock-triage":
            return httpx.Response(200, json={"answers": {}})
        return completion(FINAL, USAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        app.dispatch.jev = jev(http, app)
        app.media_describer = MediaDescriber(chat(http, app, "mock-media"), FakeDownload())
        await deliver(app, photo_message())
        assert not await MemoryPass(app).run(GROUP)
        await deliver(app, tg(msg_id=2))
        assert requested == []
        await deliver(app, tg(chat_id=OWNER, sender=OWNER))
        assert requested == ["mock-triage", "mock-chat"]

    assert (await app.store.get_unread(GROUP))[0] == 2
    assert (await app.store.get_unread(OWNER))[0] == 0
    assert await app.store.get_memory_cursor(GROUP) == 0
    notice, = [call for call in app.bot.of("send", OWNER) if call[2].startswith("Daily spend cap")]
    expected = (
        "Daily spend cap of $1.00 reached ($1.25 spent). Friends' chats are paused until midnight. "
        "Change `daily_spend_cap_usd` in config.toml."
    )
    assert notice[2] == (
        "Daily spend cap reached: $1.25 of $1.00\nFriends' chats are paused until midnight.\n"
        "Change daily_spend_cap_usd in config.toml."
    )
    assert [entity.type for entity in notice[3]["entities"]] == ["bold"]
    events = [m for m in await app.store.get_recent_chat_window(OWNER) if m.role == "event"]
    assert [m.text for m in events] == [expected]


async def test_cap_stops_before_second_chat_completion_in_a_turn(make_app):
    app = make_app()
    await online(app)
    calls = []

    def respond(request):
        calls.append(request)
        return completion("This requires a correction.", {"cost": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        await deliver(app, tg())

    assert len(calls) == 1
    assert (await app.store.get_unread(GROUP))[0] == 1
    assert app.bot.of("send", GROUP) == []


async def test_cap_rechecked_after_triage_reaches_it(make_app):
    app = make_app()
    await online(app)

    def respond(request):
        return httpx.Response(200, json={"answers": {}, "usage": {"cost": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.dispatch.jev = jev(http, app)
        await deliver(app, tg())

    assert app.script.requests == []
    assert (await app.store.get_unread(GROUP))[0] == 1
    assert [row[0] for row in usage_rows(app)] == ["triage"]


async def test_local_day_reset_and_one_notice_per_day_across_dispatch_restart(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Asia/Singapore"})
    clock = app.dispatch.now
    clock.at = datetime(2026, 1, 1, 15, 59, tzinfo=timezone.utc)
    await online(app)
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1})
    await deliver(app, tg())
    await app.dispatch.close()
    app.dispatch = type(app.dispatch)(app, app.dispatch.presence, None, no_sleep, clock)
    await deliver(app, tg(msg_id=2))
    assert len(app.bot.of("send", OWNER)) == 1

    clock.advance(minutes=1)  # January 2 for the owner, January 1 in UTC
    await deliver(app, tg(msg_id=3))
    assert len(app.script.requests) == 1
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1})
    await deliver(app, tg(msg_id=4))
    assert len(app.bot.of("send", OWNER)) == 2
    assert (await app.store.get_unread(GROUP))[0] == 1


async def test_zero_cap_disables_pause(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0})
    await online(app)
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 20})
    await deliver(app, tg())
    assert len(app.script.requests) == 1
    assert app.bot.of("send", OWNER) == []


async def test_fallback_usage_and_status_footer(make_app):
    app = make_app()
    await online(app)
    tz = ZoneInfo("Asia/Singapore")
    app.config = app.config.model_copy(update={"timezone": tz.key})
    primary_up = False

    def respond(request):
        if request.url.host == "primary.example.org" and not primary_up:
            raise httpx.ConnectError("offline", request=request)
        return completion(FINAL, USAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = FallbackClient(chat(http, app, "primary"), chat(http, app, "fallback"))
        await deliver(app, tg())
        assert app.client.last_model == "fallback"
        assert app.client.last_fallback_at == app.dispatch.now()
        assert usage_rows(app)[0] == ("chat", "fallback", "fallback.example.org", GROUP, 12, 4, 0.25, 1)
        primary_up = True
        await deliver(app, tg(msg_id=2))

    when = app.dispatch.now().astimezone(tz).strftime("%H:%M")
    status = await render_status(app)
    for body in (status.plain, status.markdown):
        assert "Today: 2 calls · $0.50" in body
        assert f"Last reply: primary (fallback at {when})" in body
    app.dispatch.now.advance(hours=5)
    status = await render_status(app)
    assert "Today: 0 calls · $0.00" in status.plain
    assert "Last reply: primary" in status.plain and "fallback at" not in status.plain


async def test_memory_pass_fallback_does_not_change_last_reply(make_app):
    app = make_app()
    await online(app)
    primary_up = True

    def respond(request):
        if request.url.host == "primary.example.org":
            if not primary_up:
                raise httpx.ConnectError("offline", request=request)
            return completion(FINAL, USAGE)
        return completion(EMPTY_MEMORY, USAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = FallbackClient(chat(http, app, "primary"), chat(http, app, "fallback"))
        await deliver(app, tg())
        primary_up = False
        assert await MemoryPass(app).run(GROUP)

    assert app.client.last_model == "primary"
    assert app.client.last_fallback_at is None
    assert usage_rows(app)[-1][0:2] == ("memory", "fallback")
    assert usage_rows(app)[-1][-1] == 1
    assert "Last reply: primary" in (await render_status(app)).plain


async def test_failed_response_counts_but_cannot_be_last_reply(make_app):
    app = make_app()
    await online(app)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400))) as http:
        app.client = FallbackClient(chat(http, app), None)
        await deliver(app, tg())

    status = await render_status(app)
    assert "Today: 1 calls · $0.00" in status.plain
    assert "Last reply: —" in status.plain


async def test_usage_recording_failure_never_breaks_a_model_call(make_app):
    app = make_app()

    async def broken_store(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    app.store.record_usage = broken_store

    def handler(request):
        if request.url.host == "triage.example.org":
            return httpx.Response(200, json={"answers": {"q": {"type": "choice", "value": "yes"}}})
        return completion("hi", USAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await chat(http, app).complete([], []) == {"content": "hi"}
        assert await jev(http, app).decide("state", {"q": {}}) == {"q": {"type": "choice", "value": "yes"}}


@pytest.mark.parametrize(("model", "hour", "expected"), [
    ("deepseek-flash", 3, 1.506),
    ("deepseek-flash", 4, 0.753),
    ("deepseek-v4-pro", 6, 5.324),
    ("deepseek-v4-pro", 10, 2.662),
])
async def test_direct_deepseek_uses_its_usd_tariff_and_counts_toward_the_cap(make_app, model, hour, expected):
    from datetime import timedelta

    app = make_app()
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": expected * 0.9})
    now = datetime(2026, 11, 24, hour, 59, tzinfo=timezone.utc)
    app.dispatch.usage.now = lambda: now

    def handler(request):
        nonlocal now
        assert request.url == "https://api.deepseek.com/chat/completions"
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert "provider" not in body and "reasoning_effort" not in body
        now += timedelta(minutes=1)  # Two calls straddle a tariff boundary during the response.
        return completion(FINAL, {
            "prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
            "cost": 111,  # Usage uses the documented tariff.
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        endpoint = ModelEndpoint(base_url="https://api.deepseek.com", model=model)
        client = ChatClient(endpoint, "fictional-deepseek-key", http, usage=app.dispatch.usage)
        await client.complete([], [{"type": "function", "function": {"name": "example"}}])
    [(kind, stored_model, host, _, input_tokens, output_tokens, cost, _)] = usage_rows(app)
    assert (kind, stored_model, host, input_tokens, output_tokens) == ("chat", model, "api.deepseek.com", 2_000_000, 1_000_000)
    assert cost == pytest.approx(expected)
    assert await app.dispatch.usage.over_cap(app.config, app.bot, sleep=no_sleep)


async def test_unknown_deepseek_model_records_no_cost_and_warns_once(make_app, caplog):
    app = make_app()
    endpoint = ModelEndpoint(base_url="https://api.deepseek.com", model="future-chat-model")
    payload = {"prompt_tokens": 500, "completion_tokens": 10, "prompt_cache_miss_tokens": 500, "cost": 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion(FINAL, payload))) as http:
        client = ChatClient(endpoint, None, http, usage=app.dispatch.usage)
        for _ in range(2):
            await client.complete([], [])
    assert [row[6] for row in usage_rows(app)] == [0, 0]
    assert caplog.text.count("No USD price is configured for a direct DeepSeek chat model.") == 1


@pytest.mark.parametrize(("month", "day", "expected"), [
    (10, 1, 0.60),  # Thursday public holiday.
    (11, 28, 0.60),  # Saturday.
    (11, 30, 1.20),  # Monday.
])
def test_deepseek_holidays_and_weekends_use_off_peak_rates(month, day, expected):
    from dom.usage import _deepseek_cost

    at = datetime(2026, month, day, 8, tzinfo=timezone.utc)
    assert _deepseek_cost("deepseek-flash", {"completion_tokens": 1_000_000}, at) == pytest.approx(expected)

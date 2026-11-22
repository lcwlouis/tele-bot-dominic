import httpx
import pytest

from dom.config import ModelEndpoint
from dom.llm import ChatClient, FallbackClient

OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


async def _nosleep(_):
    return None


def _client(handler, http, name):
    return ChatClient(ModelEndpoint(base_url=f"http://{name}/v1", model=name, api_key_env=None), None, http, sleep=_nosleep)


async def test_unreachable_primary_falls_back():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "home":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        assert (await client.complete([], []))["content"] == "hi"
    assert calls == ["home", "cloud"]


async def test_primary_5xx_after_retries_falls_back():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503 if request.url.host == "home" else 200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        assert (await client.complete([], []))["content"] == "hi"
    assert calls == ["home", "home", "home", "cloud"]


async def test_primary_400_is_raised_not_hidden():
    def handler(request):
        return httpx.Response(400 if request.url.host == "home" else 200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete([], [])


async def test_no_fallback_configured_raises():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.ConnectError):
            await FallbackClient(_client(handler, http, "home"), None).complete([], [])


async def test_once_falls_back_with_a_single_call_each():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "home":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        assert (await client.complete([], [], once=True))["content"] == "hi"
    assert calls == ["home", "cloud"]


async def test_once_does_not_retry_the_fallback_either():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete([], [], once=True)
    assert calls == ["home", "cloud"]


async def test_once_400_is_raised_without_fallback():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(400, json={"error": "bad"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete([], [], once=True)
    assert calls == ["home"]


@pytest.mark.parametrize("status", [401, 402, 403, 404])
async def test_a_primary_this_key_may_not_use_falls_back(status):
    # An expired key or no credit on the primary used to fail the owner's turn three times with no fallback tried.
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status, json={"error": "no"}) if request.url.host == "home" else httpx.Response(200, json=OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FallbackClient(_client(handler, http, "home"), _client(handler, http, "cloud"))
        await client.complete([], [], once=True)
    assert calls == ["home", "cloud"]


async def test_chain_uses_saved_order_and_records_serving_provider(make_app):
    from dom.config import Config
    from dom.llm import routed_client
    from dom.turn_log import TurnTrace, track
    from test_usage import usage_rows

    app = make_app()
    config = Config(home=app.config.home, models={
        "chat": {"base_url": "http://localhost:8000/v1", "model": "primary", "fallbacks": [
            {"base_url": "https://first.example.test/v1", "model": "first"},
        ]},
        "fallback": {"base_url": "https://second.example.test/v1", "model": "second", "fallbacks": [
            {"base_url": "https://third.example.test/v1", "model": "third"},
        ]},
    })
    attempts = []

    def handler(request):
        attempts.append(request.url.host)
        if request.url.host == "localhost":
            raise httpx.ReadTimeout("offline", request=request)
        statuses = {"first.example.test": 402, "second.example.test": 503, "third.example.test": 200}
        return httpx.Response(statuses[request.url.host], json=OK)

    trace = TurnTrace()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = routed_client(config, "chat", http, app.dispatch.usage)
        with track(trace):
            assert (await client.complete([], [], once=True))["content"] == "hi"
        assert client.last_model == "third"
        assert client.last_fallback_at is not None
        assert (await client.complete_on_fallback([], [], once=True))["content"] == "hi"

    assert attempts == ["localhost", "first.example.test", "second.example.test", "third.example.test",
                        "first.example.test", "second.example.test", "third.example.test"]
    assert [(call.model, call.status, call.fallback) for call in trace.model_calls] == [
        ("primary", "ReadTimeout", False), ("first", 402, True), ("second", 503, True), ("third", 200, True),
    ]
    assert trace.model_calls[0].host == "localhost:8000"
    assert trace.model_calls[-1].host == "third.example.test"
    assert usage_rows(app)[2][:3] == ("chat", "third", "third.example.test")


@pytest.mark.parametrize("role", ["chat", "fallback", "media", "drafting"])
async def test_role_chain_checks_spend_before_advancing(role, make_app):
    from dom.config import Config
    from dom.llm import routed_client
    from dom.usage import ModelCallSkipped, model_request

    app = make_app()
    config = Config(home=app.config.home, models={
        role: {"base_url": "https://primary.example.test/v1", "model": "one", "fallbacks": [
            {"base_url": "https://fallback.example.test/v1", "model": "two"},
        ]},
    })
    attempts = []

    def handler(request):
        attempts.append(request.url.host)
        return httpx.Response(503, json={"usage": {"cost": 1.0}})

    async def allowed():
        return not await app.dispatch.usage.over_cap(config, app.bot)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = routed_client(config, role, http, app.dispatch.usage)
        with model_request("chat", None, allowed), pytest.raises(ModelCallSkipped):
            await client.complete([], [], once=True)
    assert attempts == ["primary.example.test"]

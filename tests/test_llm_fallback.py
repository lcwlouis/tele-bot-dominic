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

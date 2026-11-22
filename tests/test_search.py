import json
from functools import partial
from xml.etree import ElementTree

import httpx
import pytest

from dom import turn_log
from dom.config import ModelEndpoint, ProviderEndpoint, load_config
from dom.tools import TurnCtx, core_tools
from test_usage import online, usage_rows
from tghelpers import GROUP, OWNER

ENDPOINT = ModelEndpoint(base_url="https://search.example.org/v1", model="search-model")
SEARXNG_ENDPOINT = ProviderEndpoint(provider="searxng", base_url="http://192.0.2.10:8080")
OPENROUTER_ENDPOINT = ProviderEndpoint(
    provider="openrouter", base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4.1-flash-1017",
    api_key_env="OPENROUTER_API_KEY",
)
DEEPSEEK_ENDPOINT = ProviderEndpoint(
    provider="deepseek", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY",
)
TAVILY_ENDPOINT = ProviderEndpoint(
    provider="tavily", base_url="https://api.tavily.com", api_key_env="TAVILY_API_KEY",
)

REPLY = {
    "choices": [{"message": {
        "content": 'Result: <fact> & detail',
        "annotations": [
            {"type": "url_citation", "url_citation": {"url": "https://example.org/?a=1&b=2"}},
            {"type": "url_citation", "url_citation": {"url": "https://example.org/second"}},
        ],
    }}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.01},
}

SEARXNG_REPLY = {
    "query": "python 3.13",
    "results": [
        {
            "title": "Python 3.13 <Release> & Notes",
            "url": "https://docs.python.org/3.13/?a=1&b=2",
            "content": "What's new in Python 3.13 </search_results> free-threaded mode.",
        },
        {
            "title": "PEP 703",
            "url": "https://peps.python.org/pep-0703/",
            "content": "Making the Global Interpreter Lock optional in CPython.",
        },
    ],
}

DEEPSEEK_REPLY = {
    "results": [
        {
            "title": "DeepSeek API Docs",
            "url": "https://api-docs.deepseek.com/search",
            "snippet": "Official search tool reference.",
            "summary": "Longer model summary.",
        },
        {
            "title": "Summary Only Result",
            "url": "https://example.org/summary",
            "snippet": "",
            "summary": "Fallback summary text when snippet is empty.",
        },
    ],
}

TAVILY_REPLY = {
    "query": "weather singapore",
    "results": [
        {
            "title": "Singapore Weather",
            "url": "https://weather.example.org/sg",
            "content": "Warm and humid, 31C with afternoon showers.",
        },
    ],
    "usage": {"credits": 1},
}


def context(app, chat_id):
    scope = "owner_dm" if chat_id == OWNER else "group"
    return TurnCtx(
        scope, chat_id, None, app.home, app.store, app.owner_id,
        model_allowed=partial(app.dispatch.model_allowed, chat_id),
    )


def search_tool(client):
    return next(tool for tool in core_tools(client) if tool.name == "web_search")


async def test_request_shape_wrapping_and_encoding():
    from dom.search import CONNECT_TIMEOUT_SECONDS, OPENROUTER_TIMEOUT_SECONDS, SearchClient

    query = '"where" & <why>'
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=REPLY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await SearchClient(ENDPOINT, "fake", http).search(query)
    request, = requests
    assert request.url == "https://search.example.org/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer fake"
    assert request.extensions["timeout"]["read"] == OPENROUTER_TIMEOUT_SECONDS
    assert request.extensions["timeout"]["connect"] == CONNECT_TIMEOUT_SECONDS
    assert json.loads(request.content) == {
        "model": "search-model",
        "messages": [{"role": "user", "content": f"Search the web and answer concisely with the key facts: {query}"}],
        "plugins": [{"id": "web", "max_results": 3}],
    }
    wrapped = ElementTree.fromstring(result)
    assert wrapped.tag == "search_results"
    assert wrapped.attrib == {"untrusted": "true", "query": query}
    assert wrapped.text == (
        "Result: <fact> & detail\nSources: https://example.org/?a=1&b=2, https://example.org/second"
    )


async def test_searxng_request_shape_wrapping_escaping_and_usage(make_app):
    from dom.search import CONNECT_TIMEOUT_SECONDS, SEARCH_TIMEOUT_SECONDS, SearchClient

    app = make_app()
    await online(app)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=SEARXNG_REPLY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SearchClient(SEARXNG_ENDPOINT, None, http, usage=app.dispatch.usage)
        assert client.available is True
        result = await search_tool(client).handler({"query": "python 3.13"}, context(app, GROUP))

    request, = requests
    assert request.method == "GET"
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"q": "python 3.13", "format": "json"}
    assert request.headers["Accept"] == "application/json"
    assert "Authorization" not in request.headers
    assert request.extensions["timeout"]["read"] == SEARCH_TIMEOUT_SECONDS
    assert request.extensions["timeout"]["connect"] == CONNECT_TIMEOUT_SECONDS

    assert len(result) <= 2000
    assert result.count("</search_results>") == 1
    assert "&lt;/search_results&gt;" in result
    wrapped = ElementTree.fromstring(result)
    assert wrapped.tag == "search_results"
    assert wrapped.attrib == {"untrusted": "true", "query": "python 3.13"}
    assert "1. Python 3.13 <Release> & Notes" in wrapped.text
    assert "https://docs.python.org/3.13/?a=1&b=2" in wrapped.text
    assert "2. PEP 703" in wrapped.text

    rows = usage_rows(app)
    assert len(rows) == 1
    assert rows[0][:4] == ("search", "searxng", "192.0.2.10:8080", GROUP)
    assert rows[0][6] == 0.0  # self-hosted SearXNG costs nothing
    assert rows[0][7] == 0  # primary (not fallback)


async def test_deepseek_search_request_shape_truncation_and_billing(make_app):
    from dom.search import DEEPSEEK_QUERY_LIMIT, DEEPSEEK_SEARCH_COST_USD, SEARCH_TIMEOUT_SECONDS, SearchClient

    app = make_app()
    await online(app)
    requests = []
    long_query = "deep " * 40  # > 128 chars

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=DEEPSEEK_REPLY)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SearchClient(DEEPSEEK_ENDPOINT, "fake", http, usage=app.dispatch.usage)
        result = await search_tool(client).handler({"query": long_query}, context(app, OWNER))
        empty = await search_tool(client).handler({"query": "no-hits"}, context(app, OWNER))

    first, second = requests
    assert first.method == "POST"
    assert str(first.url) == "https://api.deepseek.com/v1/tools/search"
    assert first.headers["Authorization"] == "Bearer fake"
    assert first.extensions["timeout"]["read"] == SEARCH_TIMEOUT_SECONDS
    body = json.loads(first.content)
    assert body == {"query": long_query.strip()[:DEEPSEEK_QUERY_LIMIT], "count": 5, "need_content": False}
    assert len(body["query"]) == DEEPSEEK_QUERY_LIMIT

    wrapped = ElementTree.fromstring(result)
    assert "1. DeepSeek API Docs" in wrapped.text
    assert "Official search tool reference." in wrapped.text
    assert "Fallback summary text when snippet is empty." in wrapped.text
    assert "No results found." in ElementTree.fromstring(empty).text

    rows = usage_rows(app)
    assert len(rows) == 2
    assert rows[0][:4] == ("search", "deepseek-search", "api.deepseek.com", OWNER)
    assert rows[0][6] == pytest.approx(DEEPSEEK_SEARCH_COST_USD)
    assert rows[1][6] == 0.0  # DeepSeek does not bill empty results


async def test_tavily_request_shape_wrapping_and_usage(make_app):
    from dom.search import SEARCH_TIMEOUT_SECONDS, TAVILY_CREDIT_COST_USD, SearchClient

    app = make_app()
    await online(app)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=TAVILY_REPLY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SearchClient(TAVILY_ENDPOINT, "fake", http, usage=app.dispatch.usage)
        result = await search_tool(client).handler({"query": "weather singapore"}, context(app, OWNER))

    request, = requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == "Bearer fake"
    assert request.extensions["timeout"]["read"] == SEARCH_TIMEOUT_SECONDS
    assert json.loads(request.content) == {
        "query": "weather singapore",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_usage": True,
    }

    wrapped = ElementTree.fromstring(result)
    assert wrapped.tag == "search_results"
    assert "1. Singapore Weather" in wrapped.text
    assert "https://weather.example.org/sg" in wrapped.text
    assert "Warm and humid" in wrapped.text

    rows = usage_rows(app)
    assert len(rows) == 1
    assert rows[0][:4] == ("search", "tavily", "api.tavily.com", OWNER)
    assert rows[0][6] == pytest.approx(TAVILY_CREDIT_COST_USD)


@pytest.mark.parametrize("failure_mode", ["connect_error", "timeout", "http_503", "http_403", "invalid_json", "unresponsive"])
async def test_router_falls_through_when_searxng_fails_or_times_out(make_app, failure_mode):
    from dom.search import SearchClient, SearchRouter

    app = make_app()
    await online(app)
    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.host == "192.0.2.10":
            if failure_mode == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if failure_mode == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure_mode == "http_503":
                return httpx.Response(503, text="down")
            if failure_mode == "http_403":
                return httpx.Response(403, text="json disabled")
            if failure_mode == "invalid_json":
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(200, json={"results": [], "unresponsive_engines": [["duckduckgo", "timeout"]]})
        assert request.url.host == "openrouter.ai"
        assert json.loads(request.content)["provider"] == {"zdr": True}
        return httpx.Response(200, json=REPLY)

    trace = turn_log.TurnTrace()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = SearchRouter(
            SearchClient(SEARXNG_ENDPOINT, None, http, usage=app.dispatch.usage),
            SearchClient(OPENROUTER_ENDPOINT, "or-key", http, usage=app.dispatch.usage),
        )
        with turn_log.track(trace):
            result = await search_tool(router).handler({"query": "latest news"}, context(app, GROUP))

    assert seen == ["192.0.2.10", "openrouter.ai"]
    assert "Result: <fact> & detail" in ElementTree.fromstring(result).text
    assert len(trace.model_calls) == 2
    assert trace.model_calls[0].model == "searxng" and trace.model_calls[0].fallback is False
    assert trace.model_calls[1].model == "deepseek/deepseek-v4.1-flash-1017" and trace.model_calls[1].fallback is True
    assert trace.fallback is True

    # Answering provider is attributed with fallback=1 and its cost.
    openrouter_rows = [row for row in usage_rows(app) if row[1] == "deepseek/deepseek-v4.1-flash-1017"]
    assert len(openrouter_rows) == 1
    assert openrouter_rows[0][2] == "openrouter.ai"
    assert openrouter_rows[0][6] == pytest.approx(0.01)
    assert openrouter_rows[0][7] == 1


async def test_router_stops_at_searxng_when_it_succeeds_and_skips_missing_keys(make_app):
    from dom.search import SearchClient, SearchRouter

    app = make_app()
    await online(app)
    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.host == "192.0.2.10":
            return httpx.Response(200, json=SEARXNG_REPLY)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(200, json=DEEPSEEK_REPLY)
        pytest.fail(f"unexpected host {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        router = SearchRouter(
            SearchClient(SEARXNG_ENDPOINT, None, http, usage=app.dispatch.usage),
            SearchClient(TAVILY_ENDPOINT, None, http, usage=app.dispatch.usage),  # missing key
            SearchClient(DEEPSEEK_ENDPOINT, "ds-key", http, usage=app.dispatch.usage),
        )
        # Primary succeeds: cloud fallback is never called.
        res = await search_tool(router).handler({"query": "python"}, context(app, GROUP))
        assert "Python 3.13" in res
        assert seen == ["192.0.2.10"]

    # If primary is down and middle provider has no key, router skips middle and uses DeepSeek.
    seen.clear()

    def down_then_deepseek(request):
        seen.append(request.url.host)
        if request.url.host == "192.0.2.10":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=DEEPSEEK_REPLY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down_then_deepseek)) as http:
        router = SearchRouter(
            SearchClient(SEARXNG_ENDPOINT, None, http, usage=app.dispatch.usage),
            SearchClient(TAVILY_ENDPOINT, None, http, usage=app.dispatch.usage),  # missing key
            SearchClient(DEEPSEEK_ENDPOINT, "ds-key", http, usage=app.dispatch.usage),
        )
        res = await search_tool(router).handler({"query": "python"}, context(app, GROUP))
        assert "DeepSeek API Docs" in res
        assert seen == ["192.0.2.10", "api.deepseek.com"]


async def test_wrapping_keeps_long_escaped_result_under_limit():
    from dom.search import SearchClient

    payload = {"choices": [{"message": {"content": "<>&" * 3000}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
        result = await SearchClient(ENDPOINT, "fake", http).search('"<&' * 500)
    assert len(result) <= 2000
    assert ElementTree.fromstring(result).tag == "search_results"

    long_searxng = {"results": [{"title": "<>&" * 300, "url": "https://example.org/" + "a" * 300, "content": "<>&" * 500}] * 10}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=long_searxng))) as http:
        result2 = await SearchClient(SEARXNG_ENDPOINT, None, http).search('"<&' * 500)
    assert len(result2) <= 2000
    assert ElementTree.fromstring(result2).tag == "search_results"


async def test_tool_limit_is_per_turn_and_calls_record_usage(make_app):
    from dom.capabilities import describe
    from dom.search import SearchClient

    app = make_app()
    await online(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=REPLY))) as http:
        app.search_client = SearchClient(ENDPOINT, "fake", http, usage=app.dispatch.usage)
        tool = search_tool(app.search_client)
        ctx = context(app, GROUP)
        for _ in range(2):
            assert "search_results" in await tool.handler({"query": "books"}, ctx)
        assert (await tool.handler({"query": "books"}, ctx)).startswith("error:")
        assert "search_results" in await search_tool(app.search_client).handler({"query": "books"}, ctx)
        assert "web_search" in describe(app, "group")
        assert len(describe(app, "group")) <= 600
    assert [row[0:4] for row in usage_rows(app)] == [
        ("search", "search-model", "search.example.org", GROUP),
    ] * 3


async def test_spend_cap_refuses_group_but_not_owner_and_gate_is_checked(make_app):
    from dom.search import SearchClient, SearchRouter

    app = make_app()
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1})
    await online(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=REPLY))) as http:
        client = SearchRouter(SearchClient(ENDPOINT, "fake", http, usage=app.dispatch.usage))
        assert await search_tool(client).handler({"query": "books"}, context(app, GROUP)) == "search unavailable right now"
        assert "search_results" in await search_tool(client).handler({"query": "books"}, context(app, OWNER))
        await app.store.set_chat_auth_status(OWNER, "blocked")
        assert await search_tool(client).handler({"query": "books"}, context(app, OWNER)) == "search unavailable right now"
    assert [row[3] for row in usage_rows(app) if row[0] == "search"] == [OWNER]


async def test_timeout_and_http_errors_keep_dom_error_strings(make_app):
    from dom.search import SearchClient, SearchRouter

    app = make_app()
    await online(app)

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        tool = search_tool(SearchRouter(
            SearchClient(SEARXNG_ENDPOINT, None, http),
            SearchClient(ENDPOINT, "fake", http),
        ))
        assert await tool.handler({"query": "books"}, context(app, GROUP)) == "search failed: request timed out"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
        tool = search_tool(SearchRouter(SearchClient(SEARXNG_ENDPOINT, None, http)))
        assert await tool.handler({"query": "books"}, context(app, GROUP)) == "search failed: HTTP 503"


async def test_search_requires_config_and_key_except_searxng(tmp_path, monkeypatch):
    from dom.cli import search_client

    config_path = tmp_path / "config.toml"
    config_path.write_text("")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as http:
        client = search_client(load_config(tmp_path), http)
        assert client is None
        assert "web_search" not in [t.name for t in core_tools(client)]

        config_path.write_text("[models.search]\n")
        monkeypatch.delenv("OPENROUTER_API_KEY")
        assert search_client(load_config(tmp_path), http) is None
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
        client = search_client(load_config(tmp_path), http)
        assert client.endpoint.model == "deepseek/deepseek-v4.1-flash-1017"
        assert set(search_tool(client).scopes) == {"group", "owner_dm", "friend_dm"}

        # SearXNG primary works without any API key and does not inherit OpenRouter defaults.
        monkeypatch.delenv("OPENROUTER_API_KEY")
        config_path.write_text(
            '[models.search]\nprovider = "searxng"\nbase_url = "http://192.0.2.10:8080"\n'
            '[[models.search.fallbacks]]\nprovider = "tavily"\nbase_url = "https://api.tavily.com"\napi_key_env = "TAVILY_API_KEY"\n'
        )
        cfg = load_config(tmp_path)
        assert cfg.models["search"].api_key_env is None
        assert cfg.models["search"].model == ""
        client = search_client(cfg, http)
        assert client is not None and client.available is True
        assert [p.provider for p in client.providers] == ["searxng"]  # Tavily has no key, so it is left out


@pytest.mark.parametrize(("base_url", "zdr"), [
    ("https://openrouter.ai/api/v1", True),
    ("https://api.openrouter.ai/v1", True),
    ("https://search.example.org/v1", False),
])
async def test_search_sends_zdr_routing_only_for_openrouter(base_url, zdr):
    from dom.search import SearchClient

    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=REPLY)

    endpoint = ModelEndpoint(base_url=base_url, model="search-model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await SearchClient(endpoint, "fake", http).search("hello")
    body, = seen
    assert (body.get("provider") == {"zdr": True}) is zdr


CHAT = {"base_url": "https://llm.example.test/v1", "model": "m"}


@pytest.mark.parametrize("search", [
    {"base_url": "https://openrouter.ai/api/v1"},
    {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    {"base_url": "https://openrouter.ai/api/v1", "model": "custom/model"},
])
def test_pre_v082_search_tables_still_take_starter_defaults(tmp_path, search):
    from dom.config import starter_config, validate_config

    starter = starter_config()["models"]["search"]
    endpoint = validate_config(tmp_path, {"models": {"chat": CHAT, "search": search}}).models["search"]
    assert endpoint.model == search.get("model", starter["model"])
    assert endpoint.api_key_env == starter["api_key_env"]


def test_routed_client_leaves_out_providers_without_their_key(tmp_path, monkeypatch):
    from dom.config import validate_config
    from dom.search import routed_search_client

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = validate_config(tmp_path, {"models": {"chat": CHAT, "search": {
        "provider": "searxng", "base_url": "http://192.0.2.10:8080",
        "fallbacks": [{"provider": "tavily", "base_url": "https://api.tavily.com", "api_key_env": "TAVILY_API_KEY"}],
    }}})
    router = routed_search_client(config, httpx.AsyncClient())
    assert [provider.provider for provider in router.providers] == ["searxng"]

    only_keyless = validate_config(tmp_path, {"models": {"chat": CHAT, "search": {
        "provider": "tavily", "base_url": "https://api.tavily.com", "api_key_env": "TAVILY_API_KEY",
    }}})
    assert routed_search_client(only_keyless, httpx.AsyncClient()) is None


async def test_chain_stops_at_its_overall_deadline(monkeypatch):
    import asyncio

    from dom import search
    from dom.search import SearchClient, SearchRouter

    monkeypatch.setattr(search, "CHAIN_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(search, "SEARCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(search, "MIN_ATTEMPT_SECONDS", 0.05)
    seen = []

    async def hang(request):
        seen.append(request.url.host)
        await asyncio.sleep(5)

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as http:
        router = SearchRouter(
            SearchClient(SEARXNG_ENDPOINT, None, http),
            SearchClient(DEEPSEEK_ENDPOINT, "ds-key", http),
            SearchClient(TAVILY_ENDPOINT, "tv-key", http),
        )
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TimeoutError):
            await router.search("python")
        assert loop.time() - start < 0.6
    assert seen == ["192.0.2.10", "api.deepseek.com"]

import json
from functools import partial
from xml.etree import ElementTree

import httpx
import pytest

from dom.config import ModelEndpoint, load_config
from dom.tools import TurnCtx, core_tools
from test_usage import online, usage_rows
from tghelpers import GROUP, OWNER

ENDPOINT = ModelEndpoint(base_url="https://search.example.org/v1", model="search-model")
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


def context(app, chat_id):
    scope = "owner_dm" if chat_id == OWNER else "group"
    return TurnCtx(
        scope, chat_id, None, app.home, app.store, app.owner_id,
        model_allowed=partial(app.dispatch.model_allowed, chat_id),
    )


def search_tool(client):
    return next(tool for tool in core_tools(client) if tool.name == "web_search")


async def test_request_shape_wrapping_and_encoding():
    from dom.search import SearchClient

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
    assert request.extensions["timeout"]["read"] == 30
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


async def test_wrapping_keeps_long_escaped_result_under_limit():
    from dom.search import SearchClient

    payload = {"choices": [{"message": {"content": "<>&" * 3000}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
        result = await SearchClient(ENDPOINT, "fake", http).search('"<&' * 500)
    assert len(result) <= 2000
    assert ElementTree.fromstring(result).tag == "search_results"


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
    from dom.search import SearchClient

    app = make_app()
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1})
    await online(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=REPLY))) as http:
        client = SearchClient(ENDPOINT, "fake", http, usage=app.dispatch.usage)
        assert await search_tool(client).handler({"query": "books"}, context(app, GROUP)) == "search unavailable right now"
        assert "search_results" in await search_tool(client).handler({"query": "books"}, context(app, OWNER))
        await app.store.set_chat_auth_status(OWNER, "blocked")
        assert await search_tool(client).handler({"query": "books"}, context(app, OWNER)) == "search unavailable right now"
    assert [row[3] for row in usage_rows(app) if row[0] == "search"] == [OWNER]


async def test_timeout_is_an_error_result(make_app):
    from dom.search import SearchClient

    app = make_app()
    await online(app)

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        tool = search_tool(SearchClient(ENDPOINT, "fake", http))
        assert await tool.handler({"query": "books"}, context(app, GROUP)) == "search failed: request timed out"


async def test_search_requires_config_and_key(tmp_path, monkeypatch):
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

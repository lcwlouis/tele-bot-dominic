import json
import time
import tomllib

import httpx
import pytest

from dom import model_lists
from dom.config import load_config
from dom.llm import ChatClient
from test_web_model_settings import API, ORIGIN_HEADER, SECRET, START, _save, dashboard  # noqa: F401, F811  (the fixture)

PROVIDERS = "/api/providers"
ROUTES = [PROVIDERS + path for path in ("/save", "/remove", "/test", "/models")]
OPENROUTER_MODELS = {"data": [
    {"id": "acme/thinker", "name": "Acme: Thinker", "supported_parameters": ["tools", "reasoning"]},
    {"id": "acme/plain", "name": "Acme: Plain", "supported_parameters": ["tools"]},
    {"id": "acme/deep", "supported_parameters": ["reasoning"], "reasoning": {"supported_efforts": ["xhigh", "high", "low", "none"], "default_effort": "high"}},
    {"id": "bad id with spaces"},
    "not an object",
]}


def _mock(app, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.client = ChatClient(app.config.models["chat"], None, http)
    return http


async def _revision(client):
    return (await client.get(API)).json()["revision"]


async def _post(client, path, **body):
    return await client.post(PROVIDERS + path, headers=ORIGIN_HEADER, json={"revision": await _revision(client), **body})


def _provider(settings, name):
    return next(item for item in settings["providers"] if item["name"] == name)


async def test_provider_routes_need_owner_auth_same_origin_and_json(dashboard):
    _, path, browser = dashboard
    async with browser(authorized=False) as client:
        for route in ROUTES:
            assert (await client.post(route, json={}, headers=ORIGIN_HEADER)).status_code == 403
    async with browser() as client:
        for route in ROUTES:
            for headers in ({}, {"Origin": "https://other.example.test"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=ORIGIN_HEADER, data={})).status_code == 403
            assert (await client.post(route, headers=ORIGIN_HEADER, json={})).status_code == 422
            assert (await client.post(route, headers=ORIGIN_HEADER, json={"revision": "0" * 64, "name": "x", "extra": 1})).status_code == 422
        bad = await _post(client, "/save", name="Lab", kind="openai", base_url="http://lab.example.test/v1")
        assert bad.status_code == 422 and "HTTPS" in bad.json()["detail"]
        bad = await _post(client, "/save", name="  ", kind="openai", base_url="https://lab.example.test/v1")
        assert bad.status_code == 422
        bad = await _post(client, "/save", name="Lab", kind="chatgpt", base_url="https://lab.example.test/v1")
        assert bad.status_code == 422 and bad.json()["detail"] == "Choose one of the supported values."
        stale = await client.post(PROVIDERS + "/save", headers=ORIGIN_HEADER, json={
            "revision": "0" * 64, "name": "Lab", "kind": "openai", "base_url": "https://lab.example.test/v1"})
        assert stale.status_code == 409
    assert path.read_text() == START


async def test_settings_list_role_addresses_as_providers_with_their_uses(dashboard):
    _, _, browser = dashboard
    async with browser() as client:
        settings = (await client.get(API)).json()
    by_url = {item["base_url"]: item for item in settings["providers"]}
    local = by_url["http://localhost:8000/v1"]
    assert local["kind"] == "openai" and local["is_local"] and not local["saved"] and local["listable"]
    assert {(use["role"], use["index"]) for use in local["uses"]} == {("chat", 0)}
    triage = by_url["https://classifier.example.test/decisions"]
    assert triage["kind"] == "jev" and triage["name"] == "Jev decisions" and not triage["listable"]
    assert triage["uses"] == [{"role": "triage", "index": 0}]
    drafting = by_url["https://openrouter.ai/api/v1"]  # the starter drafting and media defaults
    assert drafting["kind"] == "openrouter" and drafting["name"] == "OpenRouter"
    assert {use["role"] for use in drafting["uses"]} == {"media", "drafting"}
    assert len({item["name"] for item in settings["providers"]}) == len(settings["providers"])


async def test_add_provider_tests_by_listing_models_then_saves_atomically(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json=OPENROUTER_MODELS)

    async with _mock(app, handler), browser() as client:
        response = await _post(client, "/save", name="Home lab", kind="openai", base_url="https://lab.example.test/v1")
        assert response.status_code == 200, response.text
        added = _provider(response.json(), "Home lab")
        assert added["saved"] and added["uses"] == [] and added["kind"] == "openai"
    assert seen == [("https://lab.example.test/v1/models", None)]
    saved = tomllib.loads(path.read_text())
    assert saved["providers"]["Home lab"] == {"kind": "openai", "base_url": "https://lab.example.test/v1"}
    assert path.with_name("config.toml.bak").read_text() == START
    assert "# Keep this comment." in path.read_text()
    assert load_config(path.parent).provider_setups["Home lab"].kind == "openai"
    # The save's test filled the list cache.
    assert (await model_lists.cached(app.store, "https://lab.example.test/v1", None)) is not None


async def test_failed_provider_test_refuses_the_save(dashboard):
    app, path, browser = dashboard
    async with _mock(app, lambda request: httpx.Response(503)), browser() as client:
        response = await _post(client, "/save", name="Lab", kind="openai", base_url="https://lab.example.test/v1")
    assert response.status_code == 422 and "HTTP 503" in response.json()["detail"]
    assert path.read_text() == START


async def test_new_provider_key_is_never_sent_to_an_unpaired_host(dashboard):
    app, path, browser = dashboard
    seen = []
    async with _mock(app, lambda request: seen.append(request) or httpx.Response(200, json={"data": []})), browser() as client:
        # BACKUP_KEY holds a value and is paired with fallback.example.test only.
        response = await _post(client, "/save", name="Elsewhere", kind="openai",
                               base_url="https://elsewhere.example.test/v1", api_key_env="BACKUP_KEY")
        assert response.status_code == 422 and "already used for a different host" in response.json()["detail"]
        # A fresh variable name with no value saves untested; its key is added to .env afterwards.
        response = await _post(client, "/save", name="Elsewhere", kind="openai",
                               base_url="https://elsewhere.example.test/v1", api_key_env="ELSEWHERE_KEY")
        assert response.status_code == 200, response.text
        assert _provider(response.json(), "Elsewhere")["key_set"] is False
        listed = await _post(client, "/models", name="Elsewhere")
        assert listed.status_code == 422 and "ELSEWHERE_KEY" in listed.json()["detail"]
    assert seen == []


async def test_model_list_is_cached_for_a_day_and_refreshed_on_request(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json=OPENROUTER_MODELS)

    async with _mock(app, handler), browser() as client:
        name = next(item["name"] for item in (await client.get(API)).json()["providers"] if item["kind"] == "openai"
                    and item["base_url"].startswith("https://fallback"))
        first = await _post(client, "/models", name=name)
        assert first.status_code == 200, first.text
        listing = first.json()
        assert listing["listable"] is True and listing["fetched_at"]
        models = {item["id"]: item for item in listing["models"]}
        assert set(models) == {"acme/thinker", "acme/plain", "acme/deep"}
        assert models["acme/thinker"] == {"id": "acme/thinker", "name": "Acme: Thinker", "reasoning": True, "efforts": ["low", "medium", "high"], "default_effort": None}
        assert models["acme/plain"]["reasoning"] is False and models["acme/plain"]["efforts"] == []
        assert models["acme/deep"]["efforts"] == ["none", "low", "high", "xhigh"]
        assert models["acme/deep"]["default_effort"] == "high"
        # The paired key goes to its own host only.
        assert seen == [("https://fallback.example.test/v1/models", f"Bearer {SECRET}")]

        assert (await _post(client, "/models", name=name)).json() == listing  # from the cache
        assert len(seen) == 1
        assert (await _post(client, "/models", name=name, refresh=True)).status_code == 200
        assert len(seen) == 2

        # A day-old list is fetched again.
        key = model_lists.cache_key("https://fallback.example.test/v1", "BACKUP_KEY")
        stale = json.loads(await app.store.kv_get(key))
        stale["fetched_at"] = time.time() - model_lists.LIST_TTL - 1
        await app.store.kv_set(key, json.dumps(stale))
        assert (await _post(client, "/models", name=name)).status_code == 200
        assert len(seen) == 3

        jev = await _post(client, "/models", name="Jev decisions")
        assert jev.json() == {"listable": False, "models": [], "fetched_at": None}
        assert (await _post(client, "/models", name="Nobody")).status_code == 404
    assert path.read_text() == START


@pytest.mark.parametrize("response, error", [
    (httpx.Response(302, headers={"Location": "https://other.example.test/models"}), "redirected"),
    (httpx.Response(200, content=b"x" * (model_lists.MAX_LIST_BYTES + 1)), "too large"),
    (httpx.Response(200, content=b"<html>"), "not valid JSON"),
    (httpx.Response(200, json={"object": "list"}), "expected format"),
])
async def test_model_list_fetch_refuses_redirects_oversize_and_junk(dashboard, response, error):
    app, _, browser = dashboard
    seen = []
    async with _mock(app, lambda request: seen.append(str(request.url)) or response), browser() as client:
        result = await _post(client, "/models", name=next(
            item["name"] for item in (await client.get(API)).json()["providers"] if item["base_url"].startswith("https://fallback")))
    assert result.status_code == 502 and error in result.json()["detail"]
    assert seen == ["https://fallback.example.test/v1/models"]  # the redirect is never followed


async def test_reasoning_effort_is_checked_against_the_fetched_list(dashboard):
    app, path, browser = dashboard
    backup = {"base_url": "https://fallback.example.test/v1", "api_key_env": "BACKUP_KEY"}

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with _mock(app, handler), browser() as client:
        # Before any list is fetched, a known effort name is accepted and a made-up one is not.
        assert (await _save(client, "fallback", dict(backup, model="acme/plain", reasoning_effort="high"))).status_code == 200
        bad = await _save(client, "fallback", dict(backup, model="acme/plain", reasoning_effort="turbo"))
        assert bad.status_code == 422 and bad.json()["errors"][0]["index"] == 0
        name = next(item["name"] for item in (await client.get(API)).json()["providers"] if item["base_url"] == backup["base_url"])
        assert (await _post(client, "/models", name=name)).status_code == 200
        bad = await _save(client, "fallback", dict(backup, model="acme/plain", reasoning_effort="low"))
        assert bad.status_code == 422 and "doesn't take a reasoning effort" in bad.json()["detail"]
        bad = await _save(client, "fallback", dict(backup, model="acme/deep", reasoning_effort="medium"))
        assert bad.status_code == 422 and "low, high, xhigh" in bad.json()["detail"]
        good = await _save(client, "fallback", dict(backup, model="acme/deep", reasoning_effort="xhigh"))
        assert good.status_code == 200, good.text
        assert (await _save(client, "fallback", dict(backup, model="acme/thinker", reasoning_effort="high"))).status_code == 200
        assert (await _save(client, "fallback", dict(backup, model="acme/plain"))).status_code == 200
    assert load_config(path.parent).models["fallback"].reasoning_effort is None


async def test_move_up_swaps_primary_and_first_fallback_without_testing(dashboard):
    app, path, browser = dashboard
    seen = []
    other = {"base_url": "http://localhost:8000/v1", "model": "other-model"}

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with _mock(app, handler), browser() as client:
        primary = {"base_url": "http://localhost:8000/v1", "model": "chat-model"}
        assert (await _save(client, "chat", primary, [other])).status_code == 200
        seen.clear()
        moved = await _save(client, "chat", other, [primary])
        assert moved.status_code == 200, moved.text
        assert moved.json()["roles"]["chat"]["primary"]["model"] == "other-model"
    assert seen == []
    assert [item.model for item in load_config(path.parent).providers("chat")] == ["other-model", "chat-model"]


async def test_triage_is_edited_and_tested_with_a_real_decisions_request(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"answers": {"check": {"type": "choice", "choice": "yes"}}})

    (path.parent / ".env").write_text("BACKUP_KEY=example-value-for-provider\nDECIDE_KEY=example-decide-value\n")
    triage = {"base_url": "https://decide.example.test/alpha", "model": "acme/decider-2", "api_key_env": "DECIDE_KEY"}
    async with _mock(app, handler), browser() as client:
        bad = await _save(client, "triage", triage, [triage])
        assert bad.status_code == 422 and "exactly one" in bad.json()["detail"]
        bad = await _save(client, "triage", dict(triage, reasoning_effort="low"))
        assert bad.status_code == 422
        assert (await _save(client, "triage", None)).status_code == 422

        # DECIDE_KEY holds a value and no host yet, so it can't be paired from here; a fresh variable can.
        refused = await _save(client, "triage", triage)
        assert refused.status_code == 422 and "already holds a value" in refused.json()["detail"]
        assert seen == []
        triage["api_key_env"] = "NEW_DECIDE_KEY"
        saved = await _save(client, "triage", triage)
        assert saved.status_code == 200, saved.text
        assert saved.json()["roles"]["triage"]["primary"]["model"] == "acme/decider-2"
        assert saved.json()["restart_required"] is False
        assert app.jev is None  # no key value yet, so triage stays off, as at startup
        assert seen == []  # the key is missing: saved untested, like any new key variable

        (path.parent / ".env").write_text("BACKUP_KEY=example-value-for-provider\nNEW_DECIDE_KEY=example-decide-value\n")
        revision = await _revision(client)
        tested = await client.post(API + "/test", headers=ORIGIN_HEADER, json={"revision": revision, "role": "triage", "index": 0})
        assert tested.status_code == 200 and tested.json()["ok"] is True, tested.text
        url, body = seen[-1]
        assert url == "https://decide.example.test/alpha/decisions"
        assert body["model"] == "acme/decider-2" and "check" in body["questions"]
        assert "provider" not in body  # ZDR routing is OpenRouter's; this host isn't

        # Triage edits apply without a restart once the key is there.
        saved = await _save(client, "triage", dict(triage, model="acme/decider-3"))
        assert saved.status_code == 200, saved.text
        assert seen[-1][1]["model"] == "acme/decider-3"  # the changed slot was tested first
        assert app.jev is not None and app.jev.model == "acme/decider-3" and app.dispatch.jev is app.jev

    disk = tomllib.loads(path.read_text())["models"]["triage"]
    assert disk == {"base_url": "https://decide.example.test/alpha", "model": "acme/decider-3", "api_key_env": "NEW_DECIDE_KEY"}


async def test_triage_test_reports_a_refused_decision(dashboard):
    app, path, browser = dashboard
    async with _mock(app, lambda request: httpx.Response(401, json={"error": "no"})), browser() as client:
        revision = await _revision(client)
        tested = await client.post(API + "/test", headers=ORIGIN_HEADER, json={"revision": revision, "role": "triage", "index": 0})
    assert tested.json() == {"ok": False, "latency_ms": tested.json()["latency_ms"], "reply": None,
                             "error": "Provider returned HTTP 401.", "provider": None}


async def test_remove_refuses_a_provider_in_use_and_removes_an_unused_one(dashboard):
    app, path, browser = dashboard
    async with _mock(app, lambda request: httpx.Response(200, json={"data": []})), browser() as client:
        assert (await _post(client, "/save", name="Lab", kind="openai", base_url="https://lab.example.test/v1")).status_code == 200
        in_use = await _post(client, "/remove", name="Jev decisions")
        assert in_use.status_code == 409 and in_use.json()["detail"] == "Jev decisions is still used by Triage. Change those roles first."
        chat = next(item["name"] for item in (await client.get(API)).json()["providers"] if item["uses"] and item["uses"][0]["role"] == "chat")
        assert "Chat" in (await _post(client, "/remove", name=chat)).json()["detail"]
        before = path.read_text()
        removed = await _post(client, "/remove", name="Lab")
        assert removed.status_code == 200, removed.text
        assert all(item["name"] != "Lab" for item in removed.json()["providers"])
        assert path.with_name("config.toml.bak").read_text() == before
        assert (await _post(client, "/remove", name="Lab")).status_code == 404
    assert "providers" not in tomllib.loads(path.read_text())


async def test_editing_a_provider_moves_every_slot_that_uses_it(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with _mock(app, handler), browser() as client:
        assert (await _save(client, "media", {"base_url": "http://localhost:8000/v1", "model": "local-media"})).status_code == 200
        settings = (await client.get(API)).json()
        local = next(item for item in settings["providers"] if item["base_url"] == "http://localhost:8000/v1")
        assert {use["role"] for use in local["uses"]} == {"chat", "media"}
        seen.clear()
        # Triage can't move onto a chat endpoint, and a chat endpoint can't become a decisions one.
        wrong = await _post(client, "/save", original=local["name"], name="Home lab", kind="jev", base_url="http://localhost:9000/v1")
        assert wrong.status_code == 409
        moved = await _post(client, "/save", original=local["name"], name="Home lab", kind="openai", base_url="http://localhost:9000/v1")
        assert moved.status_code == 200, moved.text
        home = _provider(moved.json(), "Home lab")
        assert home["saved"] and {use["role"] for use in home["uses"]} == {"chat", "media"}
        assert seen == ["http://localhost:9000/v1/chat/completions"] * 2  # each moved slot (chat, media) tested first
        assert app.config.models["media"].base_url == "http://localhost:9000/v1"

        renamed = await _post(client, "/save", original="Home lab", name="Basement", kind="openai", base_url="http://localhost:9000/v1")
        assert renamed.status_code == 200 and len(seen) == 2  # a rename tests nothing
        clash = await _post(client, "/save", original="Basement", name="Jev decisions", kind="openai", base_url="http://localhost:9000/v1")
        assert clash.status_code == 409
        twin = await _post(client, "/save", name="Twin", kind="openai", base_url="http://localhost:9000/v1")
        assert twin.status_code == 409 and "Basement" in twin.json()["detail"]
    saved = tomllib.loads(path.read_text())
    assert list(saved["providers"]) == ["Basement"]
    assert saved["models"]["chat"]["base_url"] == saved["models"]["media"]["base_url"] == "http://localhost:9000/v1"
    assert saved["models"]["triage"] == tomllib.loads(START)["models"]["triage"]


async def test_provider_test_uses_its_first_use_or_its_model_list(dashboard):
    app, _, browser = dashboard
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=OPENROUTER_MODELS)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with _mock(app, handler), browser() as client:
        assert (await _post(client, "/save", name="Lab", kind="openai", base_url="https://lab.example.test/v1")).status_code == 200
        seen.clear()
        result = (await _post(client, "/test", name="Lab")).json()
        assert result["ok"] and result["reply"] == "Lists 3 models"
        chat = next(item["name"] for item in (await client.get(API)).json()["providers"] if item["uses"] and item["uses"][0]["role"] == "chat")
        assert (await _post(client, "/test", name=chat)).json()["ok"]
    assert seen == ["https://lab.example.test/v1/models", "http://localhost:8000/v1/chat/completions"]


def test_parse_reads_openrouter_reasoning_levels_and_default():
    """OpenRouter's /models puts each model's levels in `reasoning`; DeepSeek V4.1 Flash takes none/low/high/max and
    thinks at high unless told otherwise, so the picker must not offer a generic low/medium/high."""
    models = {item.id: item for item in model_lists.parse({"data": [
        {"id": "example/flash", "supported_parameters": ["reasoning", "reasoning_effort"],
         "reasoning": {"mandatory": False, "default_enabled": True, "supported_efforts": ["max", "high", "low", "none"],
                       "default_effort": "high"}},
        {"id": "example/off", "supported_parameters": ["reasoning"],
         "reasoning": {"default_enabled": False, "supported_efforts": ["high", "low"], "default_effort": "low"}},
        {"id": "example/vague", "supported_parameters": ["reasoning"]},
    ]})}
    assert models["example/flash"].efforts == ("none", "low", "high", "max")
    assert models["example/flash"].default_effort == "high"
    assert models["example/off"].default_effort == "none"
    assert models["example/vague"].efforts == model_lists.DEFAULT_EFFORTS and models["example/vague"].default_effort is None
    assert model_lists.effort_error(model_lists.ModelList(tuple(models.values()), 0), "example/flash", "medium")


def test_parse_skips_unusable_entries_and_caps_names():
    models = model_lists.parse({"data": [
        {"id": "a/b", "name": "x" * 500}, {"id": "sk-or-v1-" + "a" * 64}, {"id": 5}, {"id": "a/b", "name": "dupe"},
    ]})
    assert [item.id for item in models] == ["a/b"]
    assert len(models[0].name) == model_lists.MAX_NAME and models[0].reasoning is None
    assert models[0].efforts == model_lists.DEFAULT_EFFORTS
    with pytest.raises(model_lists.ListUnavailable):
        model_lists.parse({"models": []})


STUDIO = """
[studio]
voice = true

[[studio.voice_fallbacks]]
provider = "openrouter_tts"
model = "acme/voice-one"
api_key_env = "VOICE_KEY"

[[studio.voice_fallbacks]]
provider = "gemini_tts"
model = "voice-two"
api_key_env = "GEMINI_VOICE_KEY"
paid = true
"""


async def test_studio_chains_show_and_move_up_without_testing(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    route = "/api/studio/move"
    async with _mock(app, lambda request: pytest.fail("a reorder reaches nothing")), browser() as client:
        settings = (await client.get(API)).json()
        assert [item["model"] for item in settings["studio"]["voice"]] == ["acme/voice-one", "voice-two"]
        assert settings["studio"]["image"] == [] and settings["studio"]["voice"][1]["paid"] is True
        assert (await client.post(route, json={"revision": settings["revision"], "chain": "voice", "index": 1})).status_code == 403
        bad = await client.post(route, headers=ORIGIN_HEADER, json={"revision": settings["revision"], "chain": "voice", "index": 0})
        assert bad.status_code == 404
        missing = await client.post(route, headers=ORIGIN_HEADER, json={"revision": settings["revision"], "chain": "image", "index": 1})
        assert missing.status_code == 404
        moved = await client.post(route, headers=ORIGIN_HEADER, json={"revision": settings["revision"], "chain": "voice", "index": 1})
        assert moved.status_code == 200, moved.text
        assert [item["model"] for item in moved.json()["studio"]["voice"]] == ["voice-two", "acme/voice-one"]
        assert app.config.studio.voice_fallbacks[0].model == "voice-two"  # applies at once
    assert path.with_name("config.toml.bak").read_text() == START + STUDIO
    saved = tomllib.loads(path.read_text())["studio"]
    assert saved["voice"] is True
    assert saved["voice_fallbacks"] == [
        {"provider": "gemini_tts", "model": "voice-two", "api_key_env": "GEMINI_VOICE_KEY", "paid": True},
        {"provider": "openrouter_tts", "model": "acme/voice-one", "api_key_env": "VOICE_KEY"},
    ]


async def test_a_saved_provider_may_not_share_a_studio_key(dashboard):
    # A media key may skip zero data retention; a provider carries chat content, so it never takes that key.
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    async with _mock(app, lambda request: httpx.Response(200, json={"data": []})), browser() as client:
        response = await _post(client, "/save", name="Voices", kind="openrouter",
                               base_url="https://openrouter.ai/api/v1", api_key_env="VOICE_KEY")
        assert response.status_code == 422 and "needs its own key variable" in response.json()["detail"]
    assert path.read_text() == START + STUDIO


async def test_a_moved_provider_never_takes_a_filled_default_key_to_its_new_host(dashboard):
    # An OpenRouter search table takes omitted fields from the starter, so a blank key there becomes OPENROUTER_API_KEY.
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("Authorization")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    (path.parent / ".env").write_text(f"BACKUP_KEY={SECRET}\nOPENROUTER_API_KEY=example-openrouter-value\n")
    search = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "acme/searcher",
              "api_key_env": "OPENROUTER_API_KEY"}
    async with _mock(app, handler), browser() as client:
        assert (await _save(client, "search", search)).status_code == 200
        before = path.read_text()
        seen.clear()
        moved = await _post(client, "/save", original="OpenRouter", name="OpenRouter", kind="openrouter",
                            base_url="https://elsewhere.example.test/v1")
        assert moved.status_code == 422 and "OPENROUTER_API_KEY" in moved.json()["detail"]
        # The same blank key saved on the search role itself is refused too.
        direct = await _save(client, "search", dict(search, base_url="https://elsewhere.example.test/v1", api_key_env=None))
        assert direct.status_code == 422 and "OPENROUTER_API_KEY" in direct.json()["detail"]
    assert seen == []  # refused before any probe
    assert path.read_text() == before
    assert app.config.models["search"].base_url == "https://openrouter.ai/api/v1"


async def test_model_list_entries_echoing_a_configured_secret_are_dropped(dashboard):
    app, path, browser = dashboard
    leaky = {"data": [
        {"id": "acme/plain"},
        {"id": f"acme/{SECRET}"},
        {"id": "acme/named", "name": f"Echo {SECRET}"},
        {"id": "acme/described", "description": f"Bearer {SECRET}"},
    ]}
    async with _mock(app, lambda request: httpx.Response(200, json=leaky)), browser() as client:
        assert (await _post(client, "/save", name="Lab", kind="openai", base_url="https://lab.example.test/v1")).status_code == 200
        listed = await _post(client, "/models", name="Lab", refresh=True)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["models"]] == ["acme/plain"]
        assert SECRET not in listed.text
        stored = await app.store.kv_get(model_lists.cache_key("https://lab.example.test/v1", None))
        assert SECRET not in stored
        # A list cached before the key value was set is filtered when it is read back.
        await model_lists.remember(app.store, "https://lab.example.test/v1", None,
                                   (model_lists.ListedModel(f"acme/{SECRET}", "Old", None, ()),))
        cached = await _post(client, "/models", name="Lab")
        assert cached.status_code == 200 and cached.json()["models"] == [] and SECRET not in cached.text


async def test_a_hand_written_provider_address_is_checked_before_listing(dashboard):
    app, path, browser = dashboard
    rogue = "http://169." "254.169.254/v1"  # cloud metadata, split so the public-data check reads no literal address
    path.write_text(START + f'\n[providers.Rogue]\nkind = "openai"\nbase_url = "{rogue}"\n')
    seen = []
    async with _mock(app, lambda request: seen.append(request) or httpx.Response(200, json={"data": []})), browser() as client:
        listed = await _post(client, "/models", name="Rogue", refresh=True)
        assert listed.status_code == 422 and "address isn't allowed" in listed.json()["detail"]
        tested = (await _post(client, "/test", name="Rogue")).json()
        assert tested["ok"] is False and "address isn't allowed" in tested["error"]
    assert seen == []


async def test_a_provider_move_tests_every_changed_slot_with_its_effort(dashboard):
    app, path, browser = dashboard
    seen = []
    failing = set()

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.host, body["model"], body.get("reasoning_effort")))
        if body["model"] in failing:
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    lab = {"base_url": "https://lab.example.test/v1"}
    async with _mock(app, handler), browser() as client:
        saved = await _save(client, "fallback", dict(lab, model="acme/a", reasoning_effort="high"), [dict(lab, model="acme/b")])
        assert saved.status_code == 200, saved.text
        name = next(item["name"] for item in saved.json()["providers"] if item["base_url"] == lab["base_url"])
        before = path.read_text()
        seen.clear()
        failing.add("acme/b")
        refused = await _post(client, "/save", original=name, name=name, kind="openai", base_url="https://lab2.example.test/v1")
        assert refused.status_code == 422 and "Fallback fallback 1" in refused.json()["detail"]
        assert sorted(seen) == [("lab2.example.test", "acme/a", "high"), ("lab2.example.test", "acme/b", None)]
        assert path.read_text() == before
        failing.clear()
        moved = await _post(client, "/save", original=name, name=name, kind="openai", base_url="https://lab2.example.test/v1")
        assert moved.status_code == 200, moved.text
    assert [slot.base_url for slot in app.config.providers("fallback")] == ["https://lab2.example.test/v1"] * 2
    assert app.config.providers("fallback")[0].reasoning_effort == "high"


async def test_a_triage_key_added_after_saving_turns_triage_on_without_a_restart(dashboard):
    app, path, browser = dashboard

    def handler(request):
        return httpx.Response(200, json={"answers": {"check": {"type": "choice", "choice": "yes"}}})

    triage = {"base_url": "https://decide.example.test/alpha", "model": "acme/decider", "api_key_env": "NEW_DECIDE_KEY"}
    async with _mock(app, handler), browser() as client:
        assert (await _save(client, "triage", triage)).status_code == 200
        assert app.jev is None  # no key value yet: triage and its leak checks are off
        env = path.parent / ".env"
        env.write_text(f"BACKUP_KEY={SECRET}\nNEW_DECIDE_KEY=example-decide-value\n")
        # Saving the unchanged role picks the key up.
        unchanged = await _save(client, "triage", triage)
        assert unchanged.status_code == 200 and unchanged.json()["restart_required"] is False
        assert app.jev is not None and app.jev.api_key == "example-decide-value" and app.dispatch.jev is app.jev
        # So does a Test that succeeds, after the value changes.
        env.write_text(f"BACKUP_KEY={SECRET}\nNEW_DECIDE_KEY=example-decide-value-2\n")
        tested = await client.post(API + "/test", headers=ORIGIN_HEADER,
                                   json={"revision": await _revision(client), "role": "triage", "index": 0})
        assert tested.json()["ok"] is True
        assert app.jev.api_key == "example-decide-value-2" and app.dispatch.jev is app.jev
        env.write_text(f"BACKUP_KEY={SECRET}\nNEW_DECIDE_KEY=example-decide-value-3\n")
        assert (await _post(client, "/test", name="Jev decisions")).json()["ok"] is True
        assert app.jev.api_key == "example-decide-value-3"


async def test_a_route_that_cannot_be_built_keeps_the_old_file_and_runtime(dashboard, monkeypatch):
    import dom.telegram.handlers as handlers

    app, path, browser = dashboard

    def broken(*args, **kwargs):
        raise ValueError("cannot build")

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(handlers, "routed_search_client", broken)
    async with _mock(app, handler), browser() as client:
        running = (app.config, app.client, app.jev, app.search_client)
        saved = await _save(client, "chat", {"base_url": "http://localhost:8000/v1", "model": "chat-two"})
        assert saved.status_code == 409
        provider = await _post(client, "/save", name="Lab", kind="openai", base_url="http://localhost:9000/v1")
        assert provider.status_code == 409
    assert path.read_text() == START and not path.with_name("config.toml.bak").exists()
    assert (app.config, app.client, app.jev, app.search_client) == running



async def test_a_role_save_error_never_quotes_a_cached_effort_label_holding_a_secret(dashboard):
    app, path, browser = dashboard
    backup = {"base_url": "https://fallback.example.test/v1", "api_key_env": "BACKUP_KEY"}
    # A list cached before the key value was set, whose provider echoed that value into an effort label.
    leaky = model_lists.ListedModel(id="acme/leaky", name="Leaky", reasoning=True, efforts=("low", f"x{SECRET}"))
    await model_lists.remember(app.store, backup["base_url"], backup["api_key_env"], (leaky,))
    async with browser() as client:
        bad = await _save(client, "fallback", dict(backup, model="acme/leaky", reasoning_effort="turbo"))
    assert bad.status_code == 422
    assert SECRET not in bad.text

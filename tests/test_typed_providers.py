"""Typed providers (E70) and the triage model picker (E71)."""

import tomllib

import httpx
import pytest
from pydantic import ValidationError

from dom import model_lists
from dom.config import ProviderSetup, load_config
from test_web_model_settings import API, ORIGIN_HEADER, SECRET, START, dashboard  # noqa: F401, F811  (the fixture)
from test_web_provider_settings import STUDIO, _mock, _post, _provider, _revision

DECISIONS = {"data": [
    {"id": "~typesafe/jev-latest", "name": "TypeSafe: Jev", "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001"}, "context_length": 32000},
    {"id": "perplexity/pplx-decider-v1.3:free", "name": "Perplexity: Decider (free)",
     "architecture": {"input_modalities": ["text"]}, "pricing": {"prompt": "0"}, "context_length": 128000},
]}
OPENROUTER_TRIAGE = """
[models.triage]
base_url = "https://openrouter.ai/api/alpha"
model = "~typesafe/jev-latest"
api_key_env = "OPENROUTER_API_KEY"
"""


def test_a_jev_table_from_before_types_loads_as_an_openrouter_decisions_provider():
    setup = ProviderSetup(kind="jev", base_url="https://openrouter.ai/api/alpha", api_key_env="OPENROUTER_API_KEY")
    assert (setup.kind, setup.type) == ("openrouter", "decisions")
    assert ProviderSetup(base_url="https://lab.example.test/v1").type == "chat"
    assert ProviderSetup(kind="openrouter", base_url="https://openrouter.ai/api/v1").type == "chat"
    assert ProviderSetup(kind="tavily", base_url="https://api.tavily.com").type == "search"
    for bad in ({"kind": "openai", "type": "decisions"}, {"kind": "gemini", "type": "chat"},
                {"kind": "searxng", "type": "chat"}, {"kind": "openai", "type": "search"}):
        with pytest.raises(ValidationError):
            ProviderSetup(base_url="https://lab.example.test/v1", **bad)
    with pytest.raises(ValidationError, match="fixed address"):
        ProviderSetup(kind="openrouter", type="media", base_url="https://openrouter.ai/api/alpha")
    assert ProviderSetup(kind="gemini", type="media", base_url="https://generativelanguage.googleapis.com/v1beta/").type == "media"


def test_an_old_config_with_a_jev_table_loads_unchanged(tmp_path):
    text = START + '\n[providers.Jev]\nkind = "jev"\nbase_url = "https://openrouter.ai/api/alpha"\napi_key_env = "JEV_API_KEY"\n'
    (tmp_path / "config.toml").write_text(text)
    config = load_config(tmp_path)
    assert (config.provider_setups["Jev"].kind, config.provider_setups["Jev"].type) == ("openrouter", "decisions")
    assert (tmp_path / "config.toml").read_text() == text


def test_a_media_provider_never_shares_a_chat_key(tmp_path):
    (tmp_path / "config.toml").write_text(START + """
[providers."OpenRouter media"]
kind = "openrouter"
type = "media"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "BACKUP_KEY"
""")
    with pytest.raises(ValidationError, match="media provider needs its own key variable"):
        load_config(tmp_path)


async def test_roles_and_backups_without_provider_tables_show_as_typed_providers(dashboard):
    # Dominic Dev's real config has no [providers] tables: every provider comes from a role or a studio backup.
    _, path, browser = dashboard
    path.write_text(START.replace(
        '[models.triage]\nbase_url = "https://classifier.example.test/decisions"\nmodel = "classifier"\n', "",
    ) + OPENROUTER_TRIAGE + STUDIO)
    async with browser() as client:
        settings = (await client.get(API)).json()
    kinds = {item["name"]: (item["kind"], item["type"], item["listable"], item["saved"]) for item in settings["providers"]}
    assert kinds["OpenRouter"] == ("openrouter", "chat", True, False)
    assert kinds["OpenRouter decisions"] == ("openrouter", "decisions", True, False)
    assert kinds["OpenRouter media"] == ("openrouter", "media", False, False)
    assert kinds["Google Gemini media"] == ("gemini", "media", False, False)
    assert _provider(settings, "OpenRouter decisions")["uses"] == [{"role": "triage", "index": 0}]
    assert _provider(settings, "OpenRouter media")["uses"] == [{"role": "voice", "index": 0}]
    assert _provider(settings, "Google Gemini media")["api_key_env"] == "GEMINI_VOICE_KEY"


async def test_openrouter_is_added_once_per_type_with_its_own_key(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=DECISIONS)

    async with _mock(app, handler), browser() as client:
        for name, type_, base, key in (
            ("Router chat", "chat", "https://openrouter.ai/api/v1", "ROUTER_CHAT_KEY"),
            ("Router media", "media", "https://openrouter.ai/api/v1", "ROUTER_MEDIA_KEY"),
            ("Router decisions", "decisions", "https://openrouter.ai/api/alpha", "ROUTER_CHAT_KEY"),
        ):
            response = await _post(client, "/save", name=name, kind="openrouter", type=type_, base_url=base, api_key_env=key)
            assert response.status_code == 200, response.text
        # A media key never serves chat, and a chat key never serves media.
        shared = await _post(client, "/save", name="Router media 2", kind="openrouter", type="media",
                             base_url="https://openrouter.ai/api/v1", api_key_env="ROUTER_CHAT_KEY")
        assert shared.status_code == 422 and "media provider needs its own key variable" in shared.json()["detail"]
        wrong = await _post(client, "/save", name="Elsewhere media", kind="openrouter", type="media",
                            base_url="https://openrouter.ai/api/v2")
        assert wrong.status_code == 422 and "fixed address" in wrong.json()["detail"]
        mismatch = await _post(client, "/save", name="Lab decisions", kind="openai", type="decisions",
                               base_url="https://lab.example.test/v1")
        assert mismatch.status_code == 422
    assert seen == []  # the new key variables have no value yet, so nothing was sent
    saved = tomllib.loads(path.read_text())["providers"]
    assert saved["Router chat"] == {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "ROUTER_CHAT_KEY"}
    assert saved["Router media"]["type"] == "media" and saved["Router decisions"]["type"] == "decisions"


async def test_triage_lists_openrouter_decision_models_cached_apart_from_chat(dashboard):
    app, path, browser = dashboard
    path.write_text(path.read_text() + OPENROUTER_TRIAGE.replace("models.triage", "models.drafting").replace(
        "api/alpha", "api/v1").replace("~typesafe/jev-latest", "acme/writer").replace("OPENROUTER_API_KEY", "BACKUP_ROUTER"))
    path.write_text(path.read_text().replace(
        '[models.triage]\nbase_url = "https://classifier.example.test/decisions"\nmodel = "classifier"\n',
        '[models.triage]\nbase_url = "https://openrouter.ai/api/alpha"\nmodel = "~typesafe/jev-latest"\n'))
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json=DECISIONS)

    async with _mock(app, handler), browser() as client:
        settings = (await client.get(API)).json()
        triage = next(item for item in settings["providers"] if item["type"] == "decisions")
        assert triage["listable"] and triage["name"] == "OpenRouter decisions"
        listed = await _post(client, "/models", name=triage["name"])
        assert listed.status_code == 200, listed.text
        models = {item["id"]: item for item in listed.json()["models"]}
        assert set(models) == {"~typesafe/jev-latest", "perplexity/pplx-decider-v1.3:free"}
        assert models["perplexity/pplx-decider-v1.3:free"]["reasoning"] is False
        assert models["perplexity/pplx-decider-v1.3:free"]["efforts"] == []
        assert seen == [("https://openrouter.ai/api/v1/models?output_modalities=decisions", None)]
        assert (await _post(client, "/models", name=triage["name"])).status_code == 200
        assert len(seen) == 1  # from the cache
        assert (await _post(client, "/models", name=triage["name"], refresh=True)).status_code == 200
        assert len(seen) == 2
    base = "https://openrouter.ai/api/alpha"
    assert model_lists.cache_key(base, None, "decisions") != model_lists.cache_key(base, None)
    assert await model_lists.cached(app.store, base, None, type_="decisions") is not None
    assert await model_lists.cached(app.store, base, None) is None


def test_decision_list_location_needs_openrouter():
    assert model_lists.list_location("openrouter", "decisions", "https://openrouter.ai/api/alpha") == (
        "https://openrouter.ai", model_lists.DECISIONS_LIST)
    assert model_lists.list_location("openrouter", "decisions", "https://classifier.example.test/decisions") is None
    assert model_lists.list_location("openrouter", "chat", "https://openrouter.ai/api/v1") == ("https://openrouter.ai/api/v1", "/models")
    assert model_lists.list_location("openrouter", "media", "https://openrouter.ai/api/v1") is None
    assert model_lists.list_location("searxng", "search", "http://localhost:8888") is None


@pytest.mark.parametrize("response, sentence", [
    (httpx.Response(404, json={"error": {"message": "No endpoints found matching your data policy"}}), "zero data retention"),
    (httpx.Response(403, json={"error": {"message": "forbidden"}}), "this key may not use this model"),
    (httpx.Response(402, json={"error": {"message": "credits"}}), "out of credits"),
    (httpx.Response(404, json={"error": {"message": "model not found"}}), "Pick one from the list"),
])
async def test_a_refused_triage_test_says_why_in_one_sentence(dashboard, response, sentence):
    app, _, browser = dashboard
    async with _mock(app, lambda request: response), browser() as client:
        tested = await client.post(API + "/test", headers=ORIGIN_HEADER,
                                   json={"revision": await _revision(client), "role": "triage", "index": 0})
    result = tested.json()
    assert result["ok"] is False and sentence in result["error"]


async def test_editing_a_media_provider_key_moves_its_backups(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    async with _mock(app, lambda request: pytest.fail("a key with no value is never sent")), browser() as client:
        settings = (await client.get(API)).json()
        media = _provider(settings, "OpenRouter media")
        chat = await _post(client, "/save", original=media["name"], name="Voices", kind="openrouter", type="chat",
                           base_url=media["base_url"], api_key_env="NEW_VOICE_KEY")
        assert chat.status_code == 409 and "Voice notes backup 1" in chat.json()["detail"]
        keyless = await _post(client, "/save", original=media["name"], name="Voices", kind="openrouter", type="media",
                              base_url=media["base_url"])
        assert keyless.status_code == 422
        moved = await _post(client, "/save", original=media["name"], name="Voices", kind="openrouter", type="media",
                            base_url=media["base_url"], api_key_env="NEW_VOICE_KEY")
        assert moved.status_code == 200, moved.text
        voices = _provider(moved.json(), "Voices")
        assert voices["saved"] and voices["uses"] == [{"role": "voice", "index": 0}]
        removed = await _post(client, "/remove", name="Voices")
        assert removed.status_code == 409 and "Voice notes backup 1" in removed.json()["detail"]
    saved = tomllib.loads(path.read_text())
    assert saved["studio"]["voice_fallbacks"][0]["api_key_env"] == "NEW_VOICE_KEY"
    assert saved["studio"]["voice_fallbacks"][1]["api_key_env"] == "GEMINI_VOICE_KEY"
    assert saved["providers"]["Voices"]["type"] == "media"
    assert app.config.studio.voice_fallbacks[0].api_key_env == "NEW_VOICE_KEY"


MEDIA_VALUE = "example-media-value"


async def test_a_media_provider_without_backups_is_tested_by_its_key(dashboard, monkeypatch):
    app, path, browser = dashboard
    monkeypatch.setenv("ROUTER_MEDIA_KEY", "")
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json={"data": {}})

    async with _mock(app, handler), browser() as client:
        saved = await _post(client, "/save", name="Router media", kind="openrouter", type="media",
                            base_url="https://openrouter.ai/api/v1", api_key_env="ROUTER_MEDIA_KEY")
        assert saved.status_code == 200, saved.text
        (path.parent / ".env").write_text(f"BACKUP_KEY={SECRET}\nROUTER_MEDIA_KEY={MEDIA_VALUE}\n")
        tested = await _post(client, "/test", name="Router media")
    assert tested.json()["ok"] is True and tested.json()["reply"] == "The key works."
    assert seen == [("https://openrouter.ai/api/v1/key", f"Bearer {MEDIA_VALUE}")]

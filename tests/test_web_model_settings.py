import json
import tomllib

import httpx
import pytest

from dom.config import Config, load_config
from dom.llm import ChatClient
from dom.web.app import create_app
from dom.web.auth import Auth
from test_usage import usage_rows
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN

API = "/api/models"
PRIMARY = {"base_url": "http://localhost:8000/v1", "model": "chat-model"}
BACKUP = {"base_url": "https://fallback.example.test/v1", "model": "backup-model", "api_key_env": "BACKUP_KEY"}
SECRET = "example-value-for-provider"
ORIGIN_HEADER = {"Origin": ORIGIN}
START = """\
# Keep the instance options and comments.
[models.chat]
base_url = "http://localhost:8000/v1"
model = "chat-model"

[models.chat_fallback]
base_url = "https://fallback.example.test/v1"
model = "backup-model"
api_key_env = "BACKUP_KEY"

[models.triage]
base_url = "https://classifier.example.test/decisions"
model = "classifier"

[custom]
stuff = [{ keep = true }] # Keep this comment.
"""


@pytest.fixture
def dashboard(make_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    path = tmp_path / "config.toml"
    path.write_text(START)
    (tmp_path / ".env").write_text(f"BACKUP_KEY={SECRET}\n")
    app = make_app()
    app.config = load_config(tmp_path)
    auth_config = Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})
    web = create_app(app, Auth(auth_config))

    def browser(*, authorized=True):
        headers = {"Authorization": f"Bearer {TOKEN}"} if authorized else {}
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN, headers=headers)

    return app, path, browser


async def test_settings_owner_auth_and_post_csrf(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.get(API)).status_code == 403
        assert (await client.get("/settings")).status_code == 403  # the old page is only a redirect, and not for strangers
        for route in (API, API + "/test"):
            assert (await client.post(route, json={}, headers=ORIGIN_HEADER)).status_code == 403

    async with browser() as client:
        for route in (API, API + "/test"):
            for headers in ({}, {"Origin": "https://other.example.test"}, {"Referer": ORIGIN}, {"HX-Request": "true"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=POST_HEADERS, json={})).status_code == 422
            assert (await client.post(route, headers=ORIGIN_HEADER, json={})).status_code == 422
            assert (await client.post(route, headers=ORIGIN_HEADER, data={})).status_code == 403  # a form is not a JSON write
        settings = (await client.get(API)).json()
        assert settings["roles"]["fallback"]["primary"]["key_set"] is True and "drafting" in settings["roles"]
        assert SECRET not in json.dumps(settings)
        app.store.owner_id = None
        assert (await client.get(API)).status_code == 409
    assert path.read_text() == START


async def test_save_backs_up_config_and_applies_new_routes_without_restart(dashboard):
    app, path, browser = dashboard
    route = dict(PRIMARY, model="local-media")
    async with browser() as client:
        initial = (await client.get(API)).json()
        assert set(initial["roles"]) == {"chat", "fallback", "media", "drafting", "search", "escalation", "triage"}
        assert initial["roles"]["search"] == {"primary": None, "fallbacks": []}
        assert {item["id"] for item in initial["search_providers"]} == {"searxng", "openrouter", "deepseek", "tavily"}
        assert initial["roles"]["fallback"]["primary"]["key_set"] is True
        assert initial["roles"]["chat"]["primary"]["kind"] == "local"
        assert initial["roles"]["fallback"]["primary"]["kind"] == "cloud"
        triage = initial["roles"]["triage"]
        assert triage["fallbacks"] == [] and {key: triage["primary"][key] for key in ("model", "base_url", "api_key_env", "key_set")} == {
            "model": "classifier", "base_url": "https://classifier.example.test/decisions", "api_key_env": None, "key_set": False,
        }
        assert SECRET not in json.dumps(initial)
        update = {"revision": initial["revision"], "role": "media", "primary": route, "fallbacks": [BACKUP]}
        result = await client.post(API, headers=ORIGIN_HEADER, json=update)
        assert result.status_code == 200, result.text
        assert result.json()["restart_required"] is False
        assert result.json()["roles"]["media"]["fallbacks"][0]["model"] == "backup-model"
        assert app.media_describer is not None
        assert [p.endpoint.model for p in app.media_describer.client.providers] == ["local-media", "backup-model"]
        assert app.draft_client.primary.endpoint.model == "anthropic/claude-haiku-5.5"

        stale = await client.post(API, headers=ORIGIN_HEADER, json=update)
        assert stale.status_code == 409

    assert path.with_name("config.toml.bak").read_text() == START
    saved = tomllib.loads(path.read_text())
    assert saved["models"]["fallback"] == BACKUP
    assert "chat_fallback" not in saved["models"]
    assert saved["models"]["triage"] == tomllib.loads(START)["models"]["triage"]
    assert "# Keep this comment." in path.read_text()
    assert not list(path.parent.glob(".config.toml.*"))
    assert load_config(path.parent).providers("media")[1].api_key_env == "BACKUP_KEY"


async def test_failed_atomic_replace_keeps_valid_config_and_backup(dashboard, monkeypatch):
    import os

    _, path, browser = dashboard
    replace = os.replace

    def fail_config(src, dest):
        if dest == path:
            assert tomllib.loads(src.read_text())["models"]["chat"]["model"] == "replacement"
            assert path.read_text() == START
            raise OSError("simulated rename failure")
        return replace(src, dest)

    monkeypatch.setattr(os, "replace", fail_config)
    async with browser() as client:
        revision = (await client.get(API)).json()["revision"]
        response = await client.post(API, headers=ORIGIN_HEADER, json={
            "revision": revision, "role": "chat", "primary": dict(PRIMARY, model="replacement"), "fallbacks": [],
        })
        assert response.status_code == 409
    assert path.read_text() == START
    assert path.with_name("config.toml.bak").read_text() == START
    assert not list(path.parent.glob(".config.toml.*"))


@pytest.mark.parametrize(("role", "primary"), [
    ("triage", PRIMARY),
    ("chat", None),
    ("chat", dict(PRIMARY, base_url="file:///tmp/db")),
    ("chat", dict(PRIMARY, base_url="https://user:pass@example.test/v1")),
    ("chat", dict(PRIMARY, base_url="https://example.test/v1?token=value")),
    ("chat", dict(PRIMARY, base_url="https://example.test:99999/v1")),
    ("chat", dict(PRIMARY, base_url="https://example.test:0/v1")),
    ("chat", dict(PRIMARY, base_url="http:///v1")),
    ("chat", dict(PRIMARY, model="")),
    ("chat", dict(PRIMARY, api_key=SECRET)),
    ("chat", dict(PRIMARY, api_key_env=SECRET)),
])
async def test_bad_settings_are_rejected_without_echoing_values(dashboard, role, primary):
    _, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get(API)).json()["revision"]
        result = await client.post(API, headers=ORIGIN_HEADER, json={
            "revision": revision, "role": role, "primary": primary, "fallbacks": [],
        })
    assert result.status_code == 422
    assert SECRET not in result.text
    assert path.read_text() == START and not path.with_name("config.toml.bak").exists()


async def test_test_call_uses_only_saved_provider_and_blocks_redirects(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "fallback.example.test"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert json.loads(request.content) == {
            "model": "backup-model", "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 256,
        }
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://other.example.test/v1"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": f"OK {SECRET}"}}], "usage": {"cost": 0.04},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        revision = (await client.get(API)).json()["revision"]
        test = {"revision": revision, "role": "fallback", "index": 0}
        for bad, status in ((dict(test, base_url="https://other.example.test"), 422), (dict(test, index=1), 404),
                            (dict(test, revision="a" * 64), 409)):
            assert (await client.post(API + "/test", headers=ORIGIN_HEADER, json=bad)).status_code == status
        assert seen == []
        redirect = (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()
        assert redirect["ok"] is False and redirect["error"] == "Provider returned HTTP 302."
        result = (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()
        assert result["ok"] and result["reply"] == "OK [redacted]" and result["latency_ms"] >= 0
    assert len(seen) == 2
    assert usage_rows(app)[-1][:3] == ("chat", "backup-model", "fallback.example.test")
    assert usage_rows(app)[-1][-2] == 0.04
    assert path.read_text() == START


async def test_escalation_settings_test_explicit_and_inherited_models(dashboard, monkeypatch):
    app, path, browser = dashboard
    monkeypatch.setenv("OPENROUTER_API_KEY", "example-openrouter-key")
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert request.url.host == "openrouter.ai" and body["provider"] == {"zdr": True}
        seen.append(body["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        settings = (await client.get(API)).json()
        assert settings["roles"]["escalation"]["primary"] is None
        test = {"revision": settings["revision"], "role": "escalation", "index": 0}
        assert (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()["ok"]

        update = {
            "revision": settings["revision"], "role": "escalation", "fallbacks": [],
            "primary": {"base_url": "https://openrouter.ai/api/v1", "model": "bigger-model",
                        "api_key_env": "OPENROUTER_API_KEY"},
        }
        saved = await client.post(API, headers=ORIGIN_HEADER, json=update)
        assert saved.status_code == 200 and not saved.json()["restart_required"]
        assert app.escalation_client.primary.endpoint.model == "bigger-model"
        test["revision"] = saved.json()["revision"]
        assert (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()["ok"]

        update.update(revision=test["revision"], primary=None)
        cleared = await client.post(API, headers=ORIGIN_HEADER, json=update)
        assert cleared.status_code == 200 and not cleared.json()["restart_required"]
        assert app.escalation_client.primary.endpoint.model == "anthropic/claude-haiku-5.5"
    assert "escalation" not in tomllib.loads(path.read_text())["models"]
    assert seen == ["anthropic/claude-haiku-5.5", "bigger-model", "bigger-model"]
    assert [(row[0], row[1], row[3]) for row in usage_rows(app)] == [
        ("escalation", model, app.owner_id) for model in seen
    ]


async def test_probe_obeys_spend_cap_and_rejects_invalid_legacy_url(dashboard):
    app, path, browser = dashboard
    await app.dispatch.usage.record("chat", "other", "localhost", {"cost": 1.0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected provider request"))) as http:
        app.client = ChatClient(app.config.models["chat"], None, http)
        async with browser() as client:
            revision = (await client.get(API)).json()["revision"]
            test = {"revision": revision, "role": "fallback", "index": 0}
            result = (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()
            assert result["error"] == "The daily spend cap has been reached."

            path.write_text(START.replace(BACKUP["base_url"], "file:///tmp/db"))
            test["revision"] = (await client.get(API)).json()["revision"]
            result = (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).json()
            assert result["error"] == "Fix this provider's URL, model or key variable in settings."


async def test_provider_timeout_does_not_hold_config_for_another_request(dashboard, monkeypatch):
    import asyncio

    from dom.web import model_settings

    app, _, browser = dashboard
    started = asyncio.Event()
    unblock = asyncio.Event()

    async def handler(request):
        assert request.url.host == "fallback.example.test"
        started.set()
        await unblock.wait()
        raise httpx.ReadTimeout("internal provider details", request=request)

    monkeypatch.setattr(model_settings, "TEST_TIMEOUT", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        revision = (await client.get(API)).json()["revision"]
        test = {"revision": revision, "role": "fallback", "index": 0}
        request = asyncio.create_task(client.post(API + "/test", headers=ORIGIN_HEADER, json=test))
        await asyncio.wait_for(started.wait(), 0.5)
        try:
            result = await asyncio.wait_for(client.post(API, headers=ORIGIN_HEADER, json={
                "revision": revision, "role": "fallback", "primary": None, "fallbacks": [],
            }), timeout=0.5)
            assert result.status_code == 200
            assert result.json()["roles"]["fallback"]["primary"] is None
            assert not request.done()
        finally:
            unblock.set()
        result = (await request).json()
        assert result["error"] == "Provider test timed out."


COLLECTOR = {"base_url": "https://collector.example.test/v1", "model": "m"}


async def _save(client, role, primary, fallbacks=()):
    revision = (await client.get(API)).json()["revision"]
    return await client.post(API, headers=ORIGIN_HEADER, json={
        "revision": revision, "role": role, "primary": primary, "fallbacks": list(fallbacks),
    })


@pytest.mark.parametrize("name", ["TELEGRAM_BOT_TOKEN", "JEV_API_KEY", "KLIPY_API_KEY"])
async def test_instance_secret_is_never_paired_with_a_new_host(dashboard, monkeypatch, name):
    app, path, browser = dashboard
    monkeypatch.setenv(name, "bot-token-value")
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        result = await _save(client, "media", dict(COLLECTOR, api_key_env=name))
        assert result.status_code == 422 and "edit config.toml by hand" in result.text
        assert "bot-token-value" not in result.text
        assert path.read_text() == START
        revision = (await client.get(API)).json()["revision"]
        # nothing was saved, so there is no slot to test either
        test = {"revision": revision, "role": "media", "index": 1}
        assert (await client.post(API + "/test", headers=ORIGIN_HEADER, json=test)).status_code == 404
    assert hosts == []  # no request reached the collector


async def test_existing_key_moves_to_a_new_host_only_by_hand(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        moved = dict(COLLECTOR, api_key_env="BACKUP_KEY")  # paired with fallback.example.test
        result = await _save(client, "media", moved)
        assert result.status_code == 422 and "edit config.toml by hand" in result.text
        assert path.read_text() == START
        # the same key on the host it is already paired with is accepted
        again = await _save(client, "media", dict(BACKUP, model="other"))
        assert again.status_code == 200
        # one new key name for two hosts in one request is not
        two = await _save(client, "drafting", dict(COLLECTOR, api_key_env="NEW_KEY"),
                          [dict(PRIMARY, api_key_env="NEW_KEY")])
        assert two.status_code == 422
        # a new name for one host passes key pairing and saves untested: its value goes into .env afterwards
        # (it cannot hold one before, see above), and the slot cannot be called until it does
        from dom.config import ProviderEndpoint
        from dom.web.model_settings import _check_keys

        _check_keys(app.config, [ProviderEndpoint(**dict(COLLECTOR, api_key_env="NEW_KEY"))])
        new_key = await _save(client, "drafting", dict(COLLECTOR, api_key_env="NEW_KEY"))
        assert new_key.status_code == 200 and 'api_key_env = "NEW_KEY"' in path.read_text()


@pytest.mark.parametrize("base_url", [
    "http://collector.example.test/v1", "http://169." "254.169.254/v1", "https://169." "254.169.254/v1",
    "http://0.0.0.0:8000/v1", "https://224.0.0.1/v1", "http://[fe" "80::1]/v1", "http://[::ffff:169." "254.169.254]/v1",
    "http://8.8.8.8/v1", "http://openrouter.ai/api/v1",
    "http://28520" "39166/v1", "http://0xa9fe" "a9fe/v1",  # the cloud metadata address, in forms the resolver accepts
])
async def test_base_url_must_be_https_or_local_and_never_link_local(dashboard, base_url):
    _, path, browser = dashboard
    async with browser() as client:
        result = await _save(client, "chat", dict(PRIMARY, base_url=base_url))
    assert result.status_code == 422 and path.read_text() == START


@pytest.mark.parametrize(("base_url", "kind"), [
    ("http://localhost:8000/v1", "local"), ("http://127.0.0.1:8000/v1", "local"), ("http://10." "1.2.3:8000/v1", "local"),
    ("http://vllm:8000/v1", "local"), ("http://box." "lan/v1", "local"), ("http://box." "local/v1", "local"),
    ("http://gpu." "internal/v1", "local"), ("http://gpu." "home.arpa/v1", "local"), ("http://[::1]:8000/v1", "local"),
    ("https://llm.example.test/v1", "cloud"),
])
async def test_local_hosts_may_use_http_and_are_marked_local(dashboard, base_url, kind):
    _, _, browser = dashboard
    async with browser() as client:
        result = await _save(client, "chat", dict(PRIMARY, base_url=base_url))
    assert result.status_code == 200, result.text
    assert result.json()["roles"]["chat"]["primary"]["kind"] == kind


async def test_chain_is_capped_and_first_config_is_kept(dashboard):
    _, path, browser = dashboard
    extra = [dict(PRIMARY, model=f"m{n}") for n in range(6)]
    async with browser() as client:
        assert (await _save(client, "chat", PRIMARY, extra)).status_code == 422
        assert (await _save(client, "chat", PRIMARY, extra[:5])).status_code == 200
        first = path.with_name("config.toml.orig")
        assert first.read_text() == START
        assert (await _save(client, "chat", dict(PRIMARY, model="again"))).status_code == 200
    assert first.read_text() == START  # the original is not overwritten
    assert "m4" in path.with_name("config.toml.bak").read_text()


def test_fallbacks_are_rejected_on_triage_and_allowed_on_search(tmp_path):
    from dom.config import validate_config

    extra = {"base_url": "https://llm.example.test/v1", "model": "m", "fallbacks": [PRIMARY]}
    with pytest.raises(ValueError, match="cannot have fallbacks"):
        validate_config(tmp_path, {"models": {"chat": PRIMARY, "triage": extra}})
    cfg = validate_config(tmp_path, {"models": {
        "chat": PRIMARY,
        "search": {
            "provider": "searxng", "base_url": "http://localhost:8080",
            "fallbacks": [{"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "m"}],
        },
    }})
    assert [p.provider for p in cfg.providers("search")] == ["searxng", "openrouter"]


async def test_a_key_with_a_value_never_gains_a_host_even_after_its_slot_is_removed(dashboard, monkeypatch):
    """Re-check of PR #11: removing a key's only slot and re-adding the name under a new host laundered the key."""
    from dom.web.model_settings import _address

    _, path, browser = dashboard
    monkeypatch.setenv("SOME_OTHER_SECRET", "unrelated-value")
    async with browser() as client:
        assert (await _save(client, "fallback", None)).status_code == 200  # BACKUP_KEY's only slot is gone
        laundered = await _save(client, "media", dict(COLLECTOR, api_key_env="BACKUP_KEY"))
        assert laundered.status_code == 422 and "already holds a value" in laundered.text
        other = await _save(client, "media", dict(COLLECTOR, api_key_env="SOME_OTHER_SECRET"))
        assert other.status_code == 422 and "unrelated-value" not in other.text
        assert "BACKUP_KEY" not in path.read_text().split("[models.media]")[-1]
    # a key paired with https on a host doesn't follow it to plain http there
    nas = "nas." "lan"  # an example local name
    assert _address(f"https://{nas}/v1") != _address(f"http://{nas}/v1")


async def test_save_probes_only_changed_or_added_slots_and_refuses_on_failure(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "no-zdr-model":
            return httpx.Response(404, json={"error": {"message": "No endpoints found matching ZDR"}})
        if body["model"] == "broken-fallback":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        before_client = app.client
        # Saving unchanged chat primary + a failing added fallback tests only the added fallback.
        bad = await _save(client, "chat", PRIMARY, [dict(PRIMARY, model="broken-fallback")])
        assert bad.status_code == 422
        payload = bad.json()
        assert payload["errors"] == [{"index": 1, "error": "Provider returned HTTP 503."}]
        assert seen == ["broken-fallback"]
        assert path.read_text() == START and not path.with_name("config.toml.bak").exists()
        assert app.client is before_client

        # OpenRouter 404 "no endpoints" / ZDR refusal returns a clear ZDR error per failing slot.
        seen.clear()
        zdr_fail = await _save(
            client, "chat", dict(PRIMARY, model="no-zdr-model"), [dict(PRIMARY, model="broken-fallback")],
        )
        assert zdr_fail.status_code == 422
        errors = zdr_fail.json()["errors"]
        assert len(errors) == 2
        assert errors[0]["index"] == 0 and "zero data retention" in errors[0]["error"]
        assert errors[1] == {"index": 1, "error": "Provider returned HTTP 503."}
        assert path.read_text() == START and app.client is before_client

        # Removing the fallback needs no probe and saves immediately.
        seen.clear()
        assert (await _save(client, "fallback", None)).status_code == 200
        assert seen == []


async def test_probe_sends_reasoning_with_a_larger_budget_accepts_thinking_and_uses_app_sleep(dashboard, monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    app, _, browser = dashboard
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        # Thinking model returns empty content with reasoning field.
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "", "reasoning": "thinking"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        saved = await _save(
            client, "drafting",
            {"base_url": "https://openrouter.ai/api/v1", "model": "thinking-model", "reasoning_effort": "low"},
        )
        assert saved.status_code == 200, saved.text
        body, = seen
        assert body == {
            "model": "thinking-model",
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 2048,
            "provider": {"zdr": True},
            "reasoning": {"effort": "low"},
        }

        # Also check sleep=app.sleep is passed to over_cap's flood retry when cap is reached.
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        app.sleep = fake_sleep
        await app.dispatch.usage.record("chat", "other", "localhost", {"cost": 1.0})
        attempts = 0
        original_send = app.bot.send_message

        async def flood_once(chat_id, text, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TelegramRetryAfter(method=None, message="flood", retry_after=2)
            return await original_send(chat_id, text, **kw)

        monkeypatch.setattr(app.bot, "send_message", flood_once)
        revision = (await client.get(API)).json()["revision"]
        res = (await client.post(API + "/test", headers=ORIGIN_HEADER, json={
            "revision": revision, "role": "drafting", "index": 0,
        })).json()
        assert res["ok"] is False and res["error"] == "The daily spend cap has been reached."
        assert slept == [2.0] and attempts == 2


async def test_save_reprobes_a_changed_effort_but_not_a_slot_that_only_moved(dashboard):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        other = dict(PRIMARY, model="other-model")
        assert (await _save(client, "chat", PRIMARY, [other])).status_code == 200
        assert seen == ["other-model"]
        seen.clear()
        assert (await _save(client, "chat", other, [PRIMARY])).status_code == 200  # swapped
        assert seen == []
        # A new effort changes the request, so it is tested as it will be sent.
        assert (await _save(client, "chat", dict(other, reasoning_effort="low"), [PRIMARY])).status_code == 200
        assert seen == ["other-model"]


async def test_the_spend_cap_never_blocks_saving_a_local_endpoint(dashboard, monkeypatch):
    app, path, browser = dashboard
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async def over_cap(*args, **kwargs):
        return True

    monkeypatch.setattr(app.dispatch.usage, "over_cap", over_cap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        assert (await _save(client, "chat", dict(PRIMARY, model="local-two"))).status_code == 200  # localhost
        assert seen == ["local-two"]
        cloud = await _save(client, "chat", dict(COLLECTOR, model="cloud-two"))
        assert cloud.status_code == 422 and "spend cap" in cloud.text


LIVE_SHAPE = """\
# Instance options.
[models.chat]
base_url = "http://localhost:8000/v1"
model = "chat-model"

[models.chat_fallback]
base_url = "https://fallback.example.test/v1"
model = "backup-model"
api_key_env = "BACKUP_KEY"

[models.triage]
base_url = "https://classifier.example.test/decisions"
model = "classifier"

[models.media]
base_url = "http://localhost:8001/v1"
model = "media-model"  # Keep this comment.

[desktop]
enabled = false

[models.search]
base_url = "https://search.example.test/v1"
model = "search-model"

[web]
port = 8080

[t3]
note = "kept"

[openers]
note = "kept"
"""
OTHER = {"base_url": "http://localhost:8002/v1", "model": "other-model"}


def _live_save(tmp_path, role, primary, fallbacks):
    from dom.config import validate_config
    from dom.web.model_settings import ModelUpdate, Snapshot, _save, _write

    (tmp_path / "config.toml").write_text(LIVE_SHAPE)
    config = validate_config(tmp_path, tomllib.loads(LIVE_SHAPE))
    current = Snapshot(config, LIVE_SHAPE)
    update = ModelUpdate(revision=current.revision, role=role, primary=primary, fallbacks=fallbacks)
    saved = _save(current, update)
    _write(current, saved)
    return saved


@pytest.mark.parametrize("role", ["chat", "fallback", "media", "drafting", "search", "escalation"])
@pytest.mark.parametrize("operation", ["set", "set_with_fallbacks", "reorder"])
def test_saving_any_role_survives_tables_split_by_another_section(tmp_path, role, operation):
    # The live file has [desktop] between its [models.*] tables and still carries the legacy chat_fallback.
    first, second = dict(PRIMARY, model="first"), dict(OTHER, model="second")
    primary, fallbacks = {
        "set": (first, []),
        "set_with_fallbacks": (first, [second]),
        "reorder": (second, [first, dict(BACKUP)]),
    }[operation]
    saved = _live_save(tmp_path, role, primary, fallbacks)
    models = tomllib.loads(saved.text)["models"]
    assert "chat_fallback" not in models
    assert models[role]["model"] == primary["model"]
    assert [item["model"] for item in models[role].get("fallbacks", [])] == [item["model"] for item in fallbacks]
    assert models["triage"]["model"] == "classifier"
    if role != "search":
        assert models["search"]["model"] == "search-model"
    assert ("# Keep this comment." in saved.text) == (role != "media")  # replacing a role rewrites its own table
    assert "# Instance options." in saved.text
    assert tomllib.loads(saved.text)["desktop"] == {"enabled": False} and tomllib.loads(saved.text)["t3"] == {"note": "kept"}
    assert (tmp_path / "config.toml").read_text() == saved.text


def test_clearing_the_fallback_role_removes_the_legacy_table_too(tmp_path):
    saved = _live_save(tmp_path, "fallback", None, [])
    models = tomllib.loads(saved.text)["models"]
    assert "fallback" not in models and "chat_fallback" not in models
    assert models["chat"]["model"] == "chat-model" and models["media"]["model"] == "media-model"
    assert "# Keep this comment." in saved.text and tomllib.loads(saved.text)["web"] == {"port": 8080}


async def test_search_settings_round_trip_test_and_data_retention_display(dashboard):
    app, path, browser = dashboard
    searx = {"provider": "searxng", "base_url": "http://127.0.0.1:8888", "model": ""}
    openrouter = {
        "provider": "openrouter", "base_url": "https://fallback.example.test/v1",
        "model": "search-web", "api_key_env": "BACKUP_KEY",
    }
    deepseek = {
        "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
        "model": "", "api_key_env": "DEEPSEEK_API_KEY",
    }
    tavily = {
        "provider": "tavily", "base_url": "https://api.tavily.com",
        "model": "", "api_key_env": "TAVILY_API_KEY",
    }
    searx_down = False
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.url.host == "127.0.0.1":
            if searx_down:
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"results": [{"title": "A", "url": "https://a.example", "content": "ok"}]})
        if request.url.host == "fallback.example.test":
            return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})
        pytest.fail(f"unexpected request to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        providers = {item["id"]: item for item in (await client.get(API)).json()["search_providers"]}
        assert providers["tavily"]["policy_url"] == "https://www.tavily.com/privacy" and providers["tavily"]["policy_label"] == "Tavily data policy"
        assert providers["openrouter"]["data_retention"].startswith("Zero data retention") and providers["searxng"]["data_retention"].startswith("Self-hosted")

        # Save full chain: SearXNG + OpenRouter web (probed) + DeepSeek + Tavily (unset keys save untested).
        saved = await _save(client, "search", searx, [openrouter, deepseek, tavily])
        assert saved.status_code == 200, saved.text
        data = saved.json()
        assert data["restart_required"] is False
        assert data["roles"]["search"]["primary"]["provider"] == "searxng"
        assert data["roles"]["search"]["primary"]["kind"] == "local"
        assert [p["provider"] for p in data["roles"]["search"]["fallbacks"]] == ["openrouter", "deepseek", "tavily"]
        assert data["roles"]["search"]["fallbacks"][2]["policy_url"] == "https://www.tavily.com/privacy"
        assert app.search_client is not None
        # The live chain leaves out DeepSeek and Tavily until their keys exist; the saved config keeps all four.
        assert [p.provider for p in app.search_client.providers] == ["searxng", "openrouter"]

        # Round-trip reload from disk.
        reloaded = load_config(path.parent)
        assert [p.provider for p in reloaded.providers("search")] == ["searxng", "openrouter", "deepseek", "tavily"]
        assert "model" not in tomllib.loads(path.read_text())["models"]["search"]

        # Test individual slot reports which provider answered.
        revision = data["revision"]
        slot0 = (await client.post(API + "/test", headers=ORIGIN_HEADER, json={
            "revision": revision, "role": "search", "index": 0,
        })).json()
        assert slot0["ok"] is True and slot0["provider"] == "searxng" and "SearXNG" in slot0["reply"]

        # Test chain falls through when SearXNG is down and reports OpenRouter answered.
        searx_down = True
        chain = (await client.post(API + "/test", headers=ORIGIN_HEADER, json={
            "revision": revision, "role": "search", "index": None,
        })).json()
        assert chain["ok"] is True and chain["provider"] == "openrouter"
        assert "OpenRouter web" in chain["reply"] and "Fallback 1" in chain["reply"]

        # Clearing the search role removes it and disables app.search_client live.
        cleared = await _save(client, "search", None, [])
        assert cleared.status_code == 200
        assert cleared.json()["roles"]["search"] == {"primary": None, "fallbacks": []}
        assert app.search_client is None
        assert "search" not in tomllib.loads(path.read_text())["models"]


async def test_search_settings_validation_and_searxng_403_hint(dashboard):
    app, path, browser = dashboard

    def handler(request):
        return httpx.Response(403, text="Forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        # SearXNG 403 gives actionable JSON format hint and refuses save.
        bad_searx = await _save(client, "search", {"provider": "searxng", "base_url": "http://127.0.0.1:8888", "model": ""})
        assert bad_searx.status_code == 422 and "formats: [html, json]" in bad_searx.text
        assert path.read_text() == START

        # Invalid field combinations are rejected.
        for role, slot in (
            ("chat", dict(PRIMARY, provider="searxng")),
            ("search", {"provider": "searxng", "base_url": "http://127.0.0.1:8888", "model": "not-allowed"}),
            ("search", {"provider": "tavily", "base_url": "https://api.tavily.com", "model": ""}),
            ("search", {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": ""}),
            ("search", {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": ""}),
            ("search", {"provider": "searxng", "base_url": "http://127.0.0.1:8888", "model": "", "reasoning_effort": "low"}),
        ):
            res = await _save(client, role, slot)
            assert res.status_code == 422, (role, slot, res.text)
    assert path.read_text() == START


async def test_search_settings_spend_cap_allows_local_searxng_blocks_cloud_search(dashboard, monkeypatch):
    app, _, browser = dashboard

    def handler(request):
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, json={"results": []})
        pytest.fail("cloud search should be blocked by spend cap")

    async def over_cap(*args, **kwargs):
        return True

    monkeypatch.setattr(app.dispatch.usage, "over_cap", over_cap)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        local_save = await _save(client, "search", {"provider": "searxng", "base_url": "http://127.0.0.1:8888", "model": ""})
        assert local_save.status_code == 200, local_save.text
        cloud_save = await _save(client, "search", {
            "provider": "openrouter", "base_url": "https://fallback.example.test/v1",
            "model": "web", "api_key_env": "BACKUP_KEY",
        })
        assert cloud_save.status_code == 422 and "spend cap" in cloud_save.text


PERMISSIONS = "/api/permissions"


async def test_permissions_need_owner_auth_same_origin_and_json(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.get(PERMISSIONS)).status_code == 403
        assert (await client.post(PERMISSIONS, json={}, headers=ORIGIN_HEADER)).status_code == 403
    async with browser() as client:
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        body = {"revision": revision, "id": "leak_check", "enabled": False}
        for headers in ({}, {"Origin": "https://other.example.test"}, {"Referer": ORIGIN}, {"HX-Request": "true"}):
            assert (await client.post(PERMISSIONS, headers=headers, json=body)).status_code == 403
        assert (await client.post(PERMISSIONS, headers=ORIGIN_HEADER, data={})).status_code == 403
        for bad in (
            {**body, "id": "web"},  # only the listed switches, never an arbitrary config key
            {**body, "enabled": "false"},  # strict booleans
            {**body, "extra": 1},
            {"id": "leak_check", "enabled": False},
        ):
            response = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json=bad)
            assert response.status_code == 422, bad
            assert "web" not in response.json()["detail"] and "extra" not in response.json()["detail"]
        app.store.owner_id = None
        assert (await client.get(PERMISSIONS)).status_code == 409
    assert path.read_text() == START


async def test_permission_save_writes_config_and_applies_without_restart(dashboard):
    app, path, browser = dashboard
    path.write_text(START.replace("[custom]", "[openers]\nenabled = false  # keep me\nmax_per_day = 3\n\n[custom]"))
    app.config = load_config(path.parent)
    async with browser() as client:
        initial = (await client.get(PERMISSIONS)).json()
        assert {item["id"]: item["enabled"] for item in initial["items"]} == {
            "memory_self_edit": False, "openers": False, "openers_groups": False, "openers_friends": False,
            "ephemeral_replies": False, "leak_check": True,
            "studio_voice": False, "studio_video": False, "studio_images": False, "studio_special": False,
            "studio_profile": False,
        }
        assert initial["revision"] == (await client.get(API)).json()["revision"]
        result = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={"revision": initial["revision"], "id": "openers", "enabled": True})
        assert result.status_code == 200, result.text
        after = result.json()
        assert next(item for item in after["items"] if item["id"] == "openers")["enabled"] is True
        assert app.config.openers.enabled is True and app.config.openers.max_per_day == 3
        for switch in ("openers_groups", "openers_friends"):
            before = path.read_text()
            result = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
                "revision": after["revision"], "id": switch, "enabled": True,
            })
            assert result.status_code == 200, result.text
            after = result.json()
            assert path.with_name("config.toml.bak").read_text() == before
        assert app.config.openers.groups and app.config.openers.friends
        result = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={"revision": after["revision"], "id": "leak_check", "enabled": False})
        assert result.status_code == 200, result.text
        assert app.config.leak_check is False
        assert app.config.models == load_config(path.parent).models  # nothing else in the running config moved

        stale = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={"revision": initial["revision"], "id": "memory_self_edit", "enabled": True})
        assert stale.status_code == 409
        assert app.config.memory_self_edit is False

    text = path.read_text()
    saved = tomllib.loads(text)
    assert saved["openers"] == {"enabled": True, "max_per_day": 3, "groups": True, "friends": True}
    assert saved["leak_check"] is False
    assert "enabled = true  # keep me" in text and "# Keep this comment." in text
    assert path.with_name("config.toml.orig").read_text().startswith(START.splitlines()[0])


async def test_triage_view_names_its_missing_key_variable(dashboard):
    app, path, browser = dashboard
    path.write_text(START.replace('model = "classifier"', 'model = "classifier"\napi_key_env = "TRIAGE_KEY"'))
    async with browser() as client:
        triage = (await client.get(API)).json()["roles"]["triage"]["primary"]
    assert triage["api_key_env"] == "TRIAGE_KEY" and triage["key_set"] is False


async def test_a_permission_save_waits_on_the_disk_once_and_off_the_event_loop(dashboard, monkeypatch):
    import asyncio
    import os
    import threading
    import time

    _, path, browser = dashboard
    synced, threads = [], []
    real_fsync = os.fsync

    def slow_fsync(fd):  # an SD card under load
        synced.append(fd)
        threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    async with browser() as client:
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        save = asyncio.create_task(client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={"revision": revision, "id": "openers", "enabled": True}))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        assert (await client.get(PERMISSIONS)).status_code == 200  # served while the save waits on the disk
        assert time.monotonic() - started < 0.25 and not save.done()
        assert (await save).status_code == 200
    # Only config.toml itself is synced; the .orig and .bak copies are not worth a wait. None of it on the loop.
    assert len(synced) == 1 and threads == [False]
    assert tomllib.loads(path.read_text())["openers"]["enabled"] is True


@pytest.mark.parametrize(("permission", "field"), [
    ("studio_voice", "voice"), ("studio_video", "video_notes"), ("studio_images", "images"),
    ("studio_special", "special"), ("studio_profile", "profile_photo_follows_preset"),
])
async def test_studio_permission_save_applies_now(dashboard, permission, field):
    app, path, browser = dashboard
    path.write_text(START + '[studio]\nenabled = true\nbase_url = "https://studio.example.org"\n')
    async with browser() as client:
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        result = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
            "revision": revision, "id": permission, "enabled": True,
        })
    assert result.status_code == 200
    assert getattr(app.config.studio, field)
    assert tomllib.loads(path.read_text())["studio"] == {
        "enabled": True, "base_url": "https://studio.example.org", field: True,
    }


DESKTOP_STEPS = "/api/permissions/desktop-steps"


async def test_desktop_steps_need_owner_auth_same_origin_and_a_valid_budget(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.post(DESKTOP_STEPS, json={}, headers=ORIGIN_HEADER)).status_code == 403
    async with browser() as client:
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        body = {"revision": revision, "max_steps": 40}
        for headers in ({}, {"Origin": "https://other.example.test"}, {"Referer": ORIGIN}):
            assert (await client.post(DESKTOP_STEPS, headers=headers, json=body)).status_code == 403
        for bad in (
            {**body, "max_steps": 9}, {**body, "max_steps": 101}, {**body, "max_steps": "40"}, {**body, "max_steps": True},
            {**body, "max_steps": 40.0}, {**body, "extra": 1}, {"max_steps": 40},
        ):
            response = await client.post(DESKTOP_STEPS, headers=ORIGIN_HEADER, json=bad)
            assert response.status_code == 422, bad
            assert "extra" not in response.json()["detail"]
    assert path.read_text() == START


async def test_desktop_steps_save_writes_config_and_applies_to_the_next_task(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        initial = (await client.get(PERMISSIONS)).json()
        assert initial["desktop_steps"] == {"value": 30, "min": 10, "max": 100}
        result = await client.post(DESKTOP_STEPS, headers=ORIGIN_HEADER, json={"revision": initial["revision"], "max_steps": 45})
        assert result.status_code == 200, result.text
        assert result.json()["desktop_steps"]["value"] == 45
        assert app.config.desktop.max_steps == 45
        stale = await client.post(DESKTOP_STEPS, headers=ORIGIN_HEADER, json={"revision": initial["revision"], "max_steps": 20})
        assert stale.status_code == 409 and app.config.desktop.max_steps == 45
    assert tomllib.loads(path.read_text())["desktop"]["max_steps"] == 45
    assert load_config(path.parent).desktop.max_steps == 45


async def test_save_says_when_the_provider_budget_is_spent(dashboard, monkeypatch):
    app, path, browser = dashboard
    monkeypatch.setenv("OPENROUTER_API_KEY", "example-openrouter-key")

    def handler(request):
        return httpx.Response(403, json={"error": {
            "message": "Workspace daily budget exceeded",
            "metadata": {"limit_source": "openrouter_workspace_budget", "scope": "workspace", "limit_usd": 0.5,
                         "resets_at": "2026-11-25T00:00:00.000Z"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http, browser() as client:
        app.client = ChatClient(app.config.models["chat"], None, http)
        bad = await _save(client, "chat", PRIMARY, [{"base_url": "https://openrouter.ai/api/v1", "model": "m",
                                                     "api_key_env": "OPENROUTER_API_KEY"}])
        assert bad.status_code == 422
        error = bad.json()["errors"][0]["error"]
        assert error.startswith("OpenRouter's workspace budget ($0.50) is used up.") and "Resets 25 Nov" in error
        assert path.read_text() == START

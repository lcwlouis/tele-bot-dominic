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
        for route in ("/settings", API):
            assert (await client.get(route)).status_code == 403
        for route in (API, API + "/test"):
            assert (await client.post(route, json={}, headers=ORIGIN_HEADER)).status_code == 403

    async with browser() as client:
        for route in (API, API + "/test"):
            for headers in ({}, {"Origin": "https://other.example.test"}, {"Referer": ORIGIN}, {"HX-Request": "true"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=POST_HEADERS, json={})).status_code == 422
            assert (await client.post(route, headers=ORIGIN_HEADER, json={})).status_code == 422
            assert (await client.post(route, headers=POST_HEADERS, data={})).status_code == 415
        page = await client.get("/settings")
        assert page.status_code == 200 and "Key set" in page.text and 'data-role="drafting"' in page.text
        assert SECRET not in page.text
        app.store.owner_id = None
        assert (await client.get(API)).status_code == 409
    assert path.read_text() == START


async def test_save_backs_up_config_and_applies_new_routes_without_restart(dashboard):
    app, path, browser = dashboard
    route = dict(PRIMARY, model="local-media")
    async with browser() as client:
        initial = (await client.get(API)).json()
        assert set(initial["roles"]) == {"chat", "fallback", "media", "drafting"}
        assert initial["roles"]["fallback"]["primary"]["key_set"] is True
        assert initial["roles"]["chat"]["primary"]["kind"] == "local"
        assert initial["roles"]["fallback"]["primary"]["kind"] == "cloud"
        assert initial["triage"] == {"model": "classifier", "base_url": "https://classifier.example.test/decisions", "key_set": False}
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


def test_fallbacks_are_rejected_on_triage_and_search(tmp_path):
    from dom.config import validate_config

    extra = {"base_url": "https://llm.example.test/v1", "model": "m", "fallbacks": [PRIMARY]}
    for name in ("triage", "search"):
        with pytest.raises(ValueError, match="cannot have fallbacks"):
            validate_config(tmp_path, {"models": {"chat": PRIMARY, name: extra}})


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


async def test_probe_omits_reasoning_uses_larger_budget_accepts_thinking_and_uses_app_sleep(dashboard, monkeypatch):
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
            "max_tokens": 256,
            "provider": {"zdr": True},
        }
        assert "reasoning" not in body and "reasoning_effort" not in body

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


async def test_save_does_not_reprobe_a_slot_that_only_moved_or_changed_reasoning(dashboard):
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
        assert (await _save(client, "chat", dict(other, reasoning_effort="low"), [PRIMARY])).status_code == 200
        assert seen == []


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

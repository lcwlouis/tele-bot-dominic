"""Tests for v0.8.4 W9: hardening edge cases, AUTOINCREMENT migration, and pinned CI."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from dom.config import StudioFallback, load_config, validate_config
from dom.home import init_home
from dom.llm import ChatClient
from dom.store import Store
from dom.store.store import MIGRATIONS, MIGRATIONS_DIR, _split_statements, migrate_database
from dom.studio.cloud import VIDEO_JOB, CloudStudio
from dom.usage import Usage
from dom.web.studio_settings import SHARED_KEY, probe
from test_web_auth import ORIGIN

JSON = {"Origin": ORIGIN, "Content-Type": "application/json"}


# ---- 4a. Media key whose VALUE equals a chat key's value ----------------------------

def test_media_key_same_value_under_another_name_refused_at_validation(tmp_path):
    init_home(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=shared-secret-value\nOPENROUTER_MEDIA_API_KEY=shared-secret-value\n")
    raw = {
        "models": {"chat": {"base_url": "https://openrouter.ai/api/v1", "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
        "studio": {
            "voice_fallbacks": [
                {"provider": "openrouter_tts", "model": "openai/gpt-audio-mini", "api_key_env": "OPENROUTER_MEDIA_API_KEY"},
            ],
        },
    }
    with pytest.raises(ValidationError, match="separate variable"):
        validate_config(tmp_path, raw)

    raw_provider = {
        "models": {"chat": {"base_url": "https://openrouter.ai/api/v1", "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
        "providers": {
            "OR Media": {
                "kind": "openrouter",
                "type": "media",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_MEDIA_API_KEY",
            },
        },
    }
    with pytest.raises(ValidationError, match="separate variable"):
        validate_config(tmp_path, raw_provider)


async def test_media_key_refused_at_runtime_against_running_chat_clients(make_app):
    app = make_app()
    # Suppose OPENROUTER_API_KEY was rotated in .env without restarting Dom:
    # the running client still holds "old-chat-key", while .env now has "new-chat-key" for chat
    # and "old-chat-key" for OPENROUTER_MEDIA_API_KEY.
    (app.config.home / ".env").write_text(
        "OPENROUTER_API_KEY=new-chat-key\nOPENROUTER_MEDIA_API_KEY=old-chat-key\n"
    )
    app.client = SimpleNamespace(api_key="old-chat-key", http=httpx.AsyncClient())
    backup = StudioFallback(
        provider="openrouter_tts", model="openai/gpt-audio-mini", api_key_env="OPENROUTER_MEDIA_API_KEY",
    )
    cfg = app.config.model_copy(
        update={"studio": app.config.studio.model_copy(update={"voice_fallbacks": (backup,)})},
    )
    try:
        result = await probe(app, cfg, backup)
        assert not result.ok
        assert result.error == SHARED_KEY.format(name="OPENROUTER_MEDIA_API_KEY")

        cloud = CloudStudio(app.client.http, Usage(app.store), running_chat_keys=app.running_chat_keys)
        got = await cloud.generate(cfg, "voice", "hello", None, app.owner_id, lambda: asyncio.sleep(0, result=True))
        assert got is None
    finally:
        await app.client.http.aclose()


# ---- 4b. Concurrent video submits must not both pass the spend cap ------------------

async def test_concurrent_video_submits_reserve_estimate_under_lock(tmp_path):
    init_home(tmp_path)
    store = Store(tmp_path, owner_id=1)
    usage = Usage(store)
    submitted = 0
    release_submit = asyncio.Event()
    submit_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submitted
        if request.url.path.endswith("/videos") and request.method == "POST":
            submitted += 1
            submit_started.set()
            await release_submit.wait()
            return httpx.Response(200, json={"id": "job-1", "status": "failed", "usage": {"cost": 0.6}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cloud = CloudStudio(http, usage)
        provider = StudioFallback(
            provider="openrouter_video", model="heygen/heygen-avatar-6-turbo",
            api_key_env="OPENROUTER_MEDIA_API_KEY", data_policy="no_training",
        )
        entry = {
            "avatar": "heygen/heygen-avatar-6-turbo",
            "inputs": ["image", "audio"],
            "supported_resolutions": ["480p"],
            "pricing_skus": {"per_second_usd": 0.2},
        }
        plan = {"resolution": "480p"}
        cap = 0.8

        async def allowed() -> bool:
            _, spent = await usage.today(usage.now().tzinfo)
            return spent < cap

        async def run_one():
            try:
                return await cloud._video(provider, 2, allowed, "media-key", entry, plan, b"wav", 3.0, [])
            except Exception as exc:
                return exc

        task1 = asyncio.create_task(run_one())
        await submit_started.wait()
        # While task1 is in-flight over the network, task2 must NOT block on the network lock,
        # and MUST see task1's reserved $0.60 estimate (cap=0.50 so $0.60 >= $0.50 triggers block).
        cap = 0.5
        res2 = await run_one()
        from dom.usage import ModelCallSkipped
        assert isinstance(res2, ModelCallSkipped)
        assert submitted == 1  # second submit never hit the network!

        release_submit.set()
        await task1
    store.close()


async def test_failed_video_submit_releases_reserved_estimate(tmp_path):
    init_home(tmp_path)
    store = Store(tmp_path, owner_id=1)
    usage = Usage(store)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cloud = CloudStudio(http, usage)
        provider = StudioFallback(
            provider="openrouter_video", model="heygen/heygen-avatar-6-turbo",
            api_key_env="OPENROUTER_MEDIA_API_KEY", data_policy="no_training",
        )
        entry = {
            "avatar": "heygen/heygen-avatar-6-turbo",
            "inputs": ["image", "audio"],
            "supported_resolutions": ["480p"],
            "pricing_skus": {"per_second_usd": 0.2},
        }
        with pytest.raises(httpx.HTTPStatusError):
            await cloud._video(
                provider, 2, lambda: asyncio.sleep(0, result=True), "media-key", entry, {"resolution": "480p"}, b"wav", 3.0, [],
            )
        _, spent = await usage.today(usage.now().tzinfo)
        assert spent == 0.0
    store.close()


# ---- 4c. Persisted video jobs restart reconciler ------------------------------------

async def test_persisted_video_job_reconciled_on_startup(tmp_path):
    init_home(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=chat-key\nOPENROUTER_MEDIA_API_KEY=media-key\n")
    store = Store(tmp_path, owner_id=1)
    usage = Usage(store)
    row = await usage.record("studio_video", "heygen/heygen-avatar-6-turbo", "openrouter.ai", {"cost": 1.5}, 1, fallback=True)
    await store.kv_set(VIDEO_JOB + "job-restart-1", json.dumps({
        "row": row,
        "model": "heygen/heygen-avatar-6-turbo",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_MEDIA_API_KEY",
        "estimate": 1.5,
        "at": "2026-11-25T10:00:00+00:00",
    }))
    await store.kv_set(VIDEO_JOB + "bad-json", "{not-json")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos/job-restart-1"):
            return httpx.Response(200, json={"id": "job-restart-1", "status": "completed", "usage": {"cost": 0.42}})
        return httpx.Response(404)

    cfg = load_config(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cloud = CloudStudio(http, usage)
        count = await cloud.reconcile_pending(cfg)
        assert count == 1
        await asyncio.gather(*list(cloud._reconciling))
        _, spent = await usage.today(usage.now().tzinfo)
        assert pytest.approx(spent) == 0.42
        assert await store.kv_get(VIDEO_JOB + "job-restart-1") is None
        assert await store.kv_get(VIDEO_JOB + "bad-json") is None
    store.close()


# ---- 4d. Malformed .env must not crash the settings view ----------------------------

async def test_malformed_dotenv_does_not_crash_settings_view(make_app, browser):
    app = make_app()
    (app.config.home / "config.toml").write_text(
        'owner_id = 9001\n\n'
        '[models.chat]\nbase_url = "https://openrouter.ai/api/v1"\nmodel = "m"\napi_key_env = "OPENROUTER_API_KEY"\n'
    )
    app.config = load_config(app.config.home)
    async with browser(app) as client:
        (app.config.home / ".env").write_bytes(b"\xff\xfeINVALID_UTF8=1\n")
        models_resp = await client.get("/api/models")
        assert models_resp.status_code == 200
        perms_resp = await client.get("/api/permissions")
        assert perms_resp.status_code == 200


# ---- 4e. Provider rewrite must not move a different search backend ------------------

async def test_provider_rewrite_does_not_move_different_search_backend(make_app, browser, monkeypatch):
    app = make_app()
    (app.config.home / "config.toml").write_text(
        'owner_id = 9001\n\n'
        '[models.chat]\nbase_url = "https://openrouter.ai/api/v1"\nmodel = "m"\napi_key_env = "OPENROUTER_API_KEY"\n\n'
        '[models.search]\nprovider = "deepseek"\nbase_url = "https://proxy.example/v1"\napi_key_env = "SHARED_SEARCH_KEY"\n\n'
        '[[models.search.fallbacks]]\nprovider = "tavily"\nbase_url = "https://proxy.example/v1"\napi_key_env = "SHARED_SEARCH_KEY"\n\n'
        '[providers."Proxy DeepSeek"]\nkind = "deepseek"\ntype = "search"\nbase_url = "https://proxy.example/v1"\napi_key_env = "SHARED_SEARCH_KEY"\n\n'
        '[providers."Proxy Tavily"]\nkind = "tavily"\ntype = "search"\nbase_url = "https://proxy.example/v1"\napi_key_env = "SHARED_SEARCH_KEY"\n'
    )
    app.config = load_config(app.config.home)
    from dom.web import model_settings, provider_settings
    monkeypatch.setattr(provider_settings, "_probe", lambda *a, **kw: asyncio.sleep(0, result=model_settings.TestResult(ok=True, latency_ms=1, reply="ok")))

    async with browser(app) as client:
        rev = (await client.get("/api/models")).json()["revision"]
        saved = await client.post("/api/providers/save", headers=JSON, json={
            "revision": rev,
            "original": "Proxy DeepSeek",
            "name": "Proxy DeepSeek",
            "kind": "deepseek",
            "type": "search",
            "base_url": "https://proxy.example/v2",
            "api_key_env": "DEEPSEEK_NEW_KEY",
        })
        assert saved.status_code == 200, saved.text
        search = saved.json()["roles"]["search"]
        assert search["primary"]["provider"] == "deepseek"
        assert search["primary"]["base_url"] == "https://proxy.example/v2"
        assert search["primary"]["api_key_env"] == "DEEPSEEK_NEW_KEY"
        assert search["fallbacks"][0]["provider"] == "tavily"
        assert search["fallbacks"][0]["base_url"] == "https://proxy.example/v1"  # untouched!
        assert search["fallbacks"][0]["api_key_env"] == "SHARED_SEARCH_KEY"  # untouched!


# ---- 4f. OpenRouter search save with omitted key must not land on decisions provider -

async def test_openrouter_search_save_with_omitted_key_cannot_land_on_decisions_provider(make_app, browser):
    app = make_app()
    (app.config.home / "config.toml").write_text(
        'owner_id = 9001\n\n'
        '[models.chat]\nbase_url = "https://api.example.com/v1"\nmodel = "m"\napi_key_env = "CHAT_KEY"\n\n'
        '[models.triage]\nbase_url = "https://openrouter.ai/api/v1"\nmodel = "t"\napi_key_env = "OPENROUTER_API_KEY"\n\n'
        '[providers."OR Triage"]\nkind = "openrouter"\ntype = "decisions"\nbase_url = "https://openrouter.ai/api/v1"\napi_key_env = "OPENROUTER_API_KEY"\n'
    )
    app.config = load_config(app.config.home)

    async with browser(app) as client:
        rev = (await client.get("/api/models")).json()["revision"]
        resp = await client.post("/api/models", headers=JSON, json={
            "revision": rev,
            "role": "search",
            "primary": {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "perplexity/sonar",
                "api_key_env": None,
            },
            "fallbacks": [],
        })
        assert resp.status_code == 422
        assert "decisions provider" in resp.json()["detail"]


# ---- 5. AUTOINCREMENT migration 0029 ------------------------------------------------

def test_migration_0029_autoincrement_preserves_rows_indexes_and_never_reuses_ids(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # Apply every migration up to 0028 (user_version 29), BEFORE 0029.
        for version, filename in MIGRATIONS:
            if version >= 30:
                break
            sql = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _split_statements(sql):
                conn.execute(stmt)
            conn.execute("COMMIT")

        conn.execute(
            "INSERT INTO desktop_tasks (id, chat_id, brief, status, steps, created_at, container, steps_budget, extra_steps) "
            "VALUES (7, 1, 'task seven', 'done', 3, '2026-11-25T00:00:00Z', 'work', 40, 10)"
        )
        conn.execute(
            "INSERT INTO reminders (id, chat_id, text, due_at, importance, repeat, status, created_at, undeliverable) "
            "VALUES (12, '1', 'call mom', '2026-11-26T00:00:00Z', 'critical', '', 'active', '2026-11-25T00:00:00Z', 1)"
        )
    finally:
        conn.close()

    assert migrate_database(db_path) == 30

    conn = sqlite3.connect(db_path)
    try:
        task = conn.execute("SELECT id, brief, container, steps_budget, extra_steps FROM desktop_tasks").fetchall()
        assert task == [(7, "task seven", "work", 40, 10)]
        rem = conn.execute("SELECT id, text, undeliverable FROM reminders").fetchall()
        assert rem == [(12, "call mom", 1)]

        # Unique active index is preserved
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'desktop_tasks'")}
        assert "idx_desktop_one_active_per_container" in indexes

        # Deleting max-id rows and inserting new ones must NOT reuse 7 or 12
        conn.execute("DELETE FROM desktop_tasks WHERE id = 7")
        conn.execute("DELETE FROM reminders WHERE id = 12")
        new_task_id = conn.execute(
            "INSERT INTO desktop_tasks (chat_id, brief, status, created_at) VALUES (1, 'new task', 'queued', '2026-11-25T01:00:00Z')"
        ).lastrowid
        new_rem_id = conn.execute(
            "INSERT INTO reminders (chat_id, text, due_at, importance, repeat, status, created_at) "
            "VALUES ('1', 'new rem', '2026-11-27T00:00:00Z', 'normal', '', 'active', '2026-11-25T01:00:00Z')"
        ).lastrowid
        assert new_task_id == 8
        assert new_rem_id == 13
    finally:
        conn.close()


# ---- 6. CI hardening ----------------------------------------------------------------

def test_ci_workflows_pin_actions_by_sha_and_playwright_by_digest():
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    uses_re = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")
    sha_re = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for path in sorted(workflows.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = uses_re.match(line)
            if match:
                ref, comment = match.group(1), match.group(2)
                assert sha_re.match(ref), f"{path.name}: action not pinned to full SHA: {line}"
                assert "#" in comment, f"{path.name}: missing version comment on {line}"

    dash = (workflows / "dashboard-app.yml").read_text(encoding="utf-8")
    digest_prefix = "@" + "sha256:"
    assert re.search(r"mcr\.microsoft\.com/playwright:[^\s]+" + re.escape(digest_prefix) + r"[0-9a-f]{64}", dash)
    assert "npm ci --ignore-scripts" in dash

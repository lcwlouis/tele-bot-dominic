"""Tests for v0.8.5 W3: encrypted secret store, R4 desktop permissions, R5 saved logins, and E101 request_secret."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import struct
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from aiogram.types import CallbackQuery, User

from dom.cli import main as cli_main
from dom.config import WebConfig, _read_dotenv, set_dotenv
from dom.desktop import tools as desktop_tools
from dom.desktop.client import SandboxClient
from dom.desktop.files import stage_inbox
from dom.desktop.permissions import host_matches_site, normalize_site
from dom.desktop.vnc_type import char_keysym, encode_typing_frames
from dom.desktop.worker import DesktopWorker
from dom.home import Home
from dom.secrets import (
    MASTER_KEY_ENV,
    PREVIOUS_KEY_ENV,
    SECRET_REQUEST_TTL,
    SecretStore,
    SecretStoreError,
    generate_master_key,
    mask_username,
    redact_secrets,
)
from dom.telegram.handlers import handle_callback
from dom.tools import REQUEST_SECRET_TOOL, TurnCtx, enabled_tools
from dom.web.api_system import restart_refusal
from dom.web.app import create_app
from dom.web.auth import Auth
from test_desktop_worker import Sandbox, adopt, call, task_row, wait_status, wait_task, worker_for
from tghelpers import FRIEND, GROUP, OWNER, Clock, tg


# ---- Helpers -----------------------------------------------------------------------------------------------------


class FakeVncSocket:
    """In-memory RFB 3.8 server speaking SecurityType None (1) and recording every KeyEvent sent."""

    def __init__(self) -> None:
        self._incoming: list[bytes] = [
            b"RFB 003.008\n",
            b"\x01\x01",  # 1 security type: None (1)
            b"\x00\x00\x00\x00",  # SecurityResult: OK
            b"\x00" * 20 + struct.pack("!I", 3) + b"dom",  # ServerInit (desktop name = "dom")
        ]
        self.sent: list[bytes] = []
        self.typed_keysyms: list[tuple[int, bool]] = []

    async def recv(self) -> bytes:
        if not self._incoming:
            return b""
        return self._incoming.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        # Parse concatenated 8-byte RFB KeyEvent packets (type 4).
        if len(data) >= 8 and len(data) % 8 == 0 and data[0] == 4:
            for offset in range(0, len(data), 8):
                msg_type, down, keysym = struct.unpack("!BBxxI", data[offset : offset + 8])
                if msg_type == 4:
                    self.typed_keysyms.append((keysym, bool(down)))


def decode_typed_chars(keysyms: list[tuple[int, bool]]) -> str:
    """Reconstruct printable characters typed via RFB KeyEvent down events (excluding Ctrl+A/BackSpace/Enter)."""
    out = []
    ctrl_down = False
    for sym, down in keysyms:
        if sym == 0xFFE3:
            ctrl_down = down
            continue
        if not down or ctrl_down:
            continue
        if sym in (0xFF08, 0xFF0D):
            continue
        if 0x20 <= sym <= 0x7E or 0xA0 <= sym <= 0xFF:
            out.append(chr(sym))
        elif sym >= 0x01000000:
            out.append(chr(sym - 0x01000000))
    return "".join(out)


def _owner_callback(data: str, *, chat_id: int = OWNER, user_id: int = OWNER, is_bot: bool = False) -> CallbackQuery:
    return CallbackQuery(
        id="cb-1",
        chat_instance="inst",
        data=data,
        from_user=User(id=user_id, is_bot=is_bot, first_name="Owner"),
        message=tg(chat_id, user_id),
    )


async def _wait_for_pending_perm(app) -> dict:
    async with asyncio.timeout(2):
        while not (pending := await app.desktop_permissions.pending_requests()):
            await asyncio.sleep(0.005)
        return pending[0]


# ---- 1. Encrypted SecretStore & key rotation --------------------------------------------------------------------


async def test_secret_store_roundtrip_bootstrap_and_w4_reuse(make_app):
    app = make_app()
    secrets_store = app.secrets
    assert not (app.home.root / ".env").exists()

    # Bootstraps DOM_MASTER_KEY automatically with 0600 permissions.
    kid = await secrets_store.put("google_oauth", "owner", {"access_token": "tok-1", "refresh_token": "ref-1"})
    assert len(kid) == 12
    env_path = app.home.root / ".env"
    assert env_path.is_file()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert MASTER_KEY_ENV in _read_dotenv(env_path)

    # Plaintext never stored in SQLite.
    raw_row = await app.store.get_encrypted_secret("google_oauth", "owner")
    assert raw_row is not None
    assert "tok-1" not in raw_row["ciphertext"]
    assert "ref-1" not in raw_row["ciphertext"]
    assert raw_row["ciphertext"].startswith(f"v1:{kid}:")

    # Clean decryption + metadata listing + delete.
    assert await secrets_store.get("google_oauth", "owner") == {"access_token": "tok-1", "refresh_token": "ref-1"}
    assert await secrets_store.exists("google_oauth", "owner") is True
    keys = await secrets_store.list_keys("google_oauth")
    assert len(keys) == 1 and keys[0].key == "owner" and keys[0].key_id == kid
    assert await secrets_store.delete("google_oauth", "owner") is True
    assert await secrets_store.get("google_oauth", "owner") is None


async def test_secret_store_fails_closed_on_tamper_swap_or_bad_key(make_app):
    app = make_app()
    await app.secrets.put("login", "1", {"username": "u", "password": "p1"})
    await app.secrets.put("login", "2", {"username": "u", "password": "p2"})

    # AAD prevents swapping ciphertext from key "2" into key "1".
    row2 = await app.store.get_encrypted_secret("login", "2")
    assert row2 is not None
    await app.store.put_encrypted_secret("login", "1", row2["key_id"], row2["ciphertext"], row2["updated_at"])
    with pytest.raises(SecretStoreError):
        await app.secrets.get("login", "1")

    # Tampered ciphertext fails GCM tag check.
    bad_ct = row2["ciphertext"][:-4] + "AAAA"
    await app.store.put_encrypted_secret("login", "2", row2["key_id"], bad_ct, row2["updated_at"])
    with pytest.raises(SecretStoreError):
        await app.secrets.get("login", "2")

    # Missing or malformed key fails closed.
    await app.secrets.put("owner_secret", "api", {"value": "val"})
    set_dotenv(app.home.root, MASTER_KEY_ENV, "not-32-bytes")
    with pytest.raises(SecretStoreError):
        await app.secrets.get("owner_secret", "api")
    set_dotenv(app.home.root, MASTER_KEY_ENV, None)
    with pytest.raises(SecretStoreError):
        await app.secrets.get("owner_secret", "api")


async def test_secret_store_crash_safe_key_rotation_and_recovery(make_app):
    app = make_app()
    await app.secrets.put("login", "10", {"username": "alice@example.org", "password": "pw-before-rotate"})
    await app.secrets.put("google_oauth", "owner", {"refresh_token": "rt-before-rotate"})
    old_env_key = _read_dotenv(app.home.root / ".env")[MASTER_KEY_ENV]
    old_status = await app.secrets.key_status()
    assert old_status["configured"] and old_status["total_entries"] == 2

    new_kid, count = await app.secrets.rotate_key()
    assert count == 2
    assert new_kid != old_status["key_id"]
    env_after = _read_dotenv(app.home.root / ".env")
    assert env_after[MASTER_KEY_ENV] != old_env_key
    assert PREVIOUS_KEY_ENV not in env_after

    # Everything decrypts cleanly under the new key.
    assert await app.secrets.get("login", "10") == {"username": "alice@example.org", "password": "pw-before-rotate"}
    assert await app.secrets.get("google_oauth", "owner") == {"refresh_token": "rt-before-rotate"}

    # Stolen old key alone can NO LONGER decrypt rotated rows.
    new_env_key = env_after[MASTER_KEY_ENV]
    set_dotenv(app.home.root, MASTER_KEY_ENV, old_env_key)
    with pytest.raises(SecretStoreError):
        await app.secrets.get("login", "10")

    # Interrupted-rotation recovery: DOM_MASTER_KEY holds a fresh key while DOM_MASTER_KEY_PREVIOUS holds new_env_key.
    brand_new = generate_master_key()
    set_dotenv(app.home.root, MASTER_KEY_ENV, brand_new)
    set_dotenv(app.home.root, PREVIOUS_KEY_ENV, new_env_key)
    assert await app.secrets.get("login", "10") == {"username": "alice@example.org", "password": "pw-before-rotate"}
    final_kid, final_count = await app.secrets.rotate_key()
    assert final_count == 2 and final_kid
    assert PREVIOUS_KEY_ENV not in _read_dotenv(app.home.root / ".env")
    assert await app.secrets.get("google_oauth", "owner") == {"refresh_token": "rt-before-rotate"}


async def test_cli_rotate_key_and_redact_secrets(make_app, capsys):
    app = make_app()
    await app.secrets.put("owner_secret", "demo", {"value": "top-secret-demo"})
    master = _read_dotenv(app.home.root / ".env")[MASTER_KEY_ENV]
    assert "[redacted]" in redact_secrets(f"leak {master} and extra-pw", app.config, extra=["extra-pw"])
    assert master not in redact_secrets(f"leak {master}", app.config)
    assert "extra-pw" not in redact_secrets("leak extra-pw", app.config, extra=["extra-pw"])

    app.store.close()
    (app.home.root / "config.toml").write_text('timezone = "UTC"\n[models.chat]\nbase_url = "http://localhost:8000/v1"\nmodel = "m"\n')
    code = await asyncio.to_thread(cli_main, ["rotate-key", str(app.home.root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Rotated DOM_MASTER_KEY" in out and "1 secret(s)" in out


# ---- 2. Zero-leak end-to-end saved-login fill via VNC -----------------------------------------------------------


async def test_saved_login_fill_never_leaks_to_model_http_logs_or_db(make_app, monkeypatch, caplog):
    """End-to-end invariant from v085-w3.md:
    a saved password never appears in any model request, tool result, sandbox HTTP request,
    turn log, trace, usage row, DB audit row, or Python log line."""
    caplog.set_level(logging.DEBUG)
    app = make_app()
    canary_user = "canary.alice@example.org"
    canary_pass = "CANARY_SUPER_SECRET_PW_987654"
    meta = await app.desktop_permissions.save_login(
        site="bank.example.org",
        label="Example Bank",
        username=canary_user,
        password=canary_pass,
    )
    assert meta["username_hint"] == mask_username(canary_user)
    assert canary_user not in json.dumps(meta)
    assert canary_pass not in json.dumps(meta)

    http_requests: list[tuple[str, str, bytes]] = []
    vnc_sessions: list[FakeVncSocket] = []

    @asynccontextmanager
    async def mock_vnc_connect(url, **kwargs):
        assert url == "ws://127.0.0.1:18080/websockify"
        assert kwargs["additional_headers"] == {"Authorization": "Bearer " + "sb-key-123"}
        session = FakeVncSocket()
        vnc_sessions.append(session)
        yield session

    monkeypatch.setattr("dom.desktop.vnc_type.connect_vnc", mock_vnc_connect)

    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        http_requests.append((request.method, str(request.url), request.content))
        path = request.url.path
        if path == "/v1/browser/tabs":
            return httpx.Response(200, json={
                "success": True,
                "data": [{"index": 0, "url": "https://bank.example.org/login", "title": "Sign in", "is_active": True}],
            })
        if path == "/v1/browser/page/evaluate":
            return httpx.Response(200, json={"success": True, "data": True})
        if path == "/v1/bash/exec":
            return httpx.Response(200, json={"success": True, "data": {"exit_code": 0, "stdout": "", "stderr": ""}})
        return httpx.Response(404, json={"success": False, "message": "unexpected endpoint"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox_handler)) as http:
        real_client = SandboxClient("http://127.0.0.1:18080", "sb-key-123", http)

        class RealClientSandbox(Sandbox):
            client = real_client

            async def ensure_running(self):
                return real_client

        worker = await worker_for(
            app,
            call("saved_logins", site="bank.example.org"),
            call("fill_saved_login", login_id=meta["id"]),
            call("finish", result="Signed in successfully."),
            {"content": '{"bubbles":[{"text":"Logged you in."}]}'},
        )
        worker.sandbox = RealClientSandbox()
        app.sandbox = worker.sandbox

        task_id = await worker.enqueue(OWNER, "Sign in to Example Bank")
        # E100: new site MUST pause and ask via Telegram card (Reject, Allow once, Allow always).
        pending = await _wait_for_pending_perm(app)
        assert pending["action"] == "fill_login"
        assert pending["site"] == "bank.example.org"

        # Restart must be refused while the permission card is pending.
        assert restart_refusal(app) is not None

        cards = app.bot.of("send", OWNER)
        assert cards
        markup = cards[-1][3]["reply_markup"]
        btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        assert btn_texts[:3] == ["Allow once", "Allow always", "Reject"]
        once_cb = markup.inline_keyboard[0][0].callback_data

        # Non-owner / group / wrong-token taps are ignored.
        await handle_callback(app, _owner_callback(once_cb, user_id=FRIEND))
        await handle_callback(app, _owner_callback(once_cb, chat_id=GROUP))
        await handle_callback(app, _owner_callback(once_cb[:-2] + "00"))
        assert len(await app.desktop_permissions.pending_requests()) == 1

        # Owner taps Allow once in owner DM.
        await handle_callback(app, _owner_callback(once_cb))
        await wait_task(worker)

    row = await task_row(app, task_id)
    assert row["status"] == "done"

    # VNC received BOTH username and password keystrokes (and Enter submit).
    all_keysyms = [item for session in vnc_sessions for item in session.typed_keysyms]
    typed = decode_typed_chars(all_keysyms)
    assert canary_user in typed
    assert canary_pass in typed
    assert any(sym == 0xFF0D and down for sym, down in all_keysyms)

    # Zero sandbox HTTP request touched page/type or page/fill, and NONE carried the password or full username.
    for method, url, body in http_requests:
        assert "/page/type" not in url and "/page/fill" not in url
        assert canary_pass.encode() not in body
        assert canary_user.encode() not in body

    # Zero model request or tool result carried the password or full username.
    all_model_json = json.dumps(app.client.requests, default=str)
    assert canary_pass not in all_model_json
    assert canary_user not in all_model_json
    assert f"Filled saved login #{meta['id']} for bank.example.org." in all_model_json

    # Zero DB rows (messages, turns, usage, desktop_permission_history, saved_logins, desktop_tasks) contain password.
    def dump_db() -> str:
        conn = app.store._conn
        chunks = []
        for table in ("messages", "turns", "usage", "desktop_permission_history", "saved_logins", "desktop_tasks"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            chunks.append(json.dumps([dict(r) for r in rows], default=str))
        return "\n".join(chunks)

    db_dump = await app.store._run(dump_db)
    assert canary_pass not in db_dump
    assert canary_user not in db_dump

    # Zero Telegram messages or Python logs contain the password.
    assert canary_pass not in str(app.bot.calls)
    assert canary_pass not in caplog.text

    # Allow once left fill_login policy as Ask for the next attempt.
    assert await app.desktop_permissions.resolve("fill_login", "bank.example.org") == "ask"


# ---- 3. Origin, HTTPS, and top-level password focus enforcement -------------------------------------------------


def test_site_normalization_and_origin_matching():
    assert normalize_site("https://WWW.Example.Org/login") == "example.org"
    assert normalize_site("192.0.2.1") is None
    assert normalize_site("box" + ".local") is None
    assert normalize_site("github.io") is None

    assert host_matches_site("https://bank.example.org/signin", "bank.example.org") is True
    assert host_matches_site("https://www.bank.example.org/signin", "bank.example.org") is True
    assert host_matches_site("http://bank.example.org/signin", "bank.example.org") is False  # HTTP refused
    assert host_matches_site("https://evil.example.net/signin", "bank.example.org") is False
    assert host_matches_site("https://sub.bank.example.org/signin", "bank.example.org", match_subdomains=False) is False
    assert host_matches_site("https://sub.bank.example.org/signin", "bank.example.org", match_subdomains=True) is True


async def test_fill_saved_login_refuses_http_wrong_host_and_unfocused_password(make_app, monkeypatch):
    app = make_app()
    meta = await app.desktop_permissions.save_login(site="app.example.org", username="u", password="secret-password-xyz")
    await app.desktop_permissions.set_site_rule("app.example.org", "fill_login", "allow")

    vnc_called = False

    @asynccontextmanager
    async def mock_vnc_connect(url, **kwargs):
        nonlocal vnc_called
        vnc_called = True
        yield FakeVncSocket()

    monkeypatch.setattr("dom.desktop.vnc_type.connect_vnc", mock_vnc_connect)

    # Case 1: HTTP (not HTTPS) -> refused before VNC.
    worker = await worker_for(
        app,
        call("fill_saved_login", login_id=meta["id"]),
        call("finish", result="could not fill"),
        {"content": '{"bubbles":[{"text":"stopped"}]}'},
    )
    worker.sandbox.url = "http://app.example.org/login"
    await worker.enqueue(OWNER, "Login")
    await wait_task(worker)
    assert not vnc_called
    assert "https:// page on app.example.org" in json.dumps(app.client.requests)

    # Case 2: Hostile / False is_top_level_password_focused -> aborts before sending password over VNC.
    def hostile_eval(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/browser/tabs":
            return httpx.Response(200, json={"success": True, "data": [{"url": "https://app.example.org/login", "is_active": True}]})
        if request.url.path == "/v1/browser/page/evaluate":
            # Focus password returns True, then is_top_level_password_focused returns hostile string "true" (not boolean True!).
            body = json.loads(request.content)
            if "querySelectorAll" in body["expression"]:
                return httpx.Response(200, json={"success": True, "data": True})
            return httpx.Response(200, json={"success": True, "data": "true"})
        return httpx.Response(200, json={"success": True, "data": {"exit_code": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(hostile_eval)) as http:
        client = SandboxClient("http://127.0.0.1:18080", "k", http)

        class Sbx(Sandbox):
            async def ensure_running(self):
                return client

        meta2 = await app.desktop_permissions.save_login(site="app.example.org", username="", password="pw-never-typed")
        worker2 = await worker_for(
            app,
            call("fill_saved_login", login_id=meta2["id"]),
            call("finish", result="aborted"),
            {"content": '{"bubbles":[{"text":"stopped"}]}'},
        )
        worker2.sandbox = Sbx()
        await worker2.enqueue(OWNER, "Login")
        await wait_task(worker2)
        assert not vnc_called
        assert "not a top-level password field" in json.dumps(app.client.requests)


# ---- 4. R4 Desktop Permissions: all 5 action kinds --------------------------------------------------------------


async def test_r4_allow_always_and_reject_for_visit_download_upload_and_background(make_app):
    app = make_app()

    # 1) visit_site: Ask -> Allow always adds per-site rule; Never blocks browse & redirects.
    await app.desktop_permissions.set_policy("visit_site", "ask")
    navigated = []

    async def nav(url):
        navigated.append(url)

    worker = await worker_for(
        app,
        call("browse", url="https://docs.example.org/start"),
        call("browse", url="https://docs.example.org/next"),
        call("finish", result="read docs"),
        {"content": '{"bubbles":[{"text":"done"}]}'},
    )
    worker.sandbox.navigate = nav
    await worker.enqueue(OWNER, "Read docs")
    pending = await _wait_for_pending_perm(app)
    assert pending["action"] == "visit_site" and pending["site"] == "docs.example.org"
    ok, _ = await app.desktop_permissions.decide(pending["id"], "always", via="dashboard")
    assert ok
    await wait_task(worker)
    assert navigated == ["https://docs.example.org/start", "https://docs.example.org/next"]
    assert await app.desktop_permissions.resolve("visit_site", "docs.example.org") == "allow"

    # Per-site Never blocks browse immediately.
    await app.desktop_permissions.set_site_rule("blocked.example.org", "visit_site", "never")
    navigated.clear()
    worker2 = await worker_for(
        app,
        call("browse", url="https://blocked.example.org/"),
        call("finish", result="was blocked"),
        {"content": '{"bubbles":[{"text":"blocked"}]}'},
    )
    worker2.sandbox.navigate = nav
    await worker2.enqueue(OWNER, "Open blocked")
    await wait_task(worker2)
    assert navigated == []
    assert "set to Never" in json.dumps(app.client.requests)

    # 2) download: Ask -> Reject refuses file download, worker finishes with text only.
    worker3 = await worker_for(
        app,
        call("finish", result="Here is the file", files=["/tmp/secret.pdf"]),
        call("finish", result="Owner rejected file download."),
        {"content": '{"bubbles":[{"text":"ok"}]}'},
    )
    worker3.sandbox.url = "https://files.example.org/doc"
    await worker3.enqueue(OWNER, "Get PDF")
    pending_dl = await _wait_for_pending_perm(app)
    assert pending_dl["action"] == "download" and pending_dl["site"] == "files.example.org"
    await app.desktop_permissions.decide(pending_dl["id"], "reject", via="dashboard")
    await wait_task(worker3)
    assert "Owner rejected permission for download" in json.dumps(app.client.requests)

    # 3) upload: Never blocks staging inbox AND upload_file tool.
    app.home = Home(app.home.root / "home")
    await app.desktop_permissions.set_policy("upload", "never")
    stage_inbox(app.home.root, "notes.txt", b"hello")
    uploaded_inbox = []
    worker4 = await worker_for(
        app,
        call("upload_file", selector="#file", path="/tmp/a.txt"),
        call("finish", result="upload blocked"),
        {"content": '{"bubbles":[{"text":"blocked"}]}'},
    )
    worker4.sandbox.upload_file = lambda p, d: uploaded_inbox.append(p)
    await worker4.enqueue(OWNER, "Upload file")
    await wait_task(worker4)
    assert uploaded_inbox == []
    assert "set to Never" in json.dumps(app.client.requests)

    # 4) background_work: Never skips plan & refuses _run; Ask sends approval card.
    await app.desktop_permissions.set_policy("background_work", "never")
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    now = app.dispatch.now()
    rows = await app.store.add_self_tasks(
        [{"goal": "Read tide pools", "why": "curious", "tools": ["browse", "read_page"], "sites": ["example.org"], "minutes": 5, "steps": 10}],
        now, now,
    )
    await app.self_work._run(rows[0])
    updated = (await app.store.self_tasks(limit=1))[0]
    assert updated["status"] == "refused" and "Never" in updated["error"]

    # background_work = ask -> Reject cancels the self-directed task.
    await app.desktop_permissions.set_policy("background_work", "ask")
    rows2 = await app.store.add_self_tasks(
        [{"goal": "Check trains", "why": "curious", "tools": ["browse", "read_page"], "sites": ["example.org"], "minutes": 5, "steps": 10}],
        now, now,
    )
    run_task = asyncio.create_task(app.self_work._run(rows2[0]))
    pending_bg = await _wait_for_pending_perm(app)
    assert pending_bg["action"] == "background_work"
    await app.desktop_permissions.decide(pending_bg["id"], "reject", via="dashboard")
    await run_task
    updated2 = (await app.store.self_tasks(limit=1))[0]
    assert updated2["status"] == "cancelled"

    # Self-directed tasks never receive saved_logins, fill_saved_login, or upload_file.
    assert "saved_logins" not in desktop_tools.OWN
    assert "fill_saved_login" not in desktop_tools.OWN
    assert "upload_file" not in desktop_tools.OWN


# ---- 5. E101 Secure secret requests (`request_secret`) ----------------------------------------------------------


async def test_e101_request_secret_scope_guardrails_and_submission(make_app):
    clock = Clock(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc))
    app = make_app()
    app.dispatch.now = clock
    app.config = app.config.model_copy(update={"web": WebConfig(public_url="https://dom.example.org")})

    # Strictly owner_dm only: absent from friend_dm and group tool lists, rejected by handler.
    assert "request_secret" in [t.name for t in await enabled_tools(app, "owner_dm")]
    assert "request_secret" not in [t.name for t in await enabled_tools(app, "friend_dm")]
    assert "request_secret" not in [t.name for t in await enabled_tools(app, "group")]

    group_ctx = TurnCtx("group", GROUP, OWNER, app.home, app.store, OWNER, app=app)
    assert "only in the owner's DM" in await REQUEST_SECRET_TOOL.handler({"label": "Key", "reason": "test"}, group_ctx)

    owner_ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, OWNER, app=app)

    # Reserved .env variables cannot be requested.
    for reserved in ("DOM_MASTER_KEY", "TELEGRAM_BOT_TOKEN", "SANDBOX_API_KEY"):
        res = await REQUEST_SECRET_TOOL.handler(
            {"label": "Bad", "reason": "try overwrite", "target": "env", "key": reserved}, owner_ctx,
        )
        assert "reserved" in res

    # Valid secret request returns NO token or URL to the model, and sends Telegram card with #t= fragment.
    res_json = json.loads(await REQUEST_SECRET_TOOL.handler(
        {"label": "Weather API key", "reason": "For local forecast plugin", "target": "secret", "key": "weather_api"},
        owner_ctx,
    ))
    assert res_json["status"] == "requested"
    assert "token" not in res_json and "#t=" not in json.dumps(res_json)

    card = app.bot.of("send", OWNER)[-1]
    url_btn = card[3]["reply_markup"].inline_keyboard[0][0]
    assert url_btn.url.startswith("https://dom.example.org/app/secret#t=")
    raw_token = url_btn.url.split("#t=", 1)[1]

    # Raw token is NOT stored in SQLite (only SHA-256 hash).
    req_row = await app.store.get_secret_request(res_json["request_id"])
    assert req_row is not None and raw_token not in json.dumps(req_row)

    # Submit value securely -> encrypted in SecretStore, owner event says ONLY "set", never the value.
    secret_canary = "WEATHER_SECRET_CANARY_4242"
    submitted = await app.secret_requests.submit(token=raw_token, value=secret_canary)
    assert submitted["status"] == "set"
    assert await app.secrets.get("owner_secret", "weather_api") == {"value": secret_canary, "label": "Weather API key"}

    # Single-use: second lookup/submit fails.
    assert await app.secret_requests.lookup(token=raw_token) is None
    with pytest.raises(ValueError, match="expired or was already used"):
        await app.secret_requests.submit(token=raw_token, value="other")

    # Owner event mentions only that it is set, never the secret value.
    window = str(await app.store.get_recent_chat_window(str(OWNER), 20))
    assert "Weather API key" in window and "status: set" in window
    assert secret_canary not in window

    # Target = env writes to .env; target = login saves encrypted login.
    env_req = await app.secret_requests.create(label="Demo Key", reason="demo", target="env", key="DEMO_CUSTOM_KEY")
    await app.secret_requests.submit(request_id=env_req["request_id"], value="custom-val-123")
    assert _read_dotenv(app.home.root / ".env")["DEMO_CUSTOM_KEY"] == "custom-val-123"

    login_req = await app.secret_requests.create(label="Forum", reason="check inbox", target="login", key="forum.example.org")
    await app.secret_requests.submit(request_id=login_req["request_id"], username="bob", password="forum-pw-99")
    logins = await app.desktop_permissions.list_logins("forum.example.org")
    assert len(logins) == 1 and logins[0]["site"] == "forum.example.org"

    # Expiry after 15 minutes.
    exp_req = await app.secret_requests.create(label="Temp", reason="short lived")
    clock.advance(seconds=int(SECRET_REQUEST_TTL.total_seconds()) + 1)
    assert await app.secret_requests.lookup(request_id=exp_req["request_id"]) is None

    # Cancel via Telegram callback.
    c_req = await app.secret_requests.create(label="Cancel me", reason="never mind")
    await handle_callback(app, _owner_callback(f"sreq:cancel:{c_req['request_id']}"))
    assert await app.secret_requests.lookup(request_id=c_req["request_id"]) is None


# ---- 6. Dashboard API (`/api/access/*`, `/api/secrets/request/*`, `/api/waiting`) ------------------------------


async def test_dashboard_access_and_secret_entry_api(make_app):
    app = make_app()
    token = "dev-token-" + "x" * 32
    set_dotenv(app.home.root, "DEV_DASH_TOKEN", token)
    app.config = app.config.model_copy(update={
        "web": WebConfig(enabled=True, bind="127.0.0.1", port=8080, dev_token_env="DEV_DASH_TOKEN"),
    })
    web = create_app(app, Auth(app.config))
    headers = {"Authorization": f"Bearer {token}", "Origin": "http://127.0.0.1:8080"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://127.0.0.1:8080") as client:
        # Unauthenticated is rejected.
        assert (await client.get("/api/access")).status_code == 403
        # Cross-origin POST is rejected.
        bad = await client.post(
            "/api/access/policy",
            headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example.net"},
            json={"action": "visit_site", "policy": "ask"},
        )
        assert bad.status_code == 403

        # Save a login and verify GET returns it masked (no password, no full username).
        saved = await client.post(
            "/api/access/logins/save",
            headers=headers,
            json={"site": "portal.example.org", "label": "Portal", "username": "alice@example.org", "password": "MyPassword!99"},
        )
        assert saved.status_code == 200
        assert "MyPassword!99" not in saved.text
        assert "alice@example.org" not in saved.text
        body = saved.json()
        assert len(body["logins"]) == 1
        assert body["logins"][0]["username_hint"] == mask_username("alice@example.org")

        # Policies & site rules & clear-allows.
        r_pol = await client.post("/api/access/policy", headers=headers, json={"action": "visit_site", "policy": "never"})
        assert r_pol.status_code == 200 and r_pol.json()["policies"]["visit_site"] == "never"
        r_site = await client.post(
            "/api/access/site-rule", headers=headers,
            json={"site": "portal.example.org", "action": "visit_site", "policy": "allow"},
        )
        assert r_site.status_code == 200 and len(r_site.json()["site_rules"]) == 1
        r_clear = await client.post("/api/access/site-rules/clear-allows", headers=headers, json={"action": "visit_site"})
        assert r_clear.status_code == 200 and r_clear.json()["site_rules"] == []

        # Key rotation via dashboard API.
        old_kid = body["key_status"]["key_id"]
        r_rot = await client.post("/api/access/rotate-key", headers=headers, json={})
        assert r_rot.status_code == 200
        assert r_rot.json()["key_status"]["key_id"] != old_kid

        # E101 request shows up in /api/waiting and /api/shell, and completes through lookup + submit.
        req = await app.secret_requests.create(label="API Key", reason="Need it", target="secret", key="my_api")
        waiting = (await client.get("/api/waiting", headers={"Authorization": f"Bearer {token}"})).json()
        assert len(waiting["secret_requests"]) == 1
        shell = (await client.get("/api/shell", headers={"Authorization": f"Bearer {token}"})).json()
        assert shell["waiting"] >= 1

        lookup = await client.post("/api/secrets/request/lookup", headers=headers, json={"request_id": req["request_id"]})
        assert lookup.status_code == 200 and lookup.json()["request"]["label"] == "API Key"

        sub = await client.post(
            "/api/secrets/request/submit", headers=headers,
            json={"request_id": req["request_id"], "value": "TOP_SECRET_API_KEY"},
        )
        assert sub.status_code == 200 and "TOP_SECRET_API_KEY" not in sub.text

        # Second lookup returns 410 Gone.
        assert (await client.post("/api/secrets/request/lookup", headers=headers, json={"request_id": req["request_id"]})).status_code == 410

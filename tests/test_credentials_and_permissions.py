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
            expr = json.loads(request.content)["expression"]
            if "formAction" in expr:
                return httpx.Response(200, json={
                    "success": True,
                    "data": {
                        "href": "https://bank.example.org/login",
                        "hasFocus": True,
                        "topLevel": True,
                        "fieldOk": True,
                        "formAction": "https://bank.example.org/login",
                    },
                })
            if "sameTarget" in expr:
                return httpx.Response(200, json={
                    "success": True,
                    "data": {
                        "href": "https://bank.example.org/login",
                        "hasFocus": True,
                        "topLevel": True,
                        "sameTarget": True,
                    },
                })
            if "hasValue" in expr:
                return httpx.Response(200, json={"success": True, "data": {"hasValue": False}})
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
            # Focus password returns True, then verify_login_field returns hostile string "true" for fieldOk (not boolean True!).
            body = json.loads(request.content)
            if "querySelectorAll" in body["expression"]:
                return httpx.Response(200, json={"success": True, "data": True})
            return httpx.Response(200, json={
                "success": True,
                "data": {"href": "https://app.example.org/login", "hasFocus": True, "topLevel": True, "fieldOk": "true", "formAction": None},
            })
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

    # Target = env writes to .env (config-referenced keys only); target = login saves encrypted login.
    env_req = await app.secret_requests.create(label="KLIPY Key", reason="GIF search", target="env", key="KLIPY_API_KEY")
    await app.secret_requests.submit(request_id=env_req["request_id"], value="custom-val-123")
    assert _read_dotenv(app.home.root / ".env")["KLIPY_API_KEY"] == "custom-val-123"

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


# ---- 7. Security review regression tests (B1-B2, S1-S8, Nits) ---------------------------------------------------


async def test_b1_interrupted_rotation_then_failed_rotation_preserves_secrets(make_app, monkeypatch):
    """B1: Interrupted rotation (CancelledError or crash) followed by a failed rotation must NEVER destroy secrets."""
    app = make_app()
    await app.secrets.put("login", "1", {"username": "u", "password": "secret-b1"})
    old0 = _read_dotenv(app.home.root / ".env")[MASTER_KEY_ENV]

    # Case 1: CancelledError during replace_encrypted_secrets must roll back .env cleanly.
    orig_replace = app.store.replace_encrypted_secrets

    async def cancel_once(items):
        raise asyncio.CancelledError

    monkeypatch.setattr(app.store, "replace_encrypted_secrets", cancel_once)
    with pytest.raises(asyncio.CancelledError):
        await app.secrets.rotate_key()
    env_after_cancel = _read_dotenv(app.home.root / ".env")
    assert env_after_cancel.get(MASTER_KEY_ENV) == old0
    assert PREVIOUS_KEY_ENV not in env_after_cancel
    assert await app.secrets.get("login", "1") == {"username": "u", "password": "secret-b1"}

    # Case 2: Simulated crash right after staging .env (MASTER=new1, PREVIOUS=old0, DB still under old0),
    # followed by Rotation 2 failing during replace_encrypted_secrets -> old0 rows MUST survive!
    new1 = generate_master_key()
    set_dotenv(app.home.root, MASTER_KEY_ENV, new1)
    set_dotenv(app.home.root, PREVIOUS_KEY_ENV, old0)

    async def fail_db(items):
        raise RuntimeError("sqlite busy")

    monkeypatch.setattr(app.store, "replace_encrypted_secrets", fail_db)
    with pytest.raises(SecretStoreError):
        await app.secrets.rotate_key()
    # Secret must still decrypt!
    assert await app.secrets.get("login", "1") == {"username": "u", "password": "secret-b1"}

    # Case 3: Recovery finishes cleanly, then fail on the second replace (new1 -> new2): DB is already on new1,
    # rollback restores MASTER=new1, and secrets still decrypt!
    calls = 0

    async def succeed_first_fail_second(items):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await orig_replace(items)
        raise RuntimeError("disk full")

    monkeypatch.setattr(app.store, "replace_encrypted_secrets", succeed_first_fail_second)
    with pytest.raises(SecretStoreError):
        await app.secrets.rotate_key()
    assert await app.secrets.get("login", "1") == {"username": "u", "password": "secret-b1"}

    # Case 4: Double rotation succeeds cleanly and old keys no longer decrypt.
    monkeypatch.setattr(app.store, "replace_encrypted_secrets", orig_replace)
    kid_a, count_a = await app.secrets.rotate_key()
    key_a = _read_dotenv(app.home.root / ".env")[MASTER_KEY_ENV]
    kid_b, count_b = await app.secrets.rotate_key()
    assert count_a == 1 and count_b == 1 and kid_a != kid_b
    assert PREVIOUS_KEY_ENV not in _read_dotenv(app.home.root / ".env")
    assert await app.secrets.get("login", "1") == {"username": "u", "password": "secret-b1"}
    set_dotenv(app.home.root, MASTER_KEY_ENV, key_a)
    with pytest.raises(SecretStoreError):
        await app.secrets.get("login", "1")


def test_b2a_fill_saved_login_removes_submit_parameter():
    """B2(a): submit must be removed from fill_saved_login schema so the model cannot pass submit=false."""
    schema = next(s for s in desktop_tools.schemas() if s["function"]["name"] == "fill_saved_login")
    assert "submit" not in schema["function"]["parameters"]["properties"]
    with pytest.raises(ValueError):
        desktop_tools.arguments({"name": "fill_saved_login", "arguments": json.dumps({"login_id": 1, "submit": False})})


async def test_b2b_and_b2c_clears_leftover_password_and_redacts_per_run(make_app, monkeypatch):
    """B2(b,c): Leftover password after submit is cleared over VNC, and username+password are redacted for the whole run."""
    app = make_app()
    canary_user = "redacted.alice@example.org"
    canary_pass = "CANARY_REDACT_PASS_5511"
    meta = await app.desktop_permissions.save_login(
        site="vault.example.org", username=canary_user, password=canary_pass,
    )
    await app.desktop_permissions.set_site_rule("vault.example.org", "fill_login", "allow")

    vnc_sessions: list[FakeVncSocket] = []

    @asynccontextmanager
    async def mock_vnc_connect(url, **kwargs):
        session = FakeVncSocket()
        vnc_sessions.append(session)
        yield session

    monkeypatch.setattr("dom.desktop.vnc_type.connect_vnc", mock_vnc_connect)

    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/browser/tabs":
            return httpx.Response(200, json={
                "success": True,
                "data": [{"url": "https://vault.example.org/login", "is_active": True}],
            })
        if path == "/v1/browser/page/evaluate":
            expr = json.loads(request.content)["expression"]
            if "hasValue" in expr:
                # Simulate failed login: password field STILL holds a value after Enter!
                return httpx.Response(200, json={"success": True, "data": {"hasValue": True}})
            if "sameTarget" in expr:
                return httpx.Response(200, json={
                    "success": True,
                    "data": {"href": "https://vault.example.org/login", "hasFocus": True, "topLevel": True, "sameTarget": True},
                })
            if "formAction" in expr:
                return httpx.Response(200, json={
                    "success": True,
                    "data": {
                        "href": "https://vault.example.org/login",
                        "hasFocus": True,
                        "topLevel": True,
                        "fieldOk": True,
                        "formAction": "https://vault.example.org/auth",
                    },
                })
            return httpx.Response(200, json={"success": True, "data": True})
        if path == "/v1/browser/page/markdown":
            # Hostile page echoes the password and username back in page text!
            return httpx.Response(200, json={
                "success": True,
                "data": {"markdown": f"Error for {canary_user}: invalid password {canary_pass}"},
            })
        if path == "/v1/browser/screenshot":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nfake")
        return httpx.Response(200, json={"success": True, "data": {"exit_code": 0}})

    class FakeDescriber:
        async def describe_desktop(self, screenshot: bytes) -> str:
            return f"Screen shows {canary_user} and {canary_pass}"

    app.media_describer = FakeDescriber()

    async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox_handler)) as http:
        client = SandboxClient("http://127.0.0.1:18080", "sb-k", http)

        class Sbx(Sandbox):
            async def ensure_running(self):
                return client

        worker = await worker_for(
            app,
            call("fill_saved_login", login_id=meta["id"]),
            call("read_page"),
            call("look"),
            call("finish", result=f"Tried {canary_user} with {canary_pass}"),
            {"content": '{"bubbles":[{"text":"Done."}]}'},
        )
        worker.sandbox = Sbx()
        task_id = await worker.enqueue(OWNER, "Log in")
        await wait_task(worker)

    # B2(b): After Enter (0xFF0D), another Ctrl+A + BackSpace clear was sent over VNC!
    all_keysyms = [item for s in vnc_sessions for item in s.typed_keysyms]
    enter_indices = [i for i, (sym, down) in enumerate(all_keysyms) if sym == 0xFF0D and down]
    assert enter_indices, "Enter must always be sent"
    after_enter = all_keysyms[enter_indices[-1] + 1 :]
    assert any(sym == 0xFF08 and down for sym, down in after_enter), "Leftover password must be cleared over VNC after submit"

    # B2(c): Neither username nor password appears in model requests (read_page, look) or task result!
    model_dump = json.dumps(app.client.requests, default=str)
    assert canary_pass not in model_dump
    assert canary_user not in model_dump
    assert "[redacted]" in model_dump
    row = await task_row(app, task_id)
    assert canary_pass not in (row["result"] or "")
    assert canary_user not in (row["result"] or "")

    # Also verify turn_log and usage scrub active per-run redactions when recorded inside a scope.
    from dom import turn_log
    from dom.secrets import secret_redaction_scope
    from dom.usage import UsageRecorder

    with secret_redaction_scope([canary_user, canary_pass]):
        async with turn_log.capture(app.store, app.dispatch.now(), OWNER, "owner_dm"):
            turn_log.step(f"saw {canary_user} and {canary_pass}")
            turn_log.outcome(f"done {canary_pass}")
        await UsageRecorder(app.store).record("desktop", f"m-{canary_pass}", f"h-{canary_user}", {}, OWNER)
    turns_rows = await app.store._run(lambda: [dict(r) for r in app.store._conn.execute("SELECT * FROM turns").fetchall()])
    usage_rows = await app.store._run(lambda: [dict(r) for r in app.store._conn.execute("SELECT * FROM usage").fetchall()])
    assert canary_pass not in json.dumps(turns_rows) and canary_user not in json.dumps(turns_rows)
    assert canary_pass not in json.dumps(usage_rows) and canary_user not in json.dumps(usage_rows)


async def test_s1_reverify_before_each_typing_step_uses_evaluate_origin_focus_and_form_action(make_app, monkeypatch):
    """S1: Re-verify via single /page/evaluate before username AND password; refuse unfocused, wrong origin, or cross-site form action."""
    app = make_app()
    meta = await app.desktop_permissions.save_login(site="bank.example.org", username="alice@example.org", password="pw-s1-secret")
    await app.desktop_permissions.set_site_rule("bank.example.org", "fill_login", "allow")

    for scenario, eval_payload, expected_err in [
        (
            "no_focus",
            {"href": "https://bank.example.org/login", "hasFocus": False, "topLevel": True, "fieldOk": True, "formAction": None},
            "focus",
        ),
        (
            "wrong_origin_from_eval_despite_tabs_api",
            {"href": "https://phish.example.net/login", "hasFocus": True, "topLevel": True, "fieldOk": True, "formAction": None},
            "https://",
        ),
        (
            "cross_site_form_action",
            {"href": "https://bank.example.org/login", "hasFocus": True, "topLevel": True, "fieldOk": True, "formAction": "https://evil.example.net/steal"},
            "form",
        ),
        (
            "downgraded_http_form_action",
            {"href": "https://bank.example.org/login", "hasFocus": True, "topLevel": True, "fieldOk": True, "formAction": "http://bank.example.org/login"},
            "form",
        ),
    ]:
        vnc_called = False

        @asynccontextmanager
        async def mock_vnc_connect(url, **kwargs):
            nonlocal vnc_called
            vnc_called = True
            yield FakeVncSocket()

        monkeypatch.setattr("dom.desktop.vnc_type.connect_vnc", mock_vnc_connect)

        def handler(request: httpx.Request, payload=eval_payload) -> httpx.Response:
            if request.url.path == "/v1/browser/tabs":
                # Tabs API still claims bank.example.org! S1 must trust evaluate's href instead.
                return httpx.Response(200, json={"success": True, "data": [{"url": "https://bank.example.org/login", "is_active": True}]})
            if request.url.path == "/v1/browser/page/evaluate":
                expr = json.loads(request.content)["expression"]
                if "formAction" in expr:
                    return httpx.Response(200, json={"success": True, "data": payload})
                return httpx.Response(200, json={"success": True, "data": True})
            return httpx.Response(200, json={"success": True, "data": {"exit_code": 0}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SandboxClient("http://127.0.0.1:18080", "k", http)

            class Sbx(Sandbox):
                async def ensure_running(self):
                    return client

            worker = await worker_for(
                app,
                call("fill_saved_login", login_id=meta["id"]),
                call("finish", result=f"stopped {scenario}"),
                {"content": '{"bubbles":[{"text":"ok"}]}'},
            )
            worker.sandbox = Sbx()
            await worker.enqueue(OWNER, f"Test {scenario}")
            await wait_task(worker)
            assert not vnc_called, f"VNC should not have been called for scenario {scenario}"
            assert expected_err in json.dumps(app.client.requests).lower(), f"Missing expected refusal for {scenario}"


async def test_s2_focus_stolen_mid_typing_aborts_clears_and_alerts_owner(make_app, monkeypatch):
    """S2: If focus leaves the password input mid-typing, do not submit, clear over VNC, and alert owner DM."""
    app = make_app()
    meta = await app.desktop_permissions.save_login(site="app.example.org", username="", password="pw-s2-stolen")
    await app.desktop_permissions.set_site_rule("app.example.org", "fill_login", "allow")

    vnc_sessions: list[FakeVncSocket] = []

    @asynccontextmanager
    async def mock_vnc_connect(url, **kwargs):
        s = FakeVncSocket()
        vnc_sessions.append(s)
        yield s

    monkeypatch.setattr("dom.desktop.vnc_type.connect_vnc", mock_vnc_connect)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/browser/tabs":
            return httpx.Response(200, json={"success": True, "data": [{"url": "https://app.example.org/login", "is_active": True}]})
        if request.url.path == "/v1/browser/page/evaluate":
            expr = json.loads(request.content)["expression"]
            if "sameTarget" in expr:
                # Focus was stolen mid-typing!
                return httpx.Response(200, json={
                    "success": True,
                    "data": {"href": "https://app.example.org/login", "hasFocus": True, "topLevel": True, "sameTarget": False},
                })
            if "formAction" in expr:
                return httpx.Response(200, json={
                    "success": True,
                    "data": {"href": "https://app.example.org/login", "hasFocus": True, "topLevel": True, "fieldOk": True, "formAction": None},
                })
            return httpx.Response(200, json={"success": True, "data": True})
        return httpx.Response(200, json={"success": True, "data": {"exit_code": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SandboxClient("http://127.0.0.1:18080", "k", http)

        class Sbx(Sandbox):
            async def ensure_running(self):
                return client

        worker = await worker_for(
            app,
            call("fill_saved_login", login_id=meta["id"]),
            call("finish", result="aborted"),
            {"content": '{"bubbles":[{"text":"stopped"}]}'},
        )
        worker.sandbox = Sbx()
        await worker.enqueue(OWNER, "Login")
        await wait_task(worker)

    # Must NOT have pressed Enter, MUST have sent a follow-up clear (BackSpace) after typing.
    all_keysyms = [item for s in vnc_sessions for item in s.typed_keysyms]
    assert not any(sym == 0xFF0D and down for sym, down in all_keysyms), "Must not press Enter when focus was stolen"
    assert len(vnc_sessions) >= 2 and any(sym == 0xFF08 and down for sym, down in vnc_sessions[-1].typed_keysyms)
    # Security alert sent to owner DM and recorded as owner event (with no password leaked).
    dm_texts = " ".join(str(c) for c in app.bot.of("send", OWNER))
    assert "focus" in dm_texts.lower() or "security" in dm_texts.lower()
    assert "pw-s2-stolen" not in dm_texts


async def test_s3_real_websockets_client_debug_logging_never_prints_password(caplog):
    """S3: Real websockets client against local RFB server with DEBUG logging must never log password frames."""
    from websockets.asyncio.server import serve
    from dom.desktop.vnc_type import vnc_type_text

    canary = "WS_DEBUG_CANARY_Z9q!"
    received_keysyms: list[tuple[int, bool]] = []

    async def rfb_server(ws):
        await ws.send(b"RFB 003.008\n")
        await ws.recv()  # client version
        await ws.send(b"\x01\x01")  # 1 security type: None
        await ws.recv()  # chosen security type
        await ws.send(b"\x00\x00\x00\x00")  # OK
        await ws.recv()  # ClientInit
        await ws.send(b"\x00" * 20 + struct.pack("!I", 3) + b"dom")  # ServerInit
        async for msg in ws:
            data = msg if isinstance(msg, bytes) else msg.encode("latin-1")
            if len(data) >= 8 and len(data) % 8 == 0 and data[0] == 4:
                for off in range(0, len(data), 8):
                    t, down, sym = struct.unpack("!BBxxI", data[off : off + 8])
                    if t == 4:
                        received_keysyms.append((sym, bool(down)))

    caplog.set_level(logging.DEBUG)
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    logging.getLogger("websockets.client").setLevel(logging.DEBUG)
    server_logger = logging.getLogger("test.rfb_server")
    server_logger.setLevel(logging.WARNING)
    async with serve(rfb_server, "127.0.0.1", 0, subprotocols=["binary"], logger=server_logger) as server:
        port = server.sockets[0].getsockname()[1]
        caplog.clear()
        async with httpx.AsyncClient() as http:
            client = SandboxClient(f"http://127.0.0.1:{port}", "test-key", http)
            await vnc_type_text(client, canary, clear=True, submit=True)

    assert decode_typed_chars(received_keysyms) == canary
    assert any(sym == 0xFF0D and down for sym, down in received_keysyms)
    # No frame logs from websockets.client (or any websockets logger) and no password bytes/hex may appear in caplog!
    ws_records = [r for r in caplog.records if r.name.startswith("websockets") and r.levelno < logging.WARNING]
    assert ws_records == [], f"Unexpected websockets DEBUG/INFO logs: {[r.getMessage() for r in ws_records]}"
    assert canary not in caplog.text
    assert "BINARY" not in caplog.text


async def test_s4_concurrent_dotenv_writes_and_cli_rotate_refuses_running_bot(tmp_path, make_app, capsys):
    """S4: Concurrent .env writes never lose keys; CLI rotate-key refuses while ready.json shows a live bot PID."""
    import concurrent.futures
    from dom.config import set_dotenv_many

    def writer(i: int) -> None:
        for j in range(10):
            set_dotenv_many(tmp_path, {f"KEY_{i}_{j}": f"val_{i}_{j}"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(writer, i) for i in range(6)]
        for f in futs:
            f.result()

    env = _read_dotenv(tmp_path / ".env")
    for i in range(6):
        for j in range(10):
            assert env.get(f"KEY_{i}_{j}") == f"val_{i}_{j}"

    # CLI rotate-key refuses while ready.json references a live PID.
    from dom.release import mark_ready

    app = make_app()
    await app.secrets.put("owner_secret", "k", {"value": "v"})
    app.store.close()
    (app.home.root / "config.toml").write_text('timezone = "UTC"\n[models.chat]\nbase_url = "http://localhost:8000/v1"\nmodel = "m"\n')
    mark_ready(app.home.root, pid=os.getpid())
    code = await asyncio.to_thread(cli_main, ["rotate-key", str(app.home.root)])
    assert code == 1
    assert "running" in capsys.readouterr().out.lower()


async def test_s5_e101_does_not_spend_request_on_validation_or_write_failure(make_app, monkeypatch):
    """S5: Invalid input or write failure must leave the E101 secret request pending so the owner can retry."""
    app = make_app()
    # 1) Login target with invalid site fails validation WITHOUT burning the request.
    req = await app.secret_requests.create(label="Portal", reason="sign in", target="login", key="")
    with pytest.raises(ValueError):
        await app.secret_requests.submit(request_id=req["request_id"], site="192.0.2.1", username="u", password="pw")
    assert await app.secret_requests.lookup(request_id=req["request_id"]) is not None
    # Retry with valid site succeeds!
    res = await app.secret_requests.submit(request_id=req["request_id"], site="portal.example.org", username="u", password="pw")
    assert res["status"] == "set"

    # 2) Write failure reverts the request back to pending.
    req2 = await app.secret_requests.create(label="API", reason="need it", target="secret", key="api_s5")
    orig_put = app.secrets.put

    async def boom(*a, **kw):
        raise SecretStoreError("simulated disk error")

    monkeypatch.setattr(app.secrets, "put", boom)
    with pytest.raises(SecretStoreError):
        await app.secret_requests.submit(request_id=req2["request_id"], value="val-123")
    assert await app.secret_requests.lookup(request_id=req2["request_id"]) is not None

    monkeypatch.setattr(app.secrets, "put", orig_put)
    res2 = await app.secret_requests.submit(request_id=req2["request_id"], value="val-123")
    assert res2["status"] == "set"


async def test_s6_bootstrap_refuses_when_encrypted_rows_exist_and_key_missing(make_app):
    """S6: If encrypted rows exist and DOM_MASTER_KEY is missing, put() must refuse instead of minting a new key."""
    app = make_app()
    await app.secrets.put("owner_secret", "existing", {"value": "keep-me"})
    set_dotenv(app.home.root, MASTER_KEY_ENV, None)
    with pytest.raises(SecretStoreError, match="missing"):
        await app.secrets.put("owner_secret", "new_one", {"value": "should-fail"})
    assert MASTER_KEY_ENV not in _read_dotenv(app.home.root / ".env")


async def test_s7_e101_env_allowlist_and_dismissal_cooldown(make_app):
    """S7: E101 env target allows only config-referenced non-reserved names, and dismissal triggers a cooldown."""
    clock = Clock(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc))
    app = make_app()
    app.dispatch.now = clock

    for bad in ("PATH", "LD_PRELOAD", "PYTHONSTARTUP", "HTTPS_PROXY", "SSL_CERT_FILE", "DOM_HOME", "DOM_MASTER_KEY", "TELEGRAM_BOT_TOKEN", "SANDBOX_API_KEY", "RANDOM_UNREFERENCED_VAR"):
        with pytest.raises(ValueError):
            await app.secret_requests.create(label="Bad", reason="nope", target="env", key=bad)

    # Config-referenced non-reserved env names (KLIPY_API_KEY, STUDIO_TOKEN) are allowed.
    ok_req = await app.secret_requests.create(label="KLIPY", reason="GIF search", target="env", key="KLIPY_API_KEY")
    assert await app.secret_requests.cancel(request_id=ok_req["request_id"]) is True

    # Dismissal cooldown prevents immediate re-request spam.
    with pytest.raises(ValueError, match="wait|dismissed|cooldown"):
        await app.secret_requests.create(label="Again", reason="spam", target="secret", key="spam_key")

    # After cooldown expires, requests are allowed again.
    clock.advance(minutes=11)
    ok2 = await app.secret_requests.create(label="After cooldown", reason="ok now", target="secret", key="after_cd")
    assert ok2["status"] == "requested"


async def test_s8_visit_site_never_enforced_on_browse_redirects(make_app):
    """S8: browse to an allowed site that redirects to a visit_site=never site must close the page."""
    app = make_app()
    await app.desktop_permissions.set_site_rule("evil.example.org", "visit_site", "never")
    navigated: list[str] = []

    worker = await worker_for(
        app,
        call("browse", url="https://good.example.org/redirect"),
        call("finish", result="redirect blocked"),
        {"content": '{"bubbles":[{"text":"blocked"}]}'},
    )

    async def redirecting_nav(url: str):
        navigated.append(url)
        if url == "https://good.example.org/redirect":
            worker.sandbox.url = "https://evil.example.org/landing"
        else:
            worker.sandbox.url = url

    worker.sandbox.navigate = redirecting_nav
    await worker.enqueue(OWNER, "Open link")
    await wait_task(worker)
    assert "about:blank" in navigated
    assert "evil.example.org is set to Never" in json.dumps(app.client.requests)


async def test_nits_strict_base64_ports_psl_allow_always_and_tampered_screenshot(make_app):
    """Nits 1-5: strict base64 key decoding, port check, extended shared suffixes, allow-always on non-site, and screenshot tamper check."""
    from dom.secrets import _decode_key

    # Nit 1: strict base64 decoding rejects garbage punctuation injected into valid key.
    valid = generate_master_key()
    corrupted = valid[:10] + "!!" + valid[10:]
    with pytest.raises(SecretStoreError):
        _decode_key(corrupted)

    # Nit 2: non-443 ports must not match in host_matches_site.
    assert host_matches_site("https://example.org:8443/login", "example.org") is False
    assert host_matches_site("https://example.org:443/login", "example.org") is True

    # Nit 3: extended shared suffixes rejected by normalize_site and match_subdomains.
    for suffix in ("webflow.io", "notion.site", "github.dev", "carrd.co", "framer.app", "my.id"):
        assert normalize_site(suffix) is None, f"{suffix} should be rejected"
        assert host_matches_site(f"https://tenant.{suffix}/login", suffix, match_subdomains=True) is False

    # Nit 4: Allow always on a site that normalizes to nothing must not claim Allowed always.
    app = make_app()
    req_id = await app.store.create_desktop_permission_request(
        action="download", site="", detail="file.pdf", token_hash="h", created_at=app.desktop_permissions._now(),
    )
    ok, label = await app.desktop_permissions.decide(req_id, "always", via="dashboard")
    assert ok
    assert "always" not in label.lower()
    row = await app.store.get_desktop_permission_request(req_id)
    assert row is not None and row["status"] == "allowed_once"

    # Nit 5: Overwriting a saved screenshot path with different bytes must NOT bypass the download gate,
    # whereas an unmodified screenshot still passes when download=never.
    await app.desktop_permissions.set_policy("download", "never")
    original_png = b"\x89PNG\r\n\x1a\noriginal-screenshot-bytes"

    async def fake_screenshot():
        return original_png

    async def fake_upload(path, data):
        return {"success": True}

    async def tampered_download(path, max_bytes=None):
        return b"not-the-original-screenshot-secret-file"

    worker = await worker_for(
        app,
        call("save_screenshot", name="page"),
        call("finish", result="sending", files=["/home/gem/outbox/page.png"]),
        call("finish", result="blocked tampered screenshot"),
        {"content": '{"bubbles":[{"text":"ok"}]}'},
    )
    worker.sandbox.screenshot = fake_screenshot
    worker.sandbox.upload_file = fake_upload
    worker.sandbox.download_file = tampered_download
    await worker.enqueue(OWNER, "Screenshot")
    await wait_task(worker)
    assert "download is set to Never" in json.dumps(app.client.requests)

    # Unmodified screenshot passes even with download=never.
    async def unmodified_download(path, max_bytes=None):
        return original_png

    worker_ok = await worker_for(
        app,
        call("save_screenshot", name="page"),
        call("finish", result="here is screenshot", files=["/home/gem/outbox/page.png"]),
        {"content": '{"bubbles":[{"text":"ok"}]}'},
    )
    worker_ok.sandbox.screenshot = fake_screenshot
    worker_ok.sandbox.upload_file = fake_upload
    worker_ok.sandbox.download_file = unmodified_download
    tid_ok = await worker_ok.enqueue(OWNER, "Good screenshot")
    await wait_task(worker_ok)
    assert (await task_row(app, tid_ok))["status"] == "done"

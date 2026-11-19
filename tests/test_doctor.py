import json
import os
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from dom import doctor
from dom.cli import main
from dom.home import MAX_SOUL, init_home
from dom.store.store import TARGET_VERSION, migrate_database

BOT_TOKEN = "12345:test-telegram-secret"
MODEL_KEY = "test-provider-secret"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    init_home(root, "UTC")
    (root / ".env").write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nOPENROUTER_API_KEY={MODEL_KEY}\n")
    config = root / "config.toml"
    config.write_text("owner_id = 12345\n" + config.read_text())
    migrate_database(root / "state.db")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / f"dom-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.tar.gz").touch()
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=3_000_000_000))
    monkeypatch.setattr(doctor.PluginRunner, "self_test", lambda self: True)
    monkeypatch.setattr(doctor, "bwrap_available", lambda runner: True)
    return root


def change_config(home, old, new):
    config = home / "config.toml"
    config.write_text(config.read_text().replace(old, new))


def test_offline_healthy_and_never_requests_http(home, monkeypatch, capsys):
    def no_http(*args, **kwargs):
        pytest.fail("offline doctor must not create an HTTP client")

    monkeypatch.setattr(doctor.httpx, "AsyncClient", no_http)
    assert main(["doctor", str(home)]) == 0
    out = capsys.readouterr().out
    for label in ("config", "secrets", "owner", "timezone", "SOUL.md", "memory", "state.db",
                  "backups", "disk", "web", "desktop", "bwrap", "plugin sandbox"):
        assert f"ok {label}:" in out
    assert BOT_TOKEN not in out and MODEL_KEY not in out


@pytest.mark.parametrize(("old", "new", "label", "status", "hint"), [
    ('timezone = "UTC"', 'timezone = [', "config", "fail", "config.toml"),
    ('timezone = "UTC"', 'timezone = ["hidden-value"]', "config", "fail", "config.toml"),
    ('timezone = "UTC"', 'timezone = "Unusable/Timezone"', "timezone", "fail", "IANA"),
    ("owner_id = 12345\n", "", "owner", "warn", "/claim"),
    ("[models.chat]", "[models.unused]", "config", "fail", "[models.chat]"),
    ("[web]\nenabled = false", "[web]\nenabled = true", "web", "fail", "access_team"),
    ('mode = "off"', 'mode = "local"', "desktop", "fail", "lan_probes"),
    ('mode = "off"', 'mode = "remote"', "desktop", "fail", "URL"),
])
def test_config_diagnostics(home, capsys, old, new, label, status, hint):
    change_config(home, old, new)
    code = main(["doctor", str(home)])
    out = capsys.readouterr().out
    assert code == int(status == "fail")
    assert f"{status} {label}:" in out and hint in out
    assert "hidden-value" not in out


def test_missing_secrets_lists_names_only(home, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (home / ".env").unlink()
    assert main(["doctor", str(home)]) == 1
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN" in out and "OPENROUTER_API_KEY" in out


@pytest.mark.parametrize(("relative", "value"), [
    ("private/NOTES.md", "x" * 2201),
    ("memory/people/100.md", "x" * 1001),
])
def test_oversized_notes_are_a_warning(home, capsys, relative, value):
    # Imported notes often start over their limit and Dom trims them; that is not a broken install.
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    main(["doctor", str(home)])
    assert "warn memory:" in capsys.readouterr().out


@pytest.mark.parametrize(("relative", "value", "label"), [
    ("SOUL.md", None, "SOUL.md"),
    ("SOUL.md", "x" * (MAX_SOUL + 1), "SOUL.md"),
    ("state.db", "not a database", "state.db"),
])
def test_invalid_files(home, capsys, relative, value, label):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if value is None:
        path.unlink()
    else:
        path.write_text(value)
    assert main(["doctor", str(home)]) == 1
    assert f"fail {label}:" in capsys.readouterr().out


def test_wrong_db_version_is_reported_without_migrating(home, capsys):
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute(f"PRAGMA user_version={TARGET_VERSION - 1}")
    assert main(["doctor", str(home)]) == 1
    assert f"expected {TARGET_VERSION}" in capsys.readouterr().out
    with sqlite3.connect(home / "state.db") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (TARGET_VERSION - 1,)


def test_missing_db_is_a_first_run_warning_and_is_not_created(home, capsys):
    (home / "state.db").unlink()
    assert main(["doctor", str(home)]) == 0
    assert "warn state.db:" in capsys.readouterr().out
    assert not (home / "state.db").exists()


@pytest.mark.parametrize(("free", "status", "code"), [
    (2_000_000_000, "ok", 0), (500_000_000, "warn", 0), (499_999_999, "fail", 1),
])
def test_disk_thresholds_and_old_backup(home, monkeypatch, capsys, free, status, code):
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    archive = next((home.parent / "backups").iterdir())
    os.utime(archive, (0, 0))
    assert main(["doctor", str(home)]) == code
    out = capsys.readouterr().out
    assert f"{status} disk:" in out and "warn backups:" in out


def test_web_dev_token_reuses_auth_and_does_not_print_token(home, capsys):
    change_config(home, "[web]\nenabled = false", "[web]\nenabled = true")
    change_config(home, 'dev_token_env = ""', 'dev_token_env = "DASHBOARD_KEY"')
    token = "private-local-token"
    with (home / ".env").open("a") as env:
        env.write(f"DASHBOARD_KEY={token}\n")
    assert main(["doctor", str(home)]) == 1
    out = capsys.readouterr().out
    assert "fail web:" in out and "32 characters" in out and token not in out


def test_local_desktop_and_optional_plugin_warnings(home, monkeypatch, capsys):
    change_config(home, 'mode = "off"', 'mode = "local"')
    change_config(home, "lan_probes = []", 'lan_probes = ["192.0.2.1:443"]')
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "bwrap_available", lambda runner: False)
    assert main(["doctor", str(home)]) == 1
    out = capsys.readouterr().out
    assert "fail desktop:" in out and "Docker" in out
    assert "warn bwrap:" in out and "warn plugin sandbox:" in out
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(doctor, "bwrap_available", lambda runner: True)
    monkeypatch.setattr(doctor.PluginRunner, "self_test", lambda self: False)
    assert main(["doctor", str(home)]) == 0
    out = capsys.readouterr().out
    assert "ok desktop:" in out and "warn plugin sandbox:" in out


def test_remote_desktop_configuration_needs_isolation_confirmation(home, capsys):
    change_config(home, 'mode = "off"', 'mode = "remote"')
    change_config(home, 'url = ""', 'url = "https://desktop.example.org"')
    assert main(["doctor", str(home)]) == 1
    assert "remote_isolation_confirmed" in capsys.readouterr().out
    change_config(home, "remote_isolation_confirmed = false", "remote_isolation_confirmed = true")
    assert main(["doctor", str(home)]) == 0
    assert "ok desktop:" in capsys.readouterr().out


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(doctor.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


def test_online_probes_use_only_synthetic_data(home, monkeypatch, capsys, caplog):
    caplog.set_level("INFO", logger="httpx")
    path = home / "config.toml"
    path.write_text(path.read_text() + """
[models.chat_fallback]
base_url = "http://localhost:8000/v1"
model = "test-fallback"
""")
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.host == "api.telegram.org":
            assert BOT_TOKEN in request.url.path
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"is_bot": True}})
            assert json.loads(request.content) == {"scope": {"type": "chat", "chat_id": 12345}}
            return httpx.Response(200, json={"ok": True, "result": [{"command": "status"}]})
        if request.url.host == "openrouter.ai":
            assert request.headers["Authorization"] == f"Bearer {MODEL_KEY}"
        if request.url.path.endswith("/decisions"):
            body = json.loads(request.content)
            assert body["state"] == "Health check."
            assert set(body["questions"]["check"]["criteria"]) == {"yes", "no"}
            return httpx.Response(200, json={"answers": {"check": {"type": "choice", "choice": "yes"}}})
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit_remaining": 3.25, "label": MODEL_KEY}})
        assert json.loads(request.content)["max_tokens"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    mock_http(monkeypatch, handler)
    assert main(["doctor", str(home), "--online"]) == 0
    out = capsys.readouterr().out
    assert calls.count("completions") == 4 and calls.count("key") == 1
    assert {"getMe", "getMyCommands", "decisions"} <= set(calls)
    assert "warn Telegram privacy:" in out and "BotFather" in out
    assert "ok Telegram getMyCommands:" in out and "$3.25" in out
    assert BOT_TOKEN not in out + caplog.text and MODEL_KEY not in out + caplog.text


def test_online_reports_failures_without_response_bodies_or_urls(home, monkeypatch, capsys):
    def handler(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {
                "is_bot": True, "can_read_all_group_messages": True, "username": "private-name",
            }})
        if request.url.path.endswith("/getMyCommands"):
            return httpx.Response(200, json={"ok": False, "description": BOT_TOKEN})
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit_remaining": None}})
        if request.url.path.endswith("/decisions"):
            return httpx.Response(200, json={"answers": {}})
        return httpx.Response(401, text=MODEL_KEY)

    mock_http(monkeypatch, handler)
    assert main(["doctor", str(home), "--online"]) == 1
    out = capsys.readouterr().out
    assert "ok Telegram privacy:" in out
    assert "fail Telegram getMyCommands:" in out and "fail models.chat:" in out
    assert "fail models.triage:" in out and "no key spend limit" in out
    assert not any(secret in out for secret in (BOT_TOKEN, MODEL_KEY, "private-name", "https://"))


def test_online_rejected_token(home, monkeypatch, capsys):
    mock_http(monkeypatch, lambda _: httpx.Response(401, text=BOT_TOKEN))
    assert main(["doctor", str(home), "--online"]) == 1
    out = capsys.readouterr().out
    assert "fail Telegram getMe:" in out and BOT_TOKEN not in out


async def test_model_probe_names_a_data_policy_block():
    import httpx
    from dom.config import ModelEndpoint
    from dom.doctor import _model

    def handler(request):
        return httpx.Response(404, json={"error": {"message": "0 endpoints available: ZDR violation (account settings)"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        check = await _model(http, "media", ModelEndpoint(base_url="https://example.org/v1", model="m", api_key_env="K"), "key")
    assert check.status == "fail" and "zero data retention" in check.detail


@pytest.mark.parametrize(("body", "zdr"), [
    ('{"error": {"message": "ZDR violation"}}', True),
    ('{"error": "data policy block"}', True),
    ('{"error": null}', False),
    ('{"error": []}', False),
    ('{"error": {"message": null}}', False),
    ('["service unavailable"]', False),
    ('null', False),
    ('"service unavailable"', False),
    ('service unavailable', False),
    ('provider data policy blocked the request', True),
    ('["Zero Data Retention required"]', True),
])
def test_probe_hint_accepts_different_error_bodies(body, zdr):
    hint = doctor._probe_hint(httpx.Response(403, content=body))
    assert hint.startswith("HTTP 403:")
    assert ("zero data retention" in hint) is zdr


@pytest.mark.parametrize("policy", [False, True])
def test_online_probe_hints_redact_provider_text(home, monkeypatch, capsys, policy):
    fake_key = "sk-" + "ExampleFakeKeyOnlyForTests"
    escape = "\x1b[31m"
    reason = "request blocked by data policy" if policy else "request failed"
    message = f"{reason}: {fake_key} {escape}"
    mock_http(monkeypatch, lambda _: httpx.Response(403, json={"error": {"message": message}}))

    assert main(["doctor", str(home), "--online"]) == 1
    out = capsys.readouterr().out
    assert "fail models.chat: HTTP 403:" in out
    assert ("zero data retention" in out) is policy
    assert reason not in out and fake_key not in out and escape not in out

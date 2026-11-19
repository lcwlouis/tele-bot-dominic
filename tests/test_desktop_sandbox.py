import json

import httpx
import pytest

from dom.capabilities import describe
from dom.config import Config, DesktopConfig, load_config
from dom.desktop.client import SandboxClient, SandboxError
from dom.desktop.sandbox import LocalSandbox, RemoteSandbox


class FakeDocker:
    def __init__(self, image=True):
        self.calls = []
        self.image = image
        self.running = False

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        command = args[1]
        if command == "image":
            code, output = (0 if self.image else 1), b""
        elif command == "container":
            code, output = (0, b"true") if self.running else (1, b"")
        else:
            self.running = command == "run"
            code, output = 0, b""
        return Process(code, output)


class Process:
    def __init__(self, code, output):
        self.returncode = code
        self.output = output

    async def communicate(self):
        return self.output, b"secret-key"


async def test_local_start_exact_arguments_and_idle_stop(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("dom.desktop.sandbox.secrets.token_urlsafe", lambda _: "secret-key")
    docker = FakeDocker()
    now = [0]
    observed = []

    def reply(request):
        observed.append(sandbox.status())
        assert request.headers["authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"success": True, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        config = Config(home=tmp_path / "home", desktop_mode="local")
        sandbox = LocalSandbox(config, run=docker, http=http, clock=lambda: now[0])
        assert sandbox.status() == "stopped"
        assert await sandbox.ensure_running() is sandbox.client
        assert sandbox.status() == "ready"
        assert observed == ["starting"]
        assert docker.calls == [
            ("docker", "image", "inspect", config.desktop.image),
            ("docker", "container", "inspect", "--format", "{{.State.Running}}", "dom-desktop"),
            ("docker", "run", "-d", "--name", "dom-desktop", "--pull=never",
             "--network", "dom-desktop", "-p", "127.0.0.1:18080:8080",
             "--shm-size", "1g", "--memory", "1600m", "--cpus", "3",
             "-e", "SANDBOX_API_KEY=secret-key", "-e", "DISABLE_CODE_SERVER=true",
             "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true",
             "-v", f"{tmp_path}/desktop-profile:/home/gem/.config/browser", config.desktop.image),
        ]
        assert await sandbox.ensure_running() is sandbox.client
        now[0] = 599
        sandbox.touch()
        now[0] = 600
        await sandbox.stop_if_idle()
        assert sandbox.status() == "ready"
        now[0] = 1199
        await sandbox.stop_if_idle()
        assert docker.calls[-2:] == [("docker", "stop", "dom-desktop"), ("docker", "rm", "dom-desktop")]
        assert sandbox.status() == "stopped"
        assert sandbox.client is None
        await sandbox.close()
    assert "secret-key" not in caplog.text


async def test_missing_image_is_an_error(tmp_path):
    docker = FakeDocker(image=False)
    sandbox = LocalSandbox(Config(home=tmp_path, desktop_mode="local"), run=docker)
    with pytest.raises(SandboxError, match="image not present"):
        await sandbox.ensure_running()
    assert sandbox.status() == "error"
    assert [call[1] for call in docker.calls] == ["image"]
    await sandbox.close()


async def test_remote_health_check(make_app, tmp_path):
    app = make_app()
    app.config = Config(home=tmp_path, desktop_mode="remote")
    seen = []

    def reply(request):
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer fake"
        return httpx.Response(200, json={"success": True, "data": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        app.sandbox = RemoteSandbox("https://example.org", "fake", http=http)
        assert "desktop: available (starts on use)" in describe(app, "owner_dm")
        await app.sandbox.ensure_running()
        assert "desktop: ready" in describe(app, "owner_dm")
        assert "desktop: off" in describe(app, "group")
        assert seen == ["https://example.org/v1/sandbox"]


def test_desktop_config_defaults(tmp_path):
    (tmp_path / "config.toml").write_text('[desktop]\nmode = "local"\n')
    config = load_config(tmp_path)
    assert config.desktop_mode == "local"
    assert config.desktop == DesktopConfig()
    assert config.desktop.port == 18080
    assert config.desktop.idle_minutes == 10


@pytest.mark.parametrize(("method", "args", "verb", "path", "body", "timeout"), [
    ("health", (), "GET", "sandbox", None, 30),
    ("navigate", ("https://example.org",), "POST", "browser/page/navigate", {"url": "https://example.org"}, 45),
    ("markdown", (), "GET", "browser/page/markdown", None, 30),
    ("elements", (), "GET", "browser/page/elements", None, 30),
    ("click", (3,), "POST", "browser/page/click", {"index": 3}, 30),
    ("click", ("button",), "POST", "browser/page/click", {"selector": "button"}, 30),
    ("fill", ("input", "hi"), "POST", "browser/page/fill", {"selector": "input", "text": "hi"}, 30),
    ("type", ("hi",), "POST", "browser/page/type", {"text": "hi"}, 30),
    ("press_key", ("Enter",), "POST", "browser/page/press_key", {"key": "Enter"}, 30),
    ("scroll", ("down",), "POST", "browser/page/scroll", {"direction": "down"}, 30),
    ("captcha_detect", (), "GET", "browser/captcha/detect", None, 30),
    ("bash", ("printf hi",), "POST", "bash/exec", {"command": "printf hi"}, 120),
    ("jupyter", ("print(1)",), "POST", "jupyter/execute", {"code": "print(1)"}, 120),
    ("read_file", ("/tmp/example",), "POST", "file/read", {"file": "/tmp/example"}, 30),
    ("write_file", ("/tmp/example", "hi"), "POST", "file/write", {"file": "/tmp/example", "content": "hi"}, 30),
])
async def test_client_shapes(method, args, verb, path, body, timeout):
    def reply(request):
        assert request.method == verb
        assert request.url.path == f"/v1/{path}"
        assert request.headers["authorization"] == "Bearer fake"
        assert request.extensions["timeout"]["read"] == timeout
        assert (json.loads(request.content) if request.content else None) == body
        return httpx.Response(200, json={"success": True, "message": "OK", "data": {"ok": True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        assert await getattr(SandboxClient("https://example.org/", "fake", http), method)(*args) == {"ok": True}


async def test_client_upload_download_and_screenshot():
    def reply(request):
        if request.url.path == "/v1/file/upload":
            assert request.method == "POST"
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert b'name="path"\r\n\r\n/home/gem/inbox/example.txt' in request.content
            assert b'name="file"; filename="example.txt"' in request.content
            return httpx.Response(200, json={"success": True, "data": {"file_size": 2}})
        if request.url.path == "/v1/file/download":
            assert request.url.params["path"] == "/tmp/example"
            return httpx.Response(200, content=b"hi")
        assert request.url.path == "/v1/browser/screenshot"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        client = SandboxClient("https://example.org", None, http)
        assert await client.upload_file("/home/gem/inbox/example.txt", b"hi") == {"file_size": 2}
        assert await client.download_file("/tmp/example") == b"hi"
        with pytest.raises(SandboxError, match="exceeds"):
            await client.download_file("/tmp/example", max_bytes=1)
        assert await client.screenshot() == b"png"


@pytest.mark.parametrize(("status", "payload", "message"), [
    (200, {"success": False, "message": "not ready"}, "not ready"),
    (400, {"success": False, "message": "bad path"}, "bad path"),
    (503, {}, "HTTP 503"),
    (200, {}, "invalid sandbox response"),
])
async def test_client_errors(status, payload, message):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload),
    )) as http:
        with pytest.raises(SandboxError, match=message):
            await SandboxClient("https://example.org", None, http).health()

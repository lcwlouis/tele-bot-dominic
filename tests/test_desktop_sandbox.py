import asyncio
import json
from pathlib import Path

import httpx
import pytest

from dom.capabilities import describe
from dom.config import Config, DesktopConfig, load_config
from dom.desktop.client import SandboxClient, SandboxError, url_host
from dom.desktop.network import PROBE_SCRIPT as PROBE
from dom.desktop.sandbox import LocalSandbox, RemoteSandbox
from dom.desktop.worker import DesktopWorker


PROBES = ["192.0.2.1:443", "192.0.2.2:22"]
NETWORK = [{"Driver": "bridge", "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": "172.30.0.0/24", "Gateway": "172.30.0.1"}]}}]


def local_config(tmp_path, **settings):
    settings = {"lan_probes": PROBES} | settings
    return Config(home=tmp_path / "home", desktop_mode="local", desktop=DesktopConfig(**settings))


class FakeDocker:
    def __init__(self, image=True, probe=0, network=NETWORK, probe_image=True):
        self.calls = []
        self.image = image
        self.probe = probe
        self.network = network
        self.probe_image = probe_image
        self.running = False
        self.environments = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        self.environments.append(kwargs.get("env"))
        command = args[1]
        if command == "image":
            present = self.probe_image if args[3] == "alpine:3.22" else self.image
            code, output = (0 if present else 1), b""
        elif command == "container":
            code, output = (0, b"true") if self.running else (1, b"")
        elif command == "network":
            code, output = (0, json.dumps(self.network).encode()) if self.network else (1, b"")
        elif command == "run" and "--rm" in args:
            code, output = self.probe, b""
        else:
            self.running = command == "run"
            code, output = 0, b""
        return Process(code, output)

    def started(self):
        return [call for call in self.calls if call[1] == "run" and "--rm" not in call]

    def probes(self):
        return [call for call in self.calls if call[1] == "run" and "--rm" in call]


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
        config = local_config(tmp_path)
        sandbox = LocalSandbox(config, run=docker, http=http, clock=lambda: now[0])
        assert sandbox.status() == "stopped"
        assert await sandbox.ensure_running() is sandbox.client
        assert sandbox.status() == "ready"
        assert observed == ["starting"]
        assert docker.calls == [
            ("docker", "image", "inspect", config.desktop.image),
            ("docker", "network", "inspect", "dom-desktop"),
            ("docker", "image", "inspect", "alpine:3.22"),
            probe_call(config, ["192.0.2.1:443", "192.0.2.2:22", "172.30.0.1:8080", "172.30.0.1:22"]),
            ("docker", "container", "inspect", "--format", "{{.State.Running}}", "dom-desktop"),
            ("docker", "run", "-d", "--name", "dom-desktop", "--pull=never",
             "--network", "dom-desktop", "-p", "127.0.0.1:18080:8080",
             "--shm-size", "1g", "--memory", "1600m", "--memory-swap", "1600m", "--pids-limit", "512",
             "--security-opt", "no-new-privileges", "--cpus", "3",
             "-e", "SANDBOX_API_KEY", "-e", "DISABLE_CODE_SERVER=true",
             "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true",
             "-v", f"{tmp_path}/desktop-profile:/home/gem/.config/browser", config.desktop.image),
        ]
        assert not any("secret-key" in str(call) for call in docker.calls)
        assert [env["SANDBOX_API_KEY"] for env in docker.environments if env] == ["secret-key"]
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


def probe_call(config, targets):
    settings = config.desktop
    return ("docker", "run", "--rm", "--pull=never", "--network", "dom-desktop", settings.probe_image,
            "sh", "-c", PROBE, "probe", "3", settings.probe_control, *targets)


async def test_missing_image_is_an_error(tmp_path):
    docker = FakeDocker(image=False)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
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
        app.sandbox = RemoteSandbox("https://example.org", "fake", http=http, isolation_confirmed=True)
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


async def start_failing(sandbox):
    with pytest.raises(SandboxError) as caught:
        await sandbox.ensure_running()
    return str(caught.value)


@pytest.mark.parametrize("probes", [
    [], ["not-a-target"], ["192.0.2.1:99999"], ["example.org:443"], ["[2001:db8::1]:443"], ["192.0.2.1"], ["192.0.2.256:80"],
])
def test_lan_probes_are_ipv4_literals_with_ports(probes):
    if probes:
        with pytest.raises(ValueError):
            DesktopConfig(lan_probes=probes)
    else:
        assert DesktopConfig().lan_probes == []


@pytest.mark.parametrize("control", ["example.org:443", "1.1.1.1", "10.0.0.1:443", "1.1.1.1:0"])
def test_probe_control_must_be_a_public_ipv4_literal_with_a_port(control):
    with pytest.raises(ValueError):
        DesktopConfig(probe_control=control)


def test_desktop_probe_defaults():
    settings = DesktopConfig()
    assert (settings.probe_image, settings.probe_control, settings.remote_isolation_confirmed) == ("alpine:3.22", "1.1.1.1:443", False)


async def test_local_mode_without_lan_probes_refuses_to_start(tmp_path):
    docker = FakeDocker()
    sandbox = LocalSandbox(local_config(tmp_path, lan_probes=[]), run=docker)
    assert "lan_probes" in await start_failing(sandbox)
    assert sandbox.status() == "error"
    assert docker.started() == [] and docker.probes() == []
    await sandbox.close()


async def test_reachable_lan_refuses_to_start_and_alerts_owner_once(tmp_path):
    alerts = []

    async def alert(text):
        alerts.append(text)

    docker = FakeDocker(probe=7)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    sandbox.alert = alert
    for _ in range(2):
        assert await start_failing(sandbox) == "LAN reachable from the sandbox — run dom desktop-setup"
        assert sandbox.status() == "error"
    assert docker.started() == []
    assert len(docker.probes()) == 2  # a failed start is verified again
    assert len(alerts) == 1 and "dom desktop-setup" in alerts[0]
    await sandbox.close()


async def test_probe_that_cannot_run_fails_closed(tmp_path):
    docker = FakeDocker(probe=125)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    assert "could not verify" in await start_failing(sandbox)
    assert docker.started() == []
    await sandbox.close()


async def test_firewall_is_probed_before_every_start(tmp_path):
    docker = FakeDocker()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": None}),
    )) as http:
        sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http)
        await sandbox.ensure_running()
        await sandbox.ensure_running()  # already running: no new start, no new probe
        assert len(docker.probes()) == 1
        await sandbox.stop()
        await sandbox.ensure_running()
        assert len(docker.probes()) == 2 and len(docker.started()) == 2
        # Each container start is preceded by its own probe.
        kinds = ["probe" if call in docker.probes() else "start" for call in docker.calls if call in docker.probes() or call in docker.started()]
        assert kinds == ["probe", "start", "probe", "start"]
        await sandbox.close()


async def test_probe_fails_closed_when_the_control_does_not_connect(tmp_path):
    docker = FakeDocker(probe=8)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    assert "could not verify" in await start_failing(sandbox)
    assert docker.started() == []
    await sandbox.close()


async def test_missing_probe_image_refuses_to_start(tmp_path):
    docker = FakeDocker(probe_image=False)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    assert "alpine:3.22" in await start_failing(sandbox)
    assert docker.started() == [] and docker.probes() == []
    await sandbox.close()


async def test_probe_targets_come_from_config(tmp_path):
    docker = FakeDocker(probe=7)
    config = local_config(tmp_path, probe_image="busybox:1", probe_control="9.9.9.9:53", lan_probes=["192.0.2.9:80"])
    sandbox = LocalSandbox(config, run=docker)
    await start_failing(sandbox)
    assert docker.probes() == [probe_call(config, ["192.0.2.9:80", "172.30.0.1:8080", "172.30.0.1:22"])]
    await sandbox.close()


@pytest.mark.parametrize("network", [
    None,
    [{**NETWORK[0], "EnableIPv6": True}],
    [{**NETWORK[0], "IPAM": {"Config": [{"Subnet": "172.31.0.0/24", "Gateway": "172.31.0.1"}]}}],
    [{**NETWORK[0], "IPAM": {"Config": [{"Subnet": "172.30.0.0/24", "Gateway": "172.30.0.1"}, {"Subnet": "fd00::/64"}]}}],
])
async def test_start_refused_when_the_network_is_not_the_configured_ipv4_bridge(tmp_path, network):
    docker = FakeDocker(network=network)
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    assert "network" in await start_failing(sandbox)
    assert docker.started() == [] and docker.probes() == []
    await sandbox.close()


async def test_remote_start_requires_the_operator_to_confirm_isolation(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": "ok"}),
    )) as http:
        sandbox = RemoteSandbox("https://example.org", "fake", http=http)
        with pytest.raises(SandboxError, match="remote_isolation_confirmed"):
            await sandbox.ensure_running()
        assert sandbox.status() == "error"
        confirmed = RemoteSandbox("https://example.org", "fake", http=http, isolation_confirmed=True)
        assert await confirmed.ensure_running() is confirmed.client


def test_configured_remote_sandbox_follows_the_config_flag(tmp_path):
    from dom.desktop.sandbox import configured_sandbox

    def sandbox(**settings):
        return configured_sandbox(Config(home=tmp_path, desktop_mode="remote", desktop=DesktopConfig(url="https://example.org", **settings)))

    assert sandbox()._isolation_confirmed is False
    assert sandbox(remote_isolation_confirmed=True)._isolation_confirmed is True


FAKE_NC = """#!/bin/sh
# usage: nc -z -w SECONDS HOST PORT
w=$3; host=$4
case $host in
  192.0.2.1) sleep "$w"; exit 1 ;;   # dropped
  192.0.2.2) exit 1 ;;               # refused at once
  *) exit 0 ;;                       # connects
esac
"""
FAKE_TIMEOUT = '#!/bin/sh\nshift\nexec "$@"\n'


def test_probe_script_passes_only_a_timeout_and_requires_the_control(tmp_path):
    import os
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    for name, body in (("nc", FAKE_NC), ("timeout", FAKE_TIMEOUT)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    live, dropped, refused, open_ = "198.51.100.1:443", "192.0.2.1:443", "192.0.2.2:443", "192.0.2.3:443"
    cases = {
        "all dropped": (live, [dropped, "192.0.2.1:22"], 0),
        "one refused": (live, [dropped, refused], 7),
        "one open": (live, [open_, dropped], 7),
        "control refused": (refused, [dropped], 8),
        "control dropped": (dropped, [dropped], 8),
    }

    def run(case):
        control, targets, _ = cases[case]
        return subprocess.run(["sh", "-c", PROBE, "probe", "2", control, *targets], env=env, capture_output=True).returncode

    with ThreadPoolExecutor(len(cases)) as pool:
        codes = dict(zip(cases, pool.map(run, cases)))
    assert codes == {name: expected for name, (_, _, expected) in cases.items()}


def stop_and_remove(docker):
    return docker.calls[-2:] == [("docker", "stop", "dom-desktop"), ("docker", "rm", "dom-desktop")]


async def test_close_stops_and_removes_the_container(tmp_path):
    docker = FakeDocker()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": None}),
    )) as http:
        sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http)
        await sandbox.ensure_running()
        await sandbox.close()
    assert stop_and_remove(docker)
    assert sandbox.status() == "stopped" and sandbox.client is None


async def test_close_is_quiet_when_nothing_was_started(tmp_path):
    docker = FakeDocker()
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    await sandbox.close()
    assert docker.calls == []


async def test_cancelled_start_removes_the_container(tmp_path):
    docker = FakeDocker()
    waiting = asyncio.Event()

    async def never(request):
        waiting.set()
        await asyncio.Future()

    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as http:
        sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http)
        start = asyncio.create_task(sandbox.ensure_running())
        await asyncio.wait_for(waiting.wait(), 2)
        assert len(docker.started()) == 1
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert stop_and_remove(docker)
        assert sandbox.status() == "stopped"
        await sandbox.close()


async def test_recover_stops_a_stray_container(make_app, tmp_path):
    app = make_app()
    docker = FakeDocker()
    docker.running = True
    app.sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    worker = DesktopWorker(app, app.sandbox)
    await worker.recover()
    assert stop_and_remove(docker)
    assert app.sandbox.status() == "stopped"
    await app.sandbox.close()


TABS = json.loads((Path(__file__).parent / "fixtures" / "sandbox_browser_tabs.json").read_text())


@pytest.mark.parametrize(("payload", "url"), [
    (TABS, "https://login.example.org/session?token=SECRET#fragment"),
    ({"success": True, "data": []}, None),
    ({"success": True, "data": [{"type": "page"}, "text", {"type": "page", "url": 3}]}, None),
    ({"success": True, "data": {"url": "https://example.org"}}, None),
    ({"success": True, "data": [{"index": 0, "url": "https://first.example.org/"}]}, "https://first.example.org/"),
])
async def test_current_url_prefers_the_active_tab(payload, url):
    def reply(request):
        assert request.method == "GET" and request.url.path == "/v1/browser/tabs"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        assert await SandboxClient("https://example.org", None, http).current_url() == url


@pytest.mark.parametrize(("url", "host"), [
    ("https://Login.Example.org:8443/a?b=c#d", "login.example.org"),
    ("http://192.0.2.1/login", "192.0.2.1"),
    ("https://user:pass@example.org/", "example.org"),
    ("https://[2001:db8::1]/", "2001:db8::1"),
    ("about:blank", None), ("javascript:alert(1)", None), ("chrome://settings", None), ("", None), (None, None),
    ("https://exa mple.org/", None),
])
def test_url_host_is_only_a_plain_http_host(url, host):
    assert url_host(url) == host


async def test_client_caps_json_and_screenshot_sizes_with_streamed_reads():
    chunks = []

    class Body(httpx.AsyncByteStream):
        def __init__(self, size):
            self.size = size

        async def __aiter__(self):
            for _ in range(self.size // 65536):
                chunks.append(1)
                yield b"x" * 65536

    def reply(request):
        if request.url.path == "/v1/browser/screenshot":
            return httpx.Response(200, headers={"content-type": "image/png"}, stream=Body(100 * 65536))
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Body(100 * 65536))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        client = SandboxClient("https://example.org", None, http)
        with pytest.raises(SandboxError, match="exceeds"):
            await client.markdown()
        assert len(chunks) <= 33  # stopped near 2 MB, not after reading the whole body
        chunks.clear()
        with pytest.raises(SandboxError, match="exceeds"):
            await client.screenshot()
        assert len(chunks) <= 77


async def test_download_json_failure_body_is_an_error_but_other_json_is_data():
    bodies = iter([
        {"success": False, "message": "no such file"}, {"success": False}, {"rows": [1]},
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(bodies)))) as http:
        client = SandboxClient("https://example.org", None, http)
        with pytest.raises(SandboxError, match="no such file"):
            await client.download_file("/tmp/missing")
        with pytest.raises(SandboxError):
            await client.download_file("/tmp/missing")
        assert json.loads(await client.download_file("/tmp/data.json")) == {"rows": [1]}

import asyncio
import hashlib
import json
import re
from ipaddress import IPv4Network
from pathlib import Path

import httpx
import pytest

from dom.capabilities import describe
from dom.config import Config, DesktopConfig, load_config
from dom.desktop.client import SandboxClient, SandboxError, url_host
from dom.desktop.network import PROBE_SCRIPT as PROBE
from dom.desktop.sandbox import LocalSandbox, RemoteSandbox
from dom.desktop.tools import RESULT_LIMIT, invoke, untrusted
from dom.desktop.worker import DesktopWorker
from tghelpers import OWNER


SECCOMP_PROFILE = Path(__file__).resolve().parents[1] / "dom/desktop/seccomp-chromium.json"

PROBES = ["192.0.2.1:443", "192.0.2.2:22"]
BRIDGE = IPv4Network("172.30.0.0/24")
GATEWAY = str(next(BRIDGE.hosts()))
NETWORK = [{"Driver": "bridge", "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": str(BRIDGE), "Gateway": GATEWAY}]}}]


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
        self.failing = set()  # docker verbs that exit 1
        self.run_gate = None  # an Event a plain "docker run" waits for
        self.run_entered = asyncio.Event()
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
            if command == "run" and self.run_gate is not None:
                self.run_entered.set()
                await self.run_gate.wait()
            code, output = (1 if command in self.failing else 0), b""
            if not code:
                self.running = command == "run"
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
        observed.append((request.url.path, sandbox.status()))
        assert request.headers["authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"success": True, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        config = local_config(tmp_path)
        sandbox = LocalSandbox(config, run=docker, http=http, clock=lambda: now[0])
        assert sandbox.status() == "stopped"
        assert await sandbox.ensure_running() is sandbox.client
        assert sandbox.status() == "ready"
        assert observed == [("/v1/sandbox", "starting"), ("/v1/browser/tabs", "starting")]
        assert docker.calls == [
            ("docker", "image", "inspect", config.desktop.image),
            ("docker", "network", "inspect", "dom-desktop"),
            ("docker", "image", "inspect", "alpine:3.22"),
            probe_call(config, ["192.0.2.1:443", "192.0.2.2:22", f"{GATEWAY}:8080", f"{GATEWAY}:22"]),
            ("docker", "container", "inspect", "--format", "{{.State.Running}}", "dom-desktop"),
            ("docker", "run", "-d", "--name", "dom-desktop", "--pull=never",
             "--network", "dom-desktop", "-p", "127.0.0.1:18080:8080",
             "--shm-size", "1g", "--memory", "1600m", "--memory-swap", "1600m", "--pids-limit", "512",
             "--security-opt", "no-new-privileges", "--security-opt", f"seccomp={SECCOMP_PROFILE}", "--cpus", "3",
             "-e", "SANDBOX_API_KEY", "-e", "DISABLE_CODE_SERVER=true",
             "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true", "-e", "DISABLE_JUPYTER=true",
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


def test_chromium_seccomp_changes_only_namespace_rules():
    profile = json.loads(SECCOMP_PROFILE.read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultErrnoRet"] == 1

    syscalls = profile["syscalls"]
    clone_rules = [rule for rule in syscalls if rule["names"] == ["clone"]]
    assert len(clone_rules) == 2
    moby_mask = 0x7e020000
    chromium_flags = 0x10000000 | 0x20000000 | 0x40000000  # CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET
    for index, rule in enumerate(clone_rules):
        assert rule["action"] == "SCMP_ACT_ALLOW"
        assert rule["args"] == [{
            "index": index, "value": moby_mask & ~chromium_flags, "op": "SCMP_CMP_MASKED_EQ",
        }]
        rule["args"][0]["value"] = moby_mask

    namespace_rule, = [rule for rule in syscalls if "clone3" in rule["names"] and "bpf" not in rule["names"]]
    assert namespace_rule == {
        "names": ["clone3", "setns", "unshare"], "action": "SCMP_ACT_ALLOW",
        "excludes": {"caps": ["CAP_SYS_ADMIN"]},
    }
    namespace_rule.update(names=["clone3"], action="SCMP_ACT_ERRNO", errnoRet=38)

    # Reconstruct the pinned Moby profile and check every other rule offline.
    upstream = json.dumps(profile, sort_keys=True).encode()
    assert hashlib.sha256(upstream).hexdigest() == "c1bd9400804f56f2b1f5dabbc34a1c8cfec2c6ebb91f02f38ba3f26af39d9011"


def probe_call(config, targets):
    settings = config.desktop
    return ("docker", "run", "--rm", "--pull=never", "--network", "dom-desktop", settings.probe_image,
            "sh", "-c", PROBE, "probe", "3", settings.probe_control, *targets)



async def test_local_start_retries_the_api_then_the_browser(tmp_path):
    docker = FakeDocker()
    steps = [
        ("/v1/sandbox", 503, {}),
        ("/v1/sandbox", 200, {"success": True, "data": None}),
        ("/v1/browser/tabs", 200, {"success": False, "message": "CDP unavailable"}),
        ("/v1/browser/tabs", 200, {"success": True, "data": []}),
    ]
    remaining = iter(steps)

    def reply(request):
        path, status, body = next(remaining)
        assert request.method == "GET" and request.url.path == path
        assert sandbox.status() == "starting" and sandbox.client is None
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http)
        assert await sandbox.ensure_running() is sandbox.client
        assert sandbox.status() == "ready"
        assert not list(remaining)
        await sandbox.close()


@pytest.mark.parametrize(("paths", "message"), [
    (["/v1/sandbox"], "API not healthy"),
    (["/v1/sandbox", "/v1/browser/tabs"], "browser did not start"),
])
async def test_local_start_timeout_names_the_stage_and_removes_the_container(tmp_path, paths, message):
    docker = FakeDocker()
    observed = []

    async def reply(request):
        observed.append(request.url.path)
        if request.url.path == paths[-1]:
            await asyncio.Future()
        return httpx.Response(200, json={"success": True, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        sandbox = LocalSandbox(local_config(tmp_path, start_timeout_seconds=0.05), run=docker, http=http)
        try:
            with pytest.raises(SandboxError, match=message):
                await asyncio.wait_for(sandbox.ensure_running(), timeout=2)
            assert observed == paths
            assert sandbox.status() == "error" and sandbox.client is None
            assert sandbox.error == message
            assert stop_and_remove(docker)
        finally:
            await sandbox.close()


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
    assert config.desktop.start_timeout_seconds == 180


def test_desktop_start_timeout_loads_from_toml_and_must_be_positive(tmp_path):
    (tmp_path / "config.toml").write_text('[desktop]\nstart_timeout_seconds = 90\n')
    assert load_config(tmp_path).desktop.start_timeout_seconds == 90
    with pytest.raises(ValueError, match="start_timeout_seconds"):
        DesktopConfig(start_timeout_seconds=0)


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



@pytest.mark.parametrize("exit_code", [0, 7, 124])
async def test_run_python_writes_a_unique_script_and_runs_it_with_a_timeout(exit_code):
    code = '''print("A quote and a dollar: '$VALUE'")\n'''
    result = {"stdout": "hello", "stderr": "details", "exit_code": exit_code}
    paths = []
    requests = []

    def reply(request):
        assert request.method == "POST"
        requests.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path == "/v1/file/write":
            path = body["file"]
            assert re.fullmatch(r"/tmp/[0-9a-f]{32}\.py", path)
            assert body == {"file": path, "content": code}
            paths.append(path)
            return httpx.Response(200, json={"success": True, "data": None})
        assert request.url.path == "/v1/bash/exec"
        path = paths[-1]
        assert body == {"command": f"trap 'rm -f -- {path}' EXIT; timeout --kill-after=5s 110s python3 {path}"}
        assert request.extensions["timeout"]["read"] == 120
        return httpx.Response(200, json={"success": True, "data": {**result, "status": "completed"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        client = SandboxClient("https://example.org", None, http)
        for _ in range(2):
            assert await invoke(client, "run_python", {"code": code}) == result
    assert requests == ["/v1/file/write", "/v1/bash/exec"] * 2
    assert len(set(paths)) == 2


async def test_run_python_uses_the_existing_tool_result_limit():
    def reply(request):
        if request.url.path == "/v1/file/write":
            return httpx.Response(200, json={"success": True, "data": None})
        assert request.url.path == "/v1/bash/exec"
        return httpx.Response(200, json={
            "success": True, "data": {"stdout": "x" * RESULT_LIMIT * 2, "stderr": "", "exit_code": 0},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        result = await invoke(SandboxClient("https://example.org", None, http), "run_python", {"code": "print(1)"})
    rendered = untrusted(result)
    assert len(rendered) == RESULT_LIMIT
    assert '"exit_code": 0' in rendered
    assert rendered.endswith("</untrusted_tool_result>")

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


@pytest.mark.parametrize("control", ["example.org:443", "1.1.1.1", f"{next(IPv4Network('10.0.0.0/8').hosts())}:443", "1.1.1.1:0"])
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
    assert docker.probes() == [probe_call(config, ["192.0.2.9:80", f"{GATEWAY}:8080", f"{GATEWAY}:22"])]
    await sandbox.close()


@pytest.mark.parametrize("network", [
    None,
    [{**NETWORK[0], "EnableIPv6": True}],
    [{**NETWORK[0], "IPAM": {"Config": [{"Subnet": "198.51.100.0/24", "Gateway": "198.51.100.1"}]}}],
    [{**NETWORK[0], "IPAM": {"Config": [{"Subnet": str(BRIDGE), "Gateway": GATEWAY}, {"Subnet": "2001:db8::/64"}]}}],
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


async def test_cancel_during_docker_run_waits_for_the_cli_before_cleanup(tmp_path):
    docker = FakeDocker()
    docker.run_gate = asyncio.Event()
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    start = asyncio.create_task(sandbox.ensure_running())
    await asyncio.wait_for(docker.run_entered.wait(), 2)
    start.cancel()
    await asyncio.sleep(0.02)
    assert not start.done()  # waiting for the CLI that may still create the container
    assert [call[1] for call in docker.calls][-1] == "run"
    docker.run_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert stop_and_remove(docker)
    assert sandbox.status() == "stopped"
    await sandbox.close()


@pytest.mark.parametrize("verb", ["stop", "rm"])
async def test_discard_checks_exit_codes_and_logs_failures(tmp_path, caplog, verb):
    docker = FakeDocker()
    docker.running = True
    docker.failing = {verb}
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    sandbox._status = "ready"
    await sandbox.close()
    assert f"could not {verb} the desktop container" in caplog.text
    assert [call[1] for call in docker.calls if call[1] in ("stop", "rm")] == ["stop", "rm"]  # a failed stop still tries rm


async def test_discard_is_quiet_when_there_is_no_container(tmp_path, caplog):
    docker = FakeDocker()
    sandbox = LocalSandbox(local_config(tmp_path), run=docker)
    sandbox._status = "error"
    await sandbox.close()
    assert "could not" not in caplog.text
    assert [call[1] for call in docker.calls] == ["container"]


async def test_cancelling_a_task_stops_and_removes_the_running_container(make_app, tmp_path):
    from test_desktop_worker import Model, task_row, wait_status

    app = make_app()
    docker = FakeDocker()
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Future()

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": None}),
    )) as http:
        sandbox = app.sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http)
        app.client = Model(blocked, {"content": "invalid"})
        app.desktop_worker = worker = DesktopWorker(app, sandbox)
        app.config = app.config.model_copy(update={"desktop_mode": "local"})
        task_id = await worker.enqueue(OWNER, "Example")
        await asyncio.wait_for(entered.wait(), 2)
        assert sandbox.status() == "ready" and docker.running
        assert await worker.cancel(OWNER, task_id)
        assert stop_and_remove(docker)
        assert sandbox.status() == "stopped" and sandbox.client is None
        assert (await task_row(app, task_id))["status"] == "cancelled"
        await sandbox.close()


async def test_cancel_releases_the_worker_lock_while_it_waits_for_the_sandbox(make_app):
    from test_desktop_worker import Model, Sandbox, task_row, wait_task

    app = make_app()
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Sandbox):
        stops = 0

        async def stop(self):
            entered.set()
            await release.wait()
            Slow.stops += 1

    async def blocked():
        await asyncio.Future()

    sandbox = app.sandbox = Slow()
    app.client = Model(blocked, {"content": "invalid"}, {"content": "invalid"})
    app.desktop_worker = worker = DesktopWorker(app, sandbox)
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    task_id = await worker.enqueue(OWNER, "Example")
    await asyncio.sleep(0.01)
    cancelling = asyncio.create_task(worker.cancel(OWNER, task_id))
    await asyncio.wait_for(entered.wait(), 2)
    assert not worker.control.locked()  # the lock is not held while the sandbox lock is awaited
    with pytest.raises(ValueError, match="shutting down"):  # but the stop is reserved: no new task can claim the container
        await worker.enqueue(OWNER, "Another")
    release.set()
    assert await cancelling
    assert Slow.stops == 1
    await worker.enqueue(OWNER, "Another")  # reservation released
    await wait_task(worker)


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
    # No tab is marked active: the sandbox does not say where the browser is, so neither do we.
    ({"success": True, "data": [{"index": 0, "url": "https://first.example.org/"}]}, None),
    ({"success": True, "data": [{"url": "https://a.example.org/", "is_active": False}, {"url": "https://b.example.org/", "is_active": "yes"}]}, None),
])
async def test_current_url_is_the_active_tab_only(payload, url):
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


async def test_requests_ask_for_identity_encoding():
    seen = []

    def reply(request):
        seen.append(request.headers["accept-encoding"])
        return httpx.Response(200, headers={"content-encoding": "identity"}, json={"success": True, "data": {"ok": True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        client = SandboxClient("https://example.org", "key", http)
        assert await client.health() == {"ok": True}
        await client.download_file("/tmp/x")
        await client.screenshot()
    assert seen == ["identity"] * 3


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "GZIP", "identity, gzip", "unknown"])
async def test_compressed_responses_are_rejected_before_the_body_is_read(encoding):
    read = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(1)
            yield b"x" * 10

    def reply(request):
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=Body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        client = SandboxClient("https://example.org", None, http)
        for call in (client.health, client.screenshot, lambda: client.download_file("/tmp/x")):
            with pytest.raises(SandboxError, match="compressed"):
                await call()
    assert read == []


async def test_size_caps_apply_to_the_raw_bytes_on_the_wire():
    import gzip

    bomb = gzip.compress(b"0" * 3_000_000)  # a few KB on the wire, 3 MB once decoded
    assert len(bomb) < 10_000

    def reply(request):
        # A hostile sandbox ignores identity and compresses anyway. httpx would decode it past the cap.
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"}, content=bomb)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        with pytest.raises(SandboxError, match="compressed"):
            await SandboxClient("https://example.org", None, http).markdown()

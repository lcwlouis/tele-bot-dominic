import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from dom.plugins import runner
from dom.plugins.manifest import Manifest, read_plugin
from dom.plugins.runner import PluginError, PluginRunner, PluginsUnavailable
from test_plugins import MANIFEST, write_plugin


BOOTSTRAP = 'import runpy, sys; sys.path.append("/plugin"); runpy.run_path("/plugin/main.py", run_name="__main__")'


@pytest.mark.parametrize("network", [False, True])
def test_exact_arguments(monkeypatch, network):
    prefix = Path("/home/example/.local/share/uv/python/cpython-3.12")
    executable = prefix / "bin/python3.12"
    venv = Path("/home/example/dom/releases/example/.venv/bin/python")
    monkeypatch.setattr(Path, "resolve", lambda path: executable if path == venv else path)
    monkeypatch.setattr(sys, "executable", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    monkeypatch.setattr(Path, "exists", lambda path: True)
    manifest = Manifest.parse(MANIFEST.replace('version = "1"', f'version = "1"\nnetwork = {str(network).lower()}'))
    command = PluginRunner().arguments(Path("/sample/convert"), manifest, filter_fd=3, network=network)
    expected = ["/usr/bin/bwrap", "--unshare-all"]
    if network:
        expected += ["--share-net"]
    expected += [
        "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc/alternatives", "/etc/alternatives",
    ]
    if network:
        expected += ["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs", "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"]
    expected += [
        "--ro-bind", str(prefix), str(prefix), "--ro-bind", "/sample/convert", "/plugin",
        "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev", "--chdir", "/plugin",
        "--disable-userns", "--seccomp", "3", "--",
        "/usr/bin/prlimit", "--as=67108864", "--cpu=10", "--", str(executable),
        "-I", "-S", "-c", BOOTSTRAP,
    ]
    assert command == expected


@pytest.mark.parametrize("payload", [
    b'{"ok":true,"result":null}\n', b'{"ok":0,"error":"failed"}\n', b'{"ok":true,"result":"ok"}',
    b'{"ok":true,"result":"ok"}\n\n', b'junk\n', b'[]\n', b'\xff\n',
])
def test_protocol_invalid(payload):
    with pytest.raises(PluginError, match="protocol"):
        runner.parse_output(payload)


def test_protocol_success_and_plugin_error():
    assert runner.parse_output(b'{"ok":true,"result":"done"}\n') == "done"
    with pytest.raises(PluginError, match="problem"):
        runner.parse_output(b'{"ok":false,"error":"problem"}\n')


def test_output_cap_during_read():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
    )
    with pytest.raises(PluginError, match="16 KB"):
        runner.exchange(process, b"{}\n", 5)
    assert process.returncode is not None


def test_timeout_kills_process_group(monkeypatch):
    class FakeProcess:
        pid = 100
        stdin = stdout = None
        returncode = None

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("plugin", timeout)
            self.returncode = -signal.SIGKILL

        def poll(self):
            return self.returncode

    kills = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    with pytest.raises(PluginError, match="timed out"):
        runner.exchange(FakeProcess(), b"{}", 1)
    assert kills == [(100, signal.SIGKILL)]


@pytest.mark.parametrize(("platform", "exists"), [("darwin", True), ("linux", False)])
def test_unavailable(monkeypatch, tmp_path, platform, exists):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "is_file", lambda path: exists)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("started a process"))
    instance = PluginRunner(home=tmp_path)
    assert instance.self_test() is False
    with pytest.raises(PluginsUnavailable):
        instance.run(tmp_path / "convert", "length", {}, Manifest.parse(MANIFEST))


def test_failed_self_test_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    checks = []

    def failed(self):
        checks.append(1)
        raise PluginError("sandbox failed")

    monkeypatch.setattr(PluginRunner, "_probe", failed)
    for _ in range(2):
        assert not PluginRunner(home=tmp_path).self_test()
    assert checks == [1]


def test_run_only_passes_requested_arguments_and_revalidates_pin(monkeypatch, tmp_path):
    folder = write_plugin(tmp_path)
    plugin = read_plugin(folder)
    calls = []
    monkeypatch.setattr(PluginRunner, "self_test", lambda self: True)

    def execute(self, snapshot, request, net):
        calls.append((snapshot.hash, json.loads(request), net))
        return "converted"

    monkeypatch.setattr(PluginRunner, "_execute_locked", execute)
    instance = PluginRunner(home=tmp_path)
    assert instance.run(folder, "length", {"amount": 2}, plugin.manifest, approved_hash=plugin.hash) == "converted"
    assert calls == [(plugin.hash, {"tool": "length", "args": {"amount": 2}}, False)]
    (folder / "main.py").write_text("print('changed')\n")
    with pytest.raises(PluginError, match="changed"):
        instance.run(folder, "length", {}, plugin.manifest, approved_hash=plugin.hash)


def test_network_requires_a_matching_approval(monkeypatch, tmp_path):
    folder = write_plugin(tmp_path, MANIFEST.replace('version = "1"', 'version = "1"\nnetwork = true'))
    plugin = read_plugin(folder)
    monkeypatch.setattr(PluginRunner, "self_test", lambda self: True)
    monkeypatch.setattr(PluginRunner, "_execute_locked", lambda self, snapshot, request, net: str(net))
    instance = PluginRunner(home=tmp_path)
    with pytest.raises(PluginError, match="requires approval"):
        instance.run(folder, "length", {}, plugin.manifest)
    assert instance.run(folder, "length", {}, plugin.manifest, approved_hash=plugin.hash) == "True"


@pytest.mark.skipif(
    sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file(),
    reason="requires Linux and /usr/bin/bwrap",
)
def test_real_sandbox_refuses_home_reads_and_sockets(tmp_path):
    home = tmp_path / "home"
    folder = write_plugin(home)
    secret = home / "private" / "secret"
    secret.parent.mkdir()
    secret.write_text("private")
    (folder / "main.py").write_text("""
import os
import socket
from _dom import run, tool

@tool
def length(path):
    assert os.environ.get("DOM_HOME") is None
    try:
        with open(path):
            return "readable"
    except OSError:
        pass
    try:
        with socket.socket():
            return "socket opened"
    except OSError:
        return "isolated"

run()
""")
    plugin = read_plugin(folder)
    instance = PluginRunner(home=home)
    assert instance.self_test()
    assert instance.run(folder, "length", {"path": str(secret)}, plugin.manifest, approved_hash=plugin.hash) == "isolated"

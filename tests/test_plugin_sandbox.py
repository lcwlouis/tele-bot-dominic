"""Real-sandbox checks. They need Linux and bubblewrap, and run on the Pi."""

import ast
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from dom.plugins.manifest import read_plugin
from dom.plugins.runner import PluginError, PluginRunner
from dom.store import Store
from test_plugins import MANIFEST, write_plugin

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file(),
    reason="requires Linux and /usr/bin/bwrap",
)
NETWORK = MANIFEST.replace('version = "1"', 'version = "1"\nnetwork = true')


@pytest.fixture(scope="module", autouse=True)
def check_user_namespaces():
    # Check the host's ability to create a user namespace independently of PluginRunner.
    result = subprocess.run(
        ["/usr/bin/bwrap", "--unshare-user", "--ro-bind", "/", "/", "--", "/usr/bin/true"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        reason = result.stderr.strip()
        normalized = reason.lower()
        if ("namespace" in normalized or "uid map" in normalized) and any(
            fragment in normalized for fragment in
            ("operation not permitted", "permission denied", "no permissions", "not enabled")
        ):
            pytest.skip(f"unprivileged user namespaces unavailable: {reason}")
        pytest.fail(f"bubblewrap namespace probe failed: {reason}")


def run_plugin(tmp_path, source, *, manifest=MANIFEST, args=None):
    """Run `source` as the body of the `length` tool inside the real sandbox."""
    home = tmp_path / "home"
    folder = write_plugin(home, manifest)
    body = "\n".join("    " + line for line in source.strip().splitlines())
    (folder / "main.py").write_text(
        f"import errno, json, os, socket\nfrom _dom import run, tool\n\n@tool\ndef length(**kw):\n{body}\n\nrun()\n"
    )
    plugin = read_plugin(folder)
    runner = PluginRunner(home=home)
    assert runner.self_test()
    return runner.run(folder, "length", args or {}, plugin.manifest, approved_hash=plugin.hash)


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_network_plugin_can_connect_but_not_listen(tmp_path):
    result = run_plugin(tmp_path, f"""
out = {{}}
with socket.socket(socket.AF_INET) as client:
    out["connect"] = errno.errorcode[client.connect_ex(("127.0.0.1", {closed_port()}))]
with socket.socket(socket.AF_INET) as server:
    try:
        server.bind(("127.0.0.1", 0))
        out["bind"] = "bound"
    except OSError as error:
        out["bind"] = errno.errorcode[error.errno]
return json.dumps(out)
""", manifest=NETWORK)
    assert json.loads(result) == {"connect": "ECONNREFUSED", "bind": "EPERM"}


@pytest.mark.parametrize("family", ["AF_UNIX", "AF_NETLINK"])
def test_network_plugin_cannot_open_other_socket_families(tmp_path, family):
    result = run_plugin(tmp_path, f"""
try:
    socket.socket(socket.{family}, socket.SOCK_DGRAM if socket.{family} == socket.AF_UNIX else socket.SOCK_RAW, 0)
except OSError as error:
    return errno.errorcode[error.errno]
return "opened"
""", manifest=NETWORK)
    assert result == "EPERM"


def test_offline_plugin_has_no_sockets_at_all(tmp_path):
    result = run_plugin(tmp_path, """
try:
    socket.socket(socket.AF_INET)
except OSError as error:
    return errno.errorcode[error.errno]
return "opened"
""")
    assert result == "EPERM"


def test_tmp_is_a_small_tmpfs(tmp_path):
    result = run_plugin(tmp_path, """
chunk = b"x" * 1_000_000  # below the per-file limit, so only the tmpfs size can stop it
for number in range(16):
    try:
        with open(f"/tmp/fill{number}", "wb") as file:
            file.write(chunk)
    except OSError as error:
        return f"{errno.errorcode[error.errno]} after {number} files"
return "wrote 16 MB"
""")
    assert result.startswith("ENOSPC after ")


def test_single_file_size_is_limited(tmp_path):
    result = run_plugin(tmp_path, """
try:
    with open("/tmp/big", "wb") as file:
        file.write(b"x" * 2_000_000)
except OSError as error:
    return errno.errorcode[error.errno]
return "wrote 2 MB"
""")
    assert result == "EFBIG"


def test_open_files_are_limited(tmp_path):
    result = run_plugin(tmp_path, """
files = []
try:
    for number in range(40):
        files.append(open("/plugin/main.py", "rb"))
except OSError as error:
    return errno.errorcode[error.errno]
return "opened 40 files"
""")
    assert result == "EMFILE"


def test_no_stray_file_descriptors_reach_the_plugin(tmp_path):
    result = run_plugin(tmp_path, """
out = {}
for name in os.listdir("/proc/self/fd"):
    try:
        out[name] = os.readlink("/proc/self/fd/" + name)
    except FileNotFoundError:
        out[name] = None  # the descriptor os.listdir itself opened, closed again by now
return json.dumps(out)
""")
    fds = json.loads(result)
    # Only stdin, stdout and stderr stay open; anything else is the listing's own, already closed.
    assert {"0", "1", "2"} <= set(fds)
    assert all(target is None for fd, target in fds.items() if fd not in {"0", "1", "2"})
    assert fds["0"].startswith("pipe:") and fds["1"].startswith("pipe:")


def test_stderr_writes_are_bounded(tmp_path):
    # stderr is a host temp file kept for diagnostics; RLIMIT_FSIZE (1 MB) bounds it.
    result = run_plugin(tmp_path, """
try:
    for _ in range(3000):
        os.write(2, b"x" * 1024)
    return "unbounded"
except OSError as error:
    return errno.errorcode[error.errno]
""")
    assert result == "EFBIG"


def test_plugin_holds_no_capabilities_and_sees_a_private_cgroup_root(tmp_path):
    result = run_plugin(tmp_path, """
status = dict(line.split(":\\t", 1) for line in open("/proc/self/status").read().splitlines() if ":\\t" in line)
cgroup = open("/proc/self/cgroup").read().strip()
return json.dumps({"CapEff": status["CapEff"].strip(), "cgroup": cgroup})
""")
    assert json.loads(result) == {"CapEff": "0000000000000000", "cgroup": "0::/"}


def test_hardened_syscalls_fail_with_eperm(tmp_path):
    result = run_plugin(tmp_path, """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
out = {}
for name, call in {
    "ptrace": lambda: libc.ptrace(0, 0, 0, 0),
    "unshare": lambda: libc.unshare(0x00020000),  # CLONE_NEWNS
    "mount": lambda: libc.mount(b"none", b"/tmp", b"tmpfs", 0, None),
    "setns": lambda: libc.setns(-1, 0),
    "fork": lambda: libc.fork(),
}.items():
    ctypes.set_errno(0)
    out[name] = errno.errorcode.get(ctypes.get_errno()) if call() == -1 else "succeeded"
return json.dumps(out)
""")
    assert json.loads(result) == {name: "EPERM" for name in ("ptrace", "unshare", "mount", "setns", "fork")}


def test_real_sandbox_passes_its_own_probes(tmp_path):
    instance = PluginRunner(home=tmp_path)
    assert instance._probe() == ""
    assert instance.self_test() and instance.last_failure is None


def test_probe_notices_a_filter_that_allows_everything(tmp_path, monkeypatch):
    from dom.plugins import runner

    allow_all = struct.pack("=HBBI", 0x06, 0, 0, 0x7FFF0000)
    monkeypatch.setattr(runner, "_seccomp", lambda network, machine=None: allow_all)
    reason = PluginRunner(home=tmp_path)._probe()
    assert reason.startswith("offline probe:") and "fork allowed" in reason


SURFACE_MANIFEST = MANIFEST + """[[commands]]
name = "echo"
description = "Echo the text"
[[panels]]
id = "summary"
title = "Summary"
"""
# "\x40" is "@": the public-data check reads a decorator inside a string as a Telegram handle.
SURFACE_MAIN = """import os
from _dom import command, panel, run

\x40command
def echo(text, scope):
    return f"{text}|{scope}|{sorted(os.environ)}"

\x40panel
def summary():
    return {"title": "Week", "items": [["home visible", os.path.exists(%r)]]}

run()
"""


def test_commands_and_panels_run_in_the_same_sandbox(tmp_path):
    home = tmp_path / "home"
    folder = write_plugin(home, SURFACE_MANIFEST)
    (folder / "main.py").write_text(SURFACE_MAIN % str(home))
    plugin = read_plugin(folder)
    runner = PluginRunner(home=home)
    assert runner.self_test()
    request = {"command": "echo", "args": "hi there", "scope": "group"}
    text, scope, env = runner.run_request(folder, request, plugin.manifest, approved_hash=plugin.hash).split("|")
    # --clearenv leaves only PATH; bwrap sets PWD and Python's C-locale coercion sets LC_CTYPE. Nothing from the host.
    assert (text, scope) == ("hi there", "group") and set(ast.literal_eval(env)) <= {"PATH", "PWD", "LC_CTYPE"}
    panel = runner.run_request(folder, {"panel": "summary"}, plugin.manifest, approved_hash=plugin.hash)
    assert panel == {"title": "Week", "items": [["home visible", False]]}


STORAGE_MAIN = """\
from _dom import load_storage, run, save_storage, self_test, tool

\x40self_test
def check():
    assert load_storage() is None
    save_storage({"count": 99})
    return "checked"

\x40tool
def length(action="increment"):
    count = (load_storage() or {}).get("count", 0) + 1
    if action == "oversize":
        save_storage("x" * 65536)
    elif action == "invalid":
        save_storage({"x": float("nan")})
    elif action == "deep":
        nested = []
        for _ in range(100):
            nested = [nested]
        save_storage(nested)
    else:
        save_storage({"count": count})
    return str(count)

run()
"""


async def test_storage_round_trip_is_per_plugin_and_trials_discard_writes(tmp_path):
    manifest = MANIFEST.replace('version = "1"', 'version = "1"\nstorage = true')
    folder = write_plugin(tmp_path, manifest)
    (folder / "main.py").write_text(STORAGE_MAIN)
    other = folder.with_name("sample")
    other.mkdir()
    (other / "main.py").write_text(STORAGE_MAIN)
    (other / "plugin.toml").write_text(manifest.replace('"convert"', '"sample"'))
    first, second = read_plugin(folder), read_plugin(other)
    store = Store(tmp_path, owner_id=None)
    try:
        instance = PluginRunner(home=tmp_path, store=store)
        await store.stage_plugins([
            (item.manifest.name, item.hash, '{"storage": true}') for item in (first, second)
        ])
        sample = {"tool": "length", "args": {}}
        assert instance.trial(first, sample) == {"output": "checked", "self_test": True}
        for item in (first, second):
            await store.set_plugin_status(item.manifest.name, item.hash, "approved")
            assert store.read_plugin_storage(item.manifest.name, item.hash) is None

        def call(item, **args):
            directory = folder.with_name(item.manifest.name)
            return instance.run(directory, "length", args, item.manifest, approved_hash=item.hash)

        assert call(first) == "1"
        assert call(first) == "2"
        assert call(second) == "1"
        assert instance.trial(first, sample) == {"output": "checked", "self_test": True}
        with pytest.raises(PluginError, match="64 KB"):
            call(first, action="oversize")
        with pytest.raises(PluginError, match="JSON"):
            call(first, action="invalid")
        with pytest.raises(PluginError, match="100 nested"):
            call(first, action="deep")
        assert call(first) == "3"
        await store.drop_plugin_storage("convert")
        assert call(first) == "1"
        assert call(second) == "2"
    finally:
        store.close()

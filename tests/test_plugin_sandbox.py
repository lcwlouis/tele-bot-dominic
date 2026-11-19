"""Real-sandbox checks. They need Linux and bubblewrap, and run on the Pi."""

import json
import socket
import sys
from pathlib import Path

import pytest

from dom.plugins.manifest import read_plugin
from dom.plugins.runner import PluginError, PluginRunner
from test_plugins import MANIFEST, write_plugin

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/usr/bin/bwrap").is_file(),
    reason="requires Linux and /usr/bin/bwrap",
)
NETWORK = MANIFEST.replace('version = "1"', 'version = "1"\nnetwork = true')


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

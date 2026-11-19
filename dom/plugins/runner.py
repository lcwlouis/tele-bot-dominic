"""Run one bounded JSON-lines exchange in a tested Linux sandbox."""

from __future__ import annotations

import errno
import json
import logging
import os
import selectors
import signal
import struct
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryFile, TemporaryDirectory
from threading import Lock
from time import monotonic

from dom.plugins.manifest import Manifest, Plugin, read_plugin

log = logging.getLogger(__name__)

MAX_OUTPUT = 16 * 1024
_BOOTSTRAP = 'import runpy, sys; sys.path.append("/plugin"); runpy.run_path("/plugin/main.py", run_name="__main__")'
_PROCESS = Lock()
_PROBE_LOCK = Lock()
_PROBES: dict[tuple, bool] = {}


class PluginError(ValueError):
    """The plugin did not complete the requested operation."""


class PluginsUnavailable(PluginError):
    """No working sandbox is available on this host."""


def parse_output(data: bytes) -> str:
    if len(data) > MAX_OUTPUT:
        raise PluginError("Plugin output exceeded 16 KB.")
    if not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise PluginError("Invalid plugin protocol response.")
    try:
        result = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise PluginError("Invalid plugin protocol response.") from None
    if isinstance(result, dict) and result.get("ok") is True and set(result) == {"ok", "result"}:
        if isinstance(result["result"], str):
            return result["result"].encode("utf-8", "replace").decode("utf-8")
    if isinstance(result, dict) and result.get("ok") is False and set(result) == {"ok", "error"}:
        if isinstance(result["error"], str):
            raise PluginError(result["error"].encode("utf-8", "replace").decode("utf-8") or "Plugin reported an error.")
    raise PluginError("Invalid plugin protocol response.")


def exchange(process: subprocess.Popen, request: bytes, timeout: int) -> str:
    """Bound both pipe I/O and exit time, without buffering unlimited output."""
    output = bytearray()
    sent = 0
    deadline = monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event)
            while selector.get_map():
                time_left = deadline - monotonic()
                if time_left <= 0:
                    raise subprocess.TimeoutExpired("plugin", timeout)
                for key, event in selector.select(time_left):
                    if event == selectors.EVENT_WRITE:
                        try:
                            sent += os.write(key.fd, request[sent:sent + 4096])
                        except BrokenPipeError:
                            sent = len(request)
                        if sent == len(request):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, MAX_OUTPUT + 1 - len(output))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise PluginError("Plugin output exceeded 16 KB.")
        if process.wait(timeout=max(0, deadline - monotonic())) != 0:
            raise PluginError("Plugin process failed.")
        return parse_output(output)
    except subprocess.TimeoutExpired:
        raise PluginError("Plugin timed out.") from None
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _seccomp(network: bool, machine: str | None = None) -> bytes:
    """Classic BPF for Linux seccomp_data. Permit only internet sockets when network is approved."""
    # Per architecture: audit arch, socket syscall, always-denied syscalls (process creation, socketpair,
    # clone3, io_uring_setup) and the server-side socket calls (bind, listen, accept, accept4) denied online.
    profiles = {
        "aarch64": (0xC00000B7, 198, (220, 435, 199, 425), (200, 201, 202, 242)),
        "x86_64": (0xC000003E, 41, (56, 57, 58, 435, 53, 425), (49, 50, 43, 288)),
        "armv7l": (0x40000028, 281, (2, 120, 190, 435, 288, 102, 425), (282, 284, 285, 366)),
    }
    profile = profiles.get(machine or os.uname().machine)
    if profile is None:
        raise PluginsUnavailable("No plugin sandbox filter for this Linux architecture.")
    arch, socket_syscall, blocked, servers = profile
    load, equal, bit_set, ret = 0x20, 0x15, 0x45, 0x06
    allow, kill, deny = 0x7FFF0000, 0x80000000, 0x00050000 | errno.EPERM
    instructions = [
        (load, 0, 0, 4),  # arch
        (equal, 1, 0, arch),
        (ret, 0, 0, kill),
        (load, 0, 0, 0),  # syscall number
        (bit_set, 0, 1, 0x40000000),  # reject the x32 compatibility ABI
        (ret, 0, 0, deny),
    ]
    # io_uring_setup could bypass the socket restriction; a network plugin may connect but never serve.
    for syscall in (*blocked, *(servers if network else ())):
        instructions += [(equal, 0, 1, syscall), (ret, 0, 0, deny)]
    if network:
        # Socket domain is the low 32 bits of args[0]. Supported profiles above are little endian.
        instructions += [
            (equal, 0, 4, socket_syscall),
            (load, 0, 0, 16),
            (equal, 2, 0, 2),   # AF_INET
            (equal, 1, 0, 10),  # AF_INET6
            (ret, 0, 0, deny),
        ]
    else:
        instructions += [(equal, 0, 1, socket_syscall), (ret, 0, 0, deny)]
    instructions.append((ret, 0, 0, allow))
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


class PluginRunner:
    def __init__(self, bwrap: str = "/usr/bin/bwrap", *, home: Path | None = None):
        self.bwrap = Path(bwrap)
        self.home = home.resolve() if home is not None else None

    def arguments(self, folder: Path, manifest: Manifest, *, filter_fd: int, network: bool = False) -> list[str]:
        executable = Path(sys.executable).resolve()
        prefix = Path(sys.base_prefix).resolve()
        if not executable.is_relative_to(prefix):
            raise PluginsUnavailable("The real Python interpreter is outside its base prefix.")
        # Debian's loader can use /lib or /lib64, including on merged-/usr installations.
        # Alternatives may be symlink targets. Only public CA certs and DNS settings are
        # needed when online, so /etc/ssl/private and the rest of /etc stay outside.
        mounts = [Path("/usr"), Path("/lib")]
        mounts += [path for path in map(Path, ("/lib64", "/bin", "/etc/alternatives")) if path.exists()]
        network = network and manifest.network
        if network:
            mounts += [path for path in map(Path, ("/etc/ssl/certs", "/etc/resolv.conf")) if path.exists()]
        if not any(prefix.is_relative_to(path.resolve()) for path in mounts):
            mounts.append(prefix)
        for mount in mounts:
            real = mount.resolve()
            if real == Path("/") or (self.home is not None and (
                self.home.is_relative_to(real) or real.is_relative_to(self.home)
            )):
                raise PluginsUnavailable("A sandbox runtime path overlaps Dom's home.")
        # bwrap 0.12 needs an explicit user namespace for --disable-userns (--unshare-all only tries one).
        cmd = [str(self.bwrap), "--unshare-all", "--unshare-user"]
        if network:
            cmd.append("--share-net")
        cmd += ["--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin"]
        for mount in mounts:
            cmd += ["--ro-bind", str(mount), str(mount)]
        return cmd + [
            "--ro-bind", str(folder), "/plugin", "--tmpfs", "/tmp", "--proc", "/proc",
            "--dev", "/dev", "--chdir", "/plugin", "--disable-userns", "--seccomp", str(filter_fd), "--",
            "/usr/bin/prlimit", f"--as={manifest.memory_mb * 1024 * 1024}", f"--cpu={manifest.timeout_seconds}",
            "--", str(executable), "-I", "-S", "-c", _BOOTSTRAP,
        ]

    def self_test(self) -> bool:
        if sys.platform != "linux" or not self.bwrap.is_file():
            return False
        key = (self.bwrap, self.home, sys.executable, sys.base_prefix)
        with _PROBE_LOCK:
            if key not in _PROBES:
                try:
                    _PROBES[key] = self._probe()
                except (PluginError, OSError, ValueError) as error:
                    log.warning("Plugin sandbox self-test failed: %s", error)
                    _PROBES[key] = False
            return _PROBES[key]

    def _probe(self) -> bool:
        with TemporaryDirectory(prefix="dom-plugin-probe-") as directory:
            root = Path(directory)
            sentinel = root / "private"
            sentinel.write_text("test")
            folder = root / "probe"
            folder.mkdir()
            (folder / "plugin.toml").write_text(
                'name = "probe"\ndescription = "Isolation probe"\nversion = "1"\n'
                '[[tools]]\nname = "probe"\ndescription = "Check isolation"\nparameters = {type = "object"}\n'
            )
            (folder / "main.py").write_text(
                "import os, socket\n"
                "from _dom import tool, run\n"
                "@tool\n"
                "def probe(path, home):\n"
                "    if os.path.exists(path) or (home and os.path.exists(home)):\n"
                "        return 'host path visible'\n"
                "    try:\n"
                "        socket.socket().close()\n"
                "    except OSError:\n"
                "        return 'isolated'\n"
                "    return 'socket available'\n"
                "run()\n"
            )
            payload = json.dumps({"tool": "probe", "args": {"path": str(sentinel), "home": str(self.home or "")}})
            return self._execute(read_plugin(folder), (payload + "\n").encode(), False) == "isolated"

    def run(
        self, plugin_dir: Path, tool: str, args: dict, manifest: Manifest, *, approved_hash: str | None = None,
    ) -> str:
        if not self.self_test():
            raise PluginsUnavailable("Plugins are unavailable because the sandbox could not be verified.")
        with _PROCESS:
            try:
                snapshot = read_plugin(plugin_dir)
                if snapshot.manifest != manifest or (approved_hash is not None and snapshot.hash != approved_hash):
                    raise PluginError("Plugin changed since approval.")
                if manifest.network and approved_hash is None:
                    raise PluginError("Network access requires approval of this plugin version.")
                request = json.dumps({"tool": tool, "args": args}, allow_nan=False) + "\n"
                return self._execute_locked(snapshot, request.encode("utf-8"), manifest.network)
            except OSError:
                raise PluginError("Could not start or read the plugin.") from None

    def _execute(self, snapshot: Plugin, request: bytes, network: bool) -> str:
        with _PROCESS:
            return self._execute_locked(snapshot, request, network)

    def _execute_locked(self, snapshot: Plugin, request: bytes, network: bool) -> str:
        # Materialize only the bytes we hashed. Edits to the live plugin cannot change this bind.
        with TemporaryDirectory(prefix="dom-plugin-") as directory, TemporaryFile() as sandbox_filter, \
                TemporaryFile() as errors:
            folder = Path(directory).resolve()
            for relative, content in snapshot.files.items():
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            helper = folder / "_dom" / "__init__.py"
            helper.parent.mkdir()
            helper.write_bytes(files("dom.plugins").joinpath("dom_plugin.py").read_bytes())
            sandbox_filter.write(_seccomp(network))
            sandbox_filter.seek(0)
            cmd = self.arguments(folder, snapshot.manifest, filter_fd=sandbox_filter.fileno(), network=network)
            process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                env={}, start_new_session=True, pass_fds=(sandbox_filter.fileno(),),
            )
            try:
                return exchange(process, request, snapshot.manifest.timeout_seconds)
            except PluginError as error:
                errors.seek(0)
                detail = errors.read(500).decode("utf-8", "replace").strip()
                if detail:  # logged for the operator only; the model sees the plain error
                    log.warning("plugin %s failed: %s (%s)", snapshot.manifest.name, error, detail)
                raise

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
from typing import TYPE_CHECKING, Callable

from dom.plugins.manifest import Manifest, Plugin, read_plugin

if TYPE_CHECKING:
    from dom.store import Store

log = logging.getLogger(__name__)

MAX_OUTPUT = 16 * 1024
MAX_STORAGE = 64 * 1024
TMP_BYTES = 8 * 1024 * 1024  # size of the plugin's /tmp tmpfs
FILE_BYTES = 1024 * 1024  # RLIMIT_FSIZE: largest file a plugin can write
OPEN_FILES = 32  # RLIMIT_NOFILE; no clone, so no process limit is needed
_BOOTSTRAP = 'import runpy, sys; sys.path.append("/plugin"); runpy.run_path("/plugin/main.py", run_name="__main__")'
_PROCESS = Lock()
_PROBE_LOCK = Lock()
FAILURE_RETRY_SECONDS = 300  # a failed self-test is retried after this long; success is kept
_PROBES: dict[tuple, tuple[bool, str, float]] = {}  # ok, failure reason, when checked
_PROBE_MAIN = """\
import os, socket
from _dom import tool, run


def denied(action):
    try:
        action()
    except OSError:
        return True
    return False


@tool
def probe(path, home, network):
    problems = []
    if os.path.exists(path) or os.path.exists(home):
        problems.append("host path visible")
    with open("/proc/self/status") as file:
        status = dict(line.split(":", 1) for line in file.read().splitlines() if ":" in line)
    for key, wanted in (("Seccomp", "2"), ("NoNewPrivs", "1"), ("CapEff", "0000000000000000")):
        found = status.get(key, "").strip() or "missing"
        if found != wanted:
            problems.append(f"{key} is {found}, not {wanted}")
    try:
        pid = os.fork()
    except PermissionError:
        pass
    except OSError as error:
        problems.append(f"fork failed with errno {error.errno}, not EPERM")
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        problems.append("fork allowed")
    if not denied(lambda: open("/plugin/probe-write", "w")):
        problems.append("/plugin is writable")
    if not denied(lambda: socket.socket(socket.AF_UNIX)):
        problems.append("unix sockets allowed")
    if network:
        try:
            internet = socket.socket(socket.AF_INET)
        except OSError:
            problems.append("internet sockets unavailable")
        else:
            with internet:
                if not denied(lambda: internet.bind(("127.0.0.1", 0))):
                    problems.append("bind allowed")
    elif not denied(lambda: socket.socket(socket.AF_INET)):
        problems.append("internet sockets allowed")
    return "; ".join(problems) or "ok"


run()
"""


class PluginError(ValueError):
    """The plugin did not complete the requested operation."""


class PluginsUnavailable(PluginError):
    """No working sandbox is available on this host."""


class PluginBusy(PluginError):
    """Another plugin is running; the caller may retry."""


def parse_output(data: bytes, *, structured: bool = False) -> str | dict:
    """The plugin's result: text for tools and commands, a JSON object for panels (validated by the caller)."""
    if len(data) > MAX_OUTPUT:
        raise PluginError("Plugin output exceeded 16 KB.")
    if not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise PluginError("Invalid plugin protocol response.")
    try:
        result = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise PluginError("Invalid plugin protocol response.") from None
    if isinstance(result, dict) and result.get("ok") is True and set(result) == {"ok", "result"}:
        if structured and isinstance(result["result"], dict):
            return result["result"]
        if not structured and isinstance(result["result"], str):
            return result["result"].encode("utf-8", "replace").decode("utf-8")
    if isinstance(result, dict) and result.get("ok") is False and set(result) == {"ok", "error"}:
        if isinstance(result["error"], str):
            raise PluginError(result["error"].encode("utf-8", "replace").decode("utf-8") or "Plugin reported an error.")
    raise PluginError("Invalid plugin protocol response.")


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value}.")


def _storage_json(data: bytes) -> str:
    if len(data) > MAX_STORAGE:
        raise PluginError("Plugin storage exceeded 64 KB.")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_invalid_json_constant)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, RecursionError):
        raise PluginError("Plugin storage must be valid JSON.") from None
    if size > MAX_STORAGE:
        raise PluginError("Plugin storage exceeded 64 KB.")
    return encoded


def exchange(
    process: subprocess.Popen, request: bytes, timeout: int, *, structured: bool = False,
    save_storage: Callable[[str], None] | None = None,
) -> str | dict:
    """Bound both pipe I/O and exit time, without buffering unlimited output."""
    output = bytearray()
    sent = 0
    deadline = monotonic() + timeout
    limit = MAX_OUTPUT + (MAX_STORAGE + 1 if save_storage is not None else 0)
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
                        chunk = os.read(key.fd, limit + 1 - len(output))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output.extend(chunk)
                        if len(output) > limit:
                            if save_storage is not None and output.find(b"\n", 0, MAX_OUTPUT) >= 0:
                                raise PluginError("Plugin storage exceeded 64 KB.")
                            raise PluginError("Plugin output exceeded 16 KB.")
        if process.wait(timeout=max(0, deadline - monotonic())) != 0:
            raise PluginError("Plugin process failed.")
        if save_storage is not None:
            first, separator, stored = bytes(output).partition(b"\n")
            result = parse_output(first + separator, structured=structured)
            if stored:
                if not stored.endswith(b"\n") or b"\n" in stored[:-1]:
                    raise PluginError("Invalid plugin storage response.")
                save_storage(_storage_json(stored[:-1]))
            return result
        return parse_output(bytes(output), structured=structured)
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


# Syscall numbers per architecture, in this order. Sources: aarch64 include/uapi/asm-generic/unistd.h,
# x86_64 arch/x86/entry/syscalls/syscall_64.tbl, armv7l arch/arm/tools/syscall.tbl (EABI), Linux 6.12.
# None: the architecture has no such call.
_MACHINES = ("aarch64", "x86_64", "armv7l")
_AUDIT_ARCH = {"aarch64": 0xC00000B7, "x86_64": 0xC000003E, "armv7l": 0x40000028}
_SOCKET = (198, 41, 281)
# No new processes, no unix socket pairs, no io_uring (it could bypass the socket restriction).
_NO_PROCESSES = (
    (220, 56, 120),  # clone
    (None, 57, 2),  # fork
    (None, 58, 190),  # vfork
    (435, 435, 435),  # clone3
    (199, 53, 288),  # socketpair
    (None, None, 102),  # socketcall (OABI only; harmless to deny)
    (425, 425, 425),  # io_uring_setup
)
# The filter is default-allow, so also deny what could attack the kernel, other processes or the mounts.
_HARDENED = (
    (117, 101, 26),  # ptrace
    (280, 321, 386),  # bpf
    (241, 298, 364),  # perf_event_open
    (282, 323, 388),  # userfaultfd
    (219, 250, 311),  # keyctl
    (217, 248, 309),  # add_key
    (218, 249, 310),  # request_key
    (40, 165, 21),  # mount
    (39, 166, 52),  # umount2
    (41, 155, 218),  # pivot_root
    (97, 272, 337),  # unshare
    (268, 308, 375),  # setns
    (270, 310, 376),  # process_vm_readv
    (271, 311, 377),  # process_vm_writev
    (265, 304, 371),  # open_by_handle_at
    (272, 312, 378),  # kcmp
)
# An online plugin may connect out but never serve: bind, listen, accept, accept4.
_SERVER_SOCKETS = ((200, 49, 282), (201, 50, 284), (202, 43, 285), (242, 288, 366))


def _seccomp(network: bool, machine: str | None = None) -> bytes:
    """Classic BPF for Linux seccomp_data. Permit only internet client sockets when network is approved."""
    machine = machine or os.uname().machine
    if machine not in _MACHINES:
        raise PluginsUnavailable("No plugin sandbox filter for this Linux architecture.")
    column = _MACHINES.index(machine)
    denied = [
        numbers[column] for numbers in (*_NO_PROCESSES, *_HARDENED, *(_SERVER_SOCKETS if network else ()))
        if numbers[column] is not None
    ]
    load, equal, bit_set, ret = 0x20, 0x15, 0x45, 0x06
    allow, kill, deny = 0x7FFF0000, 0x80000000, 0x00050000 | errno.EPERM
    instructions = [
        (load, 0, 0, 4),  # arch
        (equal, 1, 0, _AUDIT_ARCH[machine]),
        (ret, 0, 0, kill),
        (load, 0, 0, 0),  # syscall number
        (bit_set, 0, 1, 0x40000000),  # reject the x32 compatibility ABI
        (ret, 0, 0, deny),
    ]
    for syscall in denied:
        instructions += [(equal, 0, 1, syscall), (ret, 0, 0, deny)]
    if network:
        # Socket domain is the low 32 bits of args[0]. Supported profiles above are little endian.
        instructions += [
            (equal, 0, 4, _SOCKET[column]),
            (load, 0, 0, 16),
            (equal, 2, 0, 2),   # AF_INET
            (equal, 1, 0, 10),  # AF_INET6
            (ret, 0, 0, deny),
        ]
    else:
        instructions += [(equal, 0, 1, _SOCKET[column]), (ret, 0, 0, deny)]
    instructions.append((ret, 0, 0, allow))
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


class PluginRunner:
    def __init__(self, bwrap: str = "/usr/bin/bwrap", *, home: Path, store: Store | None = None):
        self.bwrap = Path(bwrap)
        self.home = home.resolve()
        self.store = store
        self.last_failure: str | None = None  # why the last self-test failed, for self_inspect

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
            if real == Path("/") or self.home.is_relative_to(real) or real.is_relative_to(self.home):
                raise PluginsUnavailable("A sandbox runtime path overlaps Dom's home.")
        # bwrap 0.12 needs an explicit user namespace for --disable-userns (--unshare-all only tries one).
        cmd = [str(self.bwrap), "--unshare-all", "--unshare-user"]
        if network:
            cmd.append("--share-net")
        cmd += ["--unshare-cgroup", "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin"]
        for mount in mounts:
            cmd += ["--ro-bind", str(mount), str(mount)]
        return cmd + [
            "--ro-bind", str(folder), "/plugin", "--size", str(TMP_BYTES), "--tmpfs", "/tmp", "--proc", "/proc",
            "--dev", "/dev", "--chdir", "/plugin", "--disable-userns", "--assert-userns-disabled", "--cap-drop", "ALL",
            "--seccomp", str(filter_fd), "--",
            "/usr/bin/prlimit", f"--as={manifest.memory_mb * 1024 * 1024}", f"--cpu={manifest.timeout_seconds}",
            f"--fsize={FILE_BYTES}", f"--nofile={OPEN_FILES}", "--core=0",
            "--", str(executable), "-I", "-S", "-c", _BOOTSTRAP,
        ]

    def self_test(self) -> bool:
        if sys.platform != "linux" or not self.bwrap.is_file():
            self.last_failure = f"Plugins need Linux and bubblewrap at {self.bwrap}."
            return False
        key = (self.bwrap, self.home, sys.executable, sys.base_prefix)
        with _PROBE_LOCK:
            cached = _PROBES.get(key)
            if cached is None or (not cached[0] and monotonic() - cached[2] >= FAILURE_RETRY_SECONDS):
                try:
                    reason = self._probe()
                except (PluginError, OSError, ValueError) as error:
                    reason = str(error) or type(error).__name__
                if reason:
                    log.warning("Plugin sandbox self-test failed: %s", reason)
                cached = _PROBES[key] = (not reason, reason, monotonic())
        self.last_failure = cached[1] or None
        return cached[0]

    def _probe(self) -> str:
        """Run the isolation probes offline and online. Return why the sandbox is unsafe, or an empty string."""
        for network in (False, True):
            result = self._probe_once(network)
            if result != "ok":
                return f"{'network' if network else 'offline'} probe: {result}"
        return ""

    def _probe_once(self, network: bool) -> str:
        with TemporaryDirectory(prefix="dom-plugin-probe-") as directory:
            root = Path(directory)
            sentinel = root / "private"
            sentinel.write_text("test")
            folder = root / "probe"
            folder.mkdir()
            (folder / "plugin.toml").write_text(
                'name = "probe"\ndescription = "Isolation probe"\nversion = "1"\n'
                f"network = {str(network).lower()}\n"
                '[[tools]]\nname = "probe"\ndescription = "Check isolation"\nparameters = {type = "object"}\n'
            )
            (folder / "main.py").write_text(_PROBE_MAIN)
            payload = json.dumps({
                "tool": "probe", "args": {"path": str(sentinel), "home": str(self.home), "network": network},
            })
            return self._execute(read_plugin(folder), (payload + "\n").encode(), network)

    def run(self, plugin_dir: Path, tool: str, args: dict, manifest: Manifest, *, approved_hash: str) -> str:
        return self.run_request(plugin_dir, {"tool": tool, "args": args}, manifest, approved_hash=approved_hash)

    def run_request(self, plugin_dir: Path, request: dict, manifest: Manifest, *, approved_hash: str) -> str | dict:
        """Send one request: {"tool", "args"}, {"command", "args", "scope"} or {"panel"}. Panels return an object."""
        if not self.self_test():
            raise PluginsUnavailable("Plugins are unavailable because the sandbox could not be verified.")
        if not _PROCESS.acquire(blocking=False):
            raise PluginBusy("plugin busy, try again")
        try:
            try:
                snapshot = read_plugin(plugin_dir)
                if snapshot.manifest != manifest or snapshot.hash != approved_hash:
                    raise PluginError("Plugin changed since approval.")
                if not manifest.storage:
                    return self._invoke(snapshot, request, manifest.network, structured="panel" in request)
                store = self.store
                if store is None:
                    raise PluginError("Plugin storage is unavailable.")
                name = snapshot.manifest.name

                def save(value: str) -> None:
                    store.write_plugin_storage(name, approved_hash, value)

                try:
                    value = store.read_plugin_storage(name, approved_hash)
                    return self._invoke(
                        snapshot, {**request, "storage": json.loads(value) if value is not None else None},
                        False, structured="panel" in request, save_storage=save,
                    )
                except ValueError as error:
                    raise PluginError(str(error)) from None
            except OSError:
                raise PluginError("Could not start or read the plugin.") from None
        finally:
            _PROCESS.release()

    def trial(self, snapshot: Plugin, sample: dict) -> dict:
        """Run staged bytes offline with synthetic arguments and disposable empty state."""
        if not self.self_test():
            raise PluginsUnavailable("Plugins are unavailable because the sandbox could not be verified.")
        if not _PROCESS.acquire(blocking=False):
            raise PluginBusy("plugin busy, try again")
        try:
            request = {**sample, "trial": True}
            if snapshot.manifest.storage:
                request["storage"] = None
                return self._invoke(snapshot, request, False, structured=True, save_storage=lambda value: None)
            return self._invoke(snapshot, request, False, structured=True)
        except OSError:
            raise PluginError("Could not start the plugin test.") from None
        finally:
            _PROCESS.release()

    def _invoke(
        self, snapshot: Plugin, request: dict, network: bool, *, structured: bool,
        save_storage: Callable[[str], None] | None = None,
    ) -> str | dict:
        line = json.dumps(request, allow_nan=False, separators=(",", ":")) + "\n"
        options = {"save_storage": save_storage} if save_storage is not None else {}
        return self._execute_locked(snapshot, line.encode("utf-8"), network, structured=structured, **options)

    def _execute(self, snapshot: Plugin, request: bytes, network: bool) -> str:
        with _PROCESS:
            return self._execute_locked(snapshot, request, network)

    def _execute_locked(
        self, snapshot: Plugin, request: bytes, network: bool, *, structured: bool = False,
        save_storage: Callable[[str], None] | None = None,
    ) -> str | dict:
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
                return exchange(
                    process, request, snapshot.manifest.timeout_seconds, structured=structured, save_storage=save_storage,
                )
            except PluginError as error:
                errors.seek(0)
                detail = errors.read(500).decode("utf-8", "replace").strip()
                if detail:  # logged for the operator only; the model sees the plain error
                    log.warning("plugin %s failed: %s (%s)", snapshot.manifest.name, error, detail)
                raise

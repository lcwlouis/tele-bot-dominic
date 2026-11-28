"""Local and remote sandbox lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Collection
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Literal

import httpx

from dom import workspace
from dom.config import Config, DesktopHost
from dom.desktop import network
from dom.desktop.client import SandboxClient, SandboxError

Status = Literal["off", "stopped", "starting", "ready", "error"]
log = logging.getLogger(__name__)
DEFAULT = "default"  # the name of the container that existed before there could be more than one
CONTAINER = "dom-desktop"
# The container of Dom's self-directed tasks (W13). Containers share the desktop bridge, so it runs only while
# every other local container is stopped, and no other may start while it runs.
SELF_WORK = "self-work"
# The container of tasks that groups and friends ask for. It has no browser profile on disk (the profile lives in the
# container and goes with it), none of the owner's mounts, and no logins; the owner's own tasks never run in it.
GUEST = "guest"
# Run in a throwaway container with the workspace mounted: the user's id, then a write and a delete in the folder.
WORKSPACE_PROBE = 'id -u; f=/workspace/.dom-probe-$$; : > "$f" && rm -f "$f"'
CONTAINER_MEMORY_MB = 1600  # the --memory limit of a local container
SECCOMP_PROFILE = Path(__file__).resolve().with_name("seccomp-chromium.json")
LAN_REACHABLE = "LAN reachable from the sandbox — run dom desktop-setup"
REMOTE_UNCONFIRMED = (
    "remote desktop not started: set [desktop] remote_isolation_confirmed = true once the remote host blocks access to your LAN"
)


def valid_remote_url(value: str) -> bool:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return False
    return url.scheme in ("http", "https") and bool(url.host)


def container_name(name: str) -> str:
    """The Docker name of a desktop container: the original name for the default one, a suffix for the rest."""
    return CONTAINER if name == DEFAULT else f"{CONTAINER}-{name}"


def desktop_profile_path(config: Config, name: str = DEFAULT) -> Path:
    """Where a container keeps its browser profile (and so its logins), outside DOM_HOME."""
    home = config.home.resolve()
    setting = config.desktop.profile_dir
    profile = (home / setting if setting else home.parent / "desktop-profile").resolve()
    if name != DEFAULT:
        profile = profile.with_name(f"{profile.name}-{name}")
    if profile.is_relative_to(home) or home.is_relative_to(profile):
        raise SandboxError("desktop profile must be outside DOM_HOME")
    return profile


def workspace_volume(config: Config, name: str = DEFAULT) -> tuple[str, ...]:
    """The `docker run` arguments that mount the shared workspace at /workspace, or none for Dom's self-directed
    and guest containers, which never see the owner's files."""
    if name in (SELF_WORK, GUEST):
        return ()
    try:
        folder = workspace.ensure_workspace(config.home)
    except (OSError, workspace.WorkspaceError) as error:
        raise SandboxError(f"could not prepare the workspace folder ({error})") from None
    return ("-v", f"{folder.resolve()}:{workspace.MOUNT}")


class Refused(SandboxError):
    """A container was not started because its host is full or short of memory. Nothing is broken: try later."""


def read_free_memory_mb(meminfo: str = "/proc/meminfo") -> int | None:
    """This machine's available memory (MemAvailable), or None where /proc/meminfo is unavailable."""
    try:
        for line in Path(meminfo).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class Host:
    """A machine that runs desktop containers, with the limits that protect it.

    Starts on one host are serialized by `start_lock`, so two containers cannot both pass the check for
    the last free slot, and each memory reading sees the container that started before it."""

    def __init__(self, config: DesktopHost, *, free_memory: Callable[[], int | None] = read_free_memory_mb):
        self.config = config
        self.free_memory = free_memory
        self.sandboxes: dict[str, LocalSandbox | RemoteSandbox] = {}
        self.start_lock = asyncio.Lock()

    def register(self, sandbox: LocalSandbox | RemoteSandbox) -> None:
        self.sandboxes[sandbox.name] = sandbox

    def running(self, besides: str, busy: Collection[str] = ()) -> list[str]:
        """Containers that hold a slot: started or starting, or with a task queued for them."""
        names = {name for name, other in self.sandboxes.items() if other.status() in ("starting", "ready")}
        return sorted((names | set(busy)) - {besides})

    async def refusal(self, sandbox: LocalSandbox | RemoteSandbox, *, busy: Collection[str] = (), memory: bool = True) -> str | None:
        """Why `sandbox` may not start now, or None. `memory=False` skips a reading that would cost a request."""
        settings = self.config
        if settings.mode == "local":
            # Containers share the desktop bridge. A container that runs pages nobody vetted (Dom's own tasks, a task
            # for a chat) runs only while every other local container is stopped, and nothing starts beside it.
            if sandbox.name in (SELF_WORK, GUEST):
                others = sorted(
                    name for name, other in self.sandboxes.items() if name != sandbox.name and other.status() not in ("stopped", "off")
                )
                if others:
                    who = "Dom's own tasks" if sandbox.name == SELF_WORK else "Tasks for a chat"
                    return f"{who} run only while no other desktop container is running ({', '.join(others)})"
            else:
                holding = self.running(sandbox.name, busy)
                if SELF_WORK in holding:
                    return "Dom's own task is using the desktop; cancel it on the Own work page or wait for it to finish"
                if GUEST in holding:
                    return "Dom is on a task for a chat; try again when it has finished"
        holding = self.running(sandbox.name, busy)
        if len(holding) >= settings.max_containers:
            return (
                f"host {settings.name} already runs its limit of {settings.max_containers} desktop "
                f"container{'s' if settings.max_containers != 1 else ''} ({', '.join(holding)}); "
                "wait for one to finish, or raise max_containers"
            )
        floor = settings.memory_floor_mb
        if floor and memory:
            free = await sandbox.free_memory_mb()
            if free is None:
                return f"could not read free memory on host {settings.name}, so no container was started"
            need = sandbox.memory_mb + floor
            if free < need:
                return (
                    f"not enough free memory on host {settings.name}: {free} MB free, {need} MB needed "
                    f"({sandbox.memory_mb} MB for the container plus the {floor} MB floor)"
                )
        return None

    async def admit(self, sandbox: LocalSandbox | RemoteSandbox) -> None:
        if reason := await self.refusal(sandbox):
            raise Refused(reason)

    def claim_port(self, name: str, base: int) -> int:
        taken = {other.port for other in self.sandboxes.values() if other.name != name and other.status() != "stopped"}
        return next(port for port in range(base, 65536) if port not in taken)


class RemoteSandbox:
    memory_mb = 0  # Dom does not start this sandbox, so it does not know its limit
    port = None
    workspace_mounted = False  # a remote host cannot see Dom's folders

    def __init__(
        self, url: str, api_key: str | None, *, http: httpx.AsyncClient | None = None, isolation_confirmed: bool = False,
        name: str = DEFAULT, host: Host | None = None,
    ):
        self.name = name
        self.host = host
        if host is not None:
            host.register(self)
        self._isolation_confirmed = isolation_confirmed
        self._http = http or httpx.AsyncClient(trust_env=False)
        self._owns_http = http is None
        self._lock = asyncio.Lock()
        self._status: Status = "stopped"
        self.error: str | None = None
        self.client: SandboxClient | None = None
        if not valid_remote_url(url):
            self._status = "error"
            self.error = "desktop URL is missing or invalid"
        else:
            self._remote = SandboxClient(url, api_key, self._http)

    def status(self) -> Status:
        return self._status

    async def ensure_running(self) -> SandboxClient:
        async with self._lock:
            if not hasattr(self, "_remote"):
                raise SandboxError(self.error)
            if not self._isolation_confirmed:
                self.client = None
                self._status = "error"
                self.error = REMOTE_UNCONFIRMED
                raise SandboxError(self.error)
            self._status = "starting"
            try:
                await self._remote.health()
            except SandboxError:
                self.client = None
                self._status = "error"
                self.error = "remote health check failed"
                raise SandboxError(self.error) from None
            except asyncio.CancelledError:
                self._status = "ready" if self.client else "stopped"
                raise
            if self.host is None:
                return self._ready()
            # Admission and taking the slot happen together under the host's lock, as for a local start.
            async with self.host.start_lock:
                try:
                    await self.host.admit(self)
                except Refused as exc:
                    self.client = None
                    self._status = "stopped"
                    self.error = str(exc)
                    raise SandboxError(self.error) from None
                return self._ready()

    def _ready(self) -> SandboxClient:
        self.client = self._remote
        self._status = "ready"
        self.error = None
        return self.client

    async def free_memory_mb(self) -> int | None:
        """The remote machine's MemAvailable, read through the sandbox's own shell."""
        try:
            result = await self._remote.bash("awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo")
            text = result.get("stdout") if isinstance(result, dict) and result.get("exit_code") == 0 else None
            return int(str(text).strip())
        except (SandboxError, ValueError):
            return None

    def touch(self) -> None:
        pass

    def in_use(self):
        return nullcontext()

    async def reset_guest(self) -> None:
        """Reset a dedicated remote guest sandbox between tasks: blank page, clear browser storage/clipboard,
        and wipe outbox + temp files. On failure the sandbox is marked error so no task runs on dirty state."""
        client = await self.ensure_running()
        try:
            await client.navigate("about:blank")
            await client._eval_raw(
                "(() => { try { localStorage.clear(); } catch (e) {} "
                "try { sessionStorage.clear(); } catch (e) {} "
                "try { document.cookie.split(';').forEach(c => { const n = c.split('=')[0].trim(); if (n) document.cookie = n + '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/'; }); } catch (e) {} "
                "return true; })()"
            )
            await client.clear_clipboard()
            res = await client.bash("rm -rf /home/gem/outbox/* /tmp/dom-guest-* /tmp/*.png /tmp/screenshot*.png")
            if not isinstance(res, dict) or res.get("exit_code") not in (0, None):
                raise SandboxError("cleanup command failed")
        except Exception as exc:
            self._status = "error"
            self.error = f"remote guest sandbox could not be reset: {exc}"
            raise SandboxError(self.error) from None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class LocalSandbox:
    memory_mb = CONTAINER_MEMORY_MB

    @property
    def workspace_mounted(self) -> bool:
        """Whether the running container has the workspace at /workspace: only after a start that checked it."""
        return self._workspace_mounted

    def __init__(
        self, config: Config, run=asyncio.create_subprocess_exec, *, http=None, clock=time.monotonic,
        name: str = DEFAULT, host: Host | None = None,
    ):
        self.config = config
        self.name = name
        self.container = container_name(name)
        self.port = config.desktop.port
        self.host = host
        if host is not None:
            host.register(self)
        self._run = run
        self._http = http or httpx.AsyncClient(trust_env=False)
        self._owns_http = http is None
        self._clock = clock
        self._last_use = clock()
        self._users = 0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._status: Status = "off" if config.desktop_mode == "off" else "stopped"
        self._workspace_mounted = False
        self.error: str | None = None
        self.client: SandboxClient | None = None
        # Told once, with a short message, when the LAN probe stops a start.
        self.alert: Callable[[str], Awaitable[None]] | None = None
        self._alerted = False
        # Called whenever this container stops (explicit stop, idle stop, reset or close).
        self.on_stop: Callable[[], None] | None = None

    def status(self) -> Status:
        return self._status

    def touch(self) -> None:
        self._last_use = self._clock()

    @contextmanager
    def in_use(self):
        self._users += 1
        self.touch()
        try:
            yield
        finally:
            self._users -= 1
            self.touch()

    async def _docker(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        extra = {} if env is None else {"env": {**os.environ, **env}}
        try:
            process = await self._run(
                "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **extra,
            )
            stdout, _ = await process.communicate()
        except OSError:
            raise SandboxError("Docker is unavailable") from None
        return process.returncode, stdout

    async def _docker_to_completion(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
        """A cancelled `docker run` leaves its CLI running, and it may still create the container.
        Wait for it so cleanup sees the container."""
        call = asyncio.ensure_future(self._docker(*args, env=env))
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            await asyncio.gather(call, return_exceptions=True)
            raise

    async def _discard(self) -> bool:
        """Best effort: nothing else owns the container, so stop and remove it whatever state it is in.
        Failures are logged; the result says whether the container is gone."""
        try:
            if (await self._docker("container", "inspect", "--format", "{{.State.Running}}", self.container))[0]:
                return True  # no such container
            gone = True
            for verb in ("stop", "rm"):
                if (await self._docker(verb, self.container))[0]:
                    log.warning("could not %s the desktop container", verb)
                    gone = False
            return gone
        except SandboxError:
            log.warning("could not discard the desktop container: Docker is unavailable")
            return False

    async def _remove_container(self, running: bool) -> None:
        if running and (await self._docker("stop", self.container))[0]:
            raise SandboxError("could not stop the desktop container")
        if (await self._docker("rm", self.container))[0]:
            raise SandboxError("could not remove the desktop container")

    async def ensure_running(self) -> SandboxClient:
        async with self._lock:
            if self._status == "off":
                raise SandboxError("desktop is off")
            if self._status == "ready" and self.client is not None:
                self.touch()
                return self.client
            self._status = "starting"
            self.error = None
            try:
                if self.host is None:
                    await self._start()
                else:
                    async with self.host.start_lock:
                        await self.host.admit(self)
                        await self._start()
            except Refused as exc:
                self._status = "stopped"  # nothing is wrong with this container; the host is full
                self.error = str(exc)
                raise Refused(self.error) from None
            except (SandboxError, OSError) as exc:
                self._status = "error"
                self.error = str(exc) if isinstance(exc, SandboxError) else "could not prepare desktop profile"
                raise SandboxError(self.error) from None
            except asyncio.CancelledError:
                self._status = "stopped"
                if not await asyncio.shield(self._discard()):
                    self._status = "error"
                    self.error = "could not remove the desktop container"
                raise
            self._status = "ready"
            self.touch()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._watch_idle())
            assert self.client is not None
            return self.client

    async def free_memory_mb(self) -> int | None:
        return self.host.free_memory() if self.host is not None else None

    async def existing_names(self) -> list[str]:
        """Names of the desktop containers Docker has now, running or not, as Dom's container names."""
        code, output = await self._docker("ps", "-a", "--filter", f"name=^{CONTAINER}", "--format", "{{.Names}}")
        if code:
            raise SandboxError("could not list desktop containers")
        names = []
        for line in output.decode(errors="replace").split():
            if line == CONTAINER:
                names.append(DEFAULT)
            elif line.startswith(CONTAINER + "-"):
                names.append(line.removeprefix(CONTAINER + "-"))
        return names

    async def _verify_firewall(self) -> None:
        """Fail closed, on every start: the sandbox starts only after a throwaway container on the
        desktop network fails to reach the LAN while it does reach the control address."""
        settings = self.config.desktop
        if not settings.lan_probes:
            raise SandboxError("set [desktop] lan_probes to LAN ipv4:port addresses so Dom can verify the firewall")
        code, output = await self._docker("network", "inspect", settings.network)
        try:
            if code:
                raise ValueError
            details = json.loads(output)[0]
            network.verify_network(details, settings.subnet)
            gateway = network.gateway(details, settings.subnet)
        except (ValueError, LookupError, TypeError):
            raise SandboxError("desktop network missing or not the configured IPv4 bridge — run dom desktop-setup") from None
        if (await self._docker("image", "inspect", settings.probe_image))[0]:
            raise SandboxError(f"probe image not present: docker pull {settings.probe_image}")
        try:
            probes = await asyncio.to_thread(network.lan_targets, settings.lan_probes)
        except ValueError as exc:
            raise SandboxError(f"could not verify the desktop firewall: {exc}") from None
        # A timeout proves a block only for a target that answers from this host; a switched-off device proves
        # nothing. Probe the ones that answer, and refuse when none does.
        live = await network.answering(probes)
        if not live:
            raise SandboxError(
                "could not verify the desktop firewall: no lan_probes target answers from this host, so a timeout would prove nothing",
            )
        targets = [*(probe for probe in probes if probe in live), f"{gateway}:{self.config.web.port}", f"{gateway}:22"]
        code, _ = await self._docker(*network.probe_command(self.config, *targets)[1:])
        if code == network.PROBE_REACHABLE:
            if self.alert is not None and not self._alerted:
                self._alerted = True
                try:
                    await self.alert("The desktop did not start: the sandbox can reach your LAN. Run dom desktop-setup on the Docker host.")
                except Exception as exc:
                    log.warning("could not alert the owner about the desktop firewall (%s)", type(exc).__name__)
            raise SandboxError(LAN_REACHABLE)
        if code == network.PROBE_NO_CONTROL:
            raise SandboxError("could not verify the desktop firewall: the probe could not reach its control address")
        if code:
            raise SandboxError("could not verify the desktop firewall")
        self._alerted = False

    async def _start(self) -> None:
        settings = self.config.desktop
        if (await self._docker("image", "inspect", settings.image))[0]:
            raise SandboxError("image not present")
        await self._verify_firewall()
        exists, running = await self._docker("container", "inspect", "--format", "{{.State.Running}}", self.container)
        if exists == 0:
            # A previous Dom process may have exited with its key still in memory.
            await self._remove_container(running.strip() == b"true")
        guest = self.name == GUEST
        if guest:
            # Nothing is mounted: no browser profile (so no logins, and nothing survives the container) and no
            # workspace, the shared folder included.
            mount = ()
            self._workspace_mounted = False
            profile_mount: tuple[str, ...] = ()
        else:
            profile = desktop_profile_path(self.config, self.name)
            profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            # No container uses the profile now. A lock left by an unclean stop names the old
            # container, and Chromium would refuse the profile until it is gone.
            for lock in profile.glob("Singleton*"):
                lock.unlink(missing_ok=True)
            mount = await self._workspace_mount()
            profile_mount = ("-v", f"{profile}:/home/gem/.config/browser")
        key = secrets.token_urlsafe(32)
        if self.host is not None:
            self.port = self.host.claim_port(self.name, settings.port)
        code, _ = await self._docker_to_completion(
            "run", "-d", "--name", self.container, "--pull=never", "--network", settings.network,
            "-p", f"127.0.0.1:{self.port}:8080", "--shm-size", "1g",
            "--memory", f"{CONTAINER_MEMORY_MB}m", "--memory-swap", f"{CONTAINER_MEMORY_MB}m", "--pids-limit", "512",
            "--security-opt", "no-new-privileges", "--security-opt", f"seccomp={SECCOMP_PROFILE}", "--cpus", "3",
            "-e", "SANDBOX_API_KEY", "-e", "DISABLE_CODE_SERVER=true",
            "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true", "-e", "DISABLE_JUPYTER=true",
            *profile_mount, *mount, settings.image,
            env={"SANDBOX_API_KEY": key},  # keeps the key out of the process list
        )
        if code:
            raise SandboxError("could not start the desktop container")
        client = SandboxClient(f"http://127.0.0.1:{self.port}", key, self._http)
        failure = "API not healthy"
        try:
            async with asyncio.timeout(settings.start_timeout_seconds):
                await self._wait_until_ready(client.health)
                failure = "browser did not start"
                await self._wait_until_ready(client.browser_tabs)
        except TimeoutError:
            if not await self._discard():
                failure += " (could not remove the desktop container)"
            raise SandboxError(failure) from None
        self.client = client

    async def _workspace_mount(self) -> tuple[str, ...]:
        """The `-v` arguments for the workspace, if the container's user can use the folder; otherwise none, with the
        reason kept for the doctor and the Files page. A bind mount shows the host's owner, so a container user that is
        not the folder's owner cannot write there (or leaves files Dom cannot read), and the desktop is better off
        without it than with a folder that fails halfway through a task."""
        self._workspace_mounted = False
        mount = workspace_volume(self.config, self.name)
        if not mount:
            return ()
        folder = workspace.workspace_dir(self.config.home).resolve()
        code, output = await self._docker_to_completion(
            "run", "--rm", "--pull=never", "--network", "none", "--memory", "64m", "--pids-limit", "32",
            "--security-opt", "no-new-privileges", "--entrypoint", "sh", "-v", f"{folder}:{workspace.MOUNT}",
            self.config.desktop.image, "-c", WORKSPACE_PROBE,
        )
        words = output.split()
        owner = folder.stat().st_uid
        if code == 0 and words and words[0] == str(owner).encode():
            workspace.set_mount_problem(self.config.home, None)
            self._workspace_mounted = True
            return mount
        who = words[0].decode(errors="replace")[:12] if words and words[0].isdigit() else "unknown"
        problem = f"the desktop can't write to the workspace: its user is uid {who}, the folder belongs to uid {owner}"
        log.error("%s. The desktop starts without it; run the sandbox as the host user or chown the folder.", problem)
        workspace.set_mount_problem(self.config.home, problem)
        return ()

    @staticmethod
    async def _wait_until_ready(check: Callable[[], Awaitable[object]]) -> None:
        while True:
            try:
                await check()
                return
            except SandboxError:
                await asyncio.sleep(1)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def reset_profile(self) -> None:
        """Stop this container and delete its browser profile, so its next start has no logins or history.
        Used before every self-directed task; the default container's profile (the owner's logins) is never reset."""
        if self.name == DEFAULT:
            raise SandboxError("the default desktop's profile is never reset")
        async with self._lock:
            await self._stop()
            profile = desktop_profile_path(self.config, self.name)
            try:
                await asyncio.to_thread(shutil.rmtree, profile)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SandboxError("could not empty the desktop profile") from exc

    async def _stop(self) -> None:
        if self._status == "off":
            return
        try:
            exists, running = await self._docker("container", "inspect", "--format", "{{.State.Running}}", self.container)
            if exists == 0:
                await self._remove_container(running.strip() == b"true")
        except SandboxError as exc:
            self._status = "error"
            self.error = str(exc)
            raise
        self.client = None
        self.error = None
        self._status = "stopped"
        if self.on_stop is not None:
            self.on_stop()

    async def stop_if_idle(self) -> None:
        async with self._lock:
            cutoff = self.config.desktop.idle_minutes * 60
            if self._status == "ready" and not self._users and self._clock() - self._last_use >= cutoff:
                await self._stop()

    async def _watch_idle(self) -> None:
        while self._status == "ready":
            await asyncio.sleep(min(30, self.config.desktop.idle_minutes * 60))
            try:
                await self.stop_if_idle()
            except SandboxError:
                break

    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            await asyncio.gather(self._idle_task, return_exceptions=True)
        if self._status in ("ready", "starting", "error"):
            await asyncio.shield(self._discard())
            self.client = None
            self._status = "stopped"
            if self.on_stop is not None:
                self.on_stop()
        if self._owns_http:
            await self._http.aclose()

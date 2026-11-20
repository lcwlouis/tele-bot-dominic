"""Local and remote sandbox lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Literal

import httpx

from dom.config import Config
from dom.desktop import network
from dom.desktop.client import SandboxClient, SandboxError

Status = Literal["off", "stopped", "starting", "ready", "error"]
log = logging.getLogger(__name__)
CONTAINER = "dom-desktop"
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


def desktop_profile_path(config: Config) -> Path:
    home = config.home.resolve()
    setting = config.desktop.profile_dir
    profile = (home / setting if setting else home.parent / "desktop-profile").resolve()
    if profile.is_relative_to(home) or home.is_relative_to(profile):
        raise SandboxError("desktop profile must be outside DOM_HOME")
    return profile


class RemoteSandbox:
    def __init__(
        self, url: str, api_key: str | None, *, http: httpx.AsyncClient | None = None, isolation_confirmed: bool = False,
    ):
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
            self.client = self._remote
            self._status = "ready"
            self.error = None
            return self.client

    def touch(self) -> None:
        pass

    def in_use(self):
        return nullcontext()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()


class LocalSandbox:
    def __init__(self, config: Config, run=asyncio.create_subprocess_exec, *, http=None, clock=time.monotonic):
        self.config = config
        self._run = run
        self._http = http or httpx.AsyncClient(trust_env=False)
        self._owns_http = http is None
        self._clock = clock
        self._last_use = clock()
        self._users = 0
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._status: Status = "off" if config.desktop_mode == "off" else "stopped"
        self.error: str | None = None
        self.client: SandboxClient | None = None
        # Told once, with a short message, when the LAN probe stops a start.
        self.alert: Callable[[str], Awaitable[None]] | None = None
        self._alerted = False

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
            if (await self._docker("container", "inspect", "--format", "{{.State.Running}}", CONTAINER))[0]:
                return True  # no such container
            gone = True
            for verb in ("stop", "rm"):
                if (await self._docker(verb, CONTAINER))[0]:
                    log.warning("could not %s the desktop container", verb)
                    gone = False
            return gone
        except SandboxError:
            log.warning("could not discard the desktop container: Docker is unavailable")
            return False

    async def _remove_container(self, running: bool) -> None:
        if running and (await self._docker("stop", CONTAINER))[0]:
            raise SandboxError("could not stop the desktop container")
        if (await self._docker("rm", CONTAINER))[0]:
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
                await self._start()
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
        targets = [*probes, f"{gateway}:{self.config.web.port}", f"{gateway}:22"]
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
        exists, running = await self._docker("container", "inspect", "--format", "{{.State.Running}}", CONTAINER)
        if exists == 0:
            # A previous Dom process may have exited with its key still in memory.
            await self._remove_container(running.strip() == b"true")
        profile = desktop_profile_path(self.config)
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        # No container uses the profile now. A lock left by an unclean stop names the old
        # container, and Chromium would refuse the profile until it is gone.
        for lock in profile.glob("Singleton*"):
            lock.unlink(missing_ok=True)
        key = secrets.token_urlsafe(32)
        code, _ = await self._docker_to_completion(
            "run", "-d", "--name", CONTAINER, "--pull=never", "--network", settings.network,
            "-p", f"127.0.0.1:{settings.port}:8080", "--shm-size", "1g", "--memory", "1600m", "--memory-swap", "1600m", "--pids-limit", "512",
            "--security-opt", "no-new-privileges", "--security-opt", f"seccomp={SECCOMP_PROFILE}", "--cpus", "3",
            "-e", "SANDBOX_API_KEY", "-e", "DISABLE_CODE_SERVER=true",
            "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true", "-e", "DISABLE_JUPYTER=true",
            "-v", f"{profile}:/home/gem/.config/browser", settings.image,
            env={"SANDBOX_API_KEY": key},  # keeps the key out of the process list
        )
        if code:
            raise SandboxError("could not start the desktop container")
        client = SandboxClient(f"http://127.0.0.1:{settings.port}", key, self._http)
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

    async def _stop(self) -> None:
        if self._status == "off":
            return
        try:
            exists, running = await self._docker("container", "inspect", "--format", "{{.State.Running}}", CONTAINER)
            if exists == 0:
                await self._remove_container(running.strip() == b"true")
        except SandboxError as exc:
            self._status = "error"
            self.error = str(exc)
            raise
        self.client = None
        self.error = None
        self._status = "stopped"

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
        if self._owns_http:
            await self._http.aclose()


def configured_sandbox(config: Config) -> LocalSandbox | RemoteSandbox | None:
    if config.desktop_mode == "local":
        return LocalSandbox(config)
    if config.desktop_mode == "remote":
        return RemoteSandbox(
            config.desktop.url, config.secret(config.desktop.api_key_env),
            isolation_confirmed=config.desktop.remote_isolation_confirmed,
        )
    return None

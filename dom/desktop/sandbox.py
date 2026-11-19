"""Local and remote sandbox lifecycle."""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Literal

import httpx

from dom.config import Config
from dom.desktop.client import SandboxClient, SandboxError

Status = Literal["off", "stopped", "starting", "ready", "error"]
CONTAINER = "dom-desktop"


class RemoteSandbox:
    def __init__(self, url: str, api_key: str | None, *, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(trust_env=False)
        self._owns_http = http is None
        self._lock = asyncio.Lock()
        self._status: Status = "stopped"
        self.error: str | None = None
        self.client: SandboxClient | None = None
        if httpx.URL(url).scheme not in ("http", "https") or not httpx.URL(url).host:
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
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._status: Status = "off" if config.desktop_mode == "off" else "stopped"
        self.error: str | None = None
        self.client: SandboxClient | None = None

    def status(self) -> Status:
        return self._status

    def touch(self) -> None:
        self._last_use = self._clock()

    async def _docker(self, *args: str) -> tuple[int, bytes]:
        try:
            process = await self._run("docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate()
        except OSError:
            raise SandboxError("Docker is unavailable") from None
        return process.returncode, stdout

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
                raise
            self._status = "ready"
            self.touch()
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._watch_idle())
            assert self.client is not None
            return self.client

    async def _start(self) -> None:
        settings = self.config.desktop
        if (await self._docker("image", "inspect", settings.image))[0]:
            raise SandboxError("image not present")
        exists, running = await self._docker("container", "inspect", "--format", "{{.State.Running}}", CONTAINER)
        if exists == 0:
            # A previous Dom process may have exited with its key still in memory.
            await self._remove_container(running.strip() == b"true")
        home = self.config.home.resolve()
        profile = Path(settings.profile_dir) if settings.profile_dir else home.parent / "desktop-profile"
        if not profile.is_absolute():
            profile = home / profile
        profile = profile.resolve()
        if profile == home or home in profile.parents or profile in home.parents:
            raise SandboxError("desktop profile must be outside DOM_HOME")
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        code, _ = await self._docker(
            "run", "-d", "--name", CONTAINER, "--pull=never", "--network", settings.network,
            "-p", f"127.0.0.1:{settings.port}:8080", "--shm-size", "1g", "--memory", "1600m", "--cpus", "3",
            "-e", f"SANDBOX_API_KEY={key}", "-e", "DISABLE_CODE_SERVER=true",
            "-e", "DISABLE_NODEJS_REPL=true", "-e", "DISABLE_MCP_BROWSER=true",
            "-v", f"{profile}:/home/gem/.config/browser", settings.image,
        )
        if code:
            raise SandboxError("could not start the desktop container")
        client = SandboxClient(f"http://127.0.0.1:{settings.port}", key, self._http)
        try:
            async with asyncio.timeout(60):
                while True:
                    try:
                        await client.health()
                        break
                    except SandboxError:
                        await asyncio.sleep(1)
        except TimeoutError:
            await self._remove_container(True)
            raise SandboxError("desktop health check timed out") from None
        self.client = client

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
            if self._status == "ready" and self._clock() - self._last_use >= self.config.desktop.idle_minutes * 60:
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
        if self._owns_http:
            await self._http.aclose()


def configured_sandbox(config: Config) -> LocalSandbox | RemoteSandbox | None:
    if config.desktop_mode == "local":
        return LocalSandbox(config)
    if config.desktop_mode == "remote":
        return RemoteSandbox(config.desktop.url, config.secret(config.desktop.api_key_env))
    return None

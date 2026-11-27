"""Named desktop containers. Each has its own sandbox and worker; the hosts limit how many run at once."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from dom import workspace
from dom.config import HOST_NAME
from dom.desktop.client import SandboxError
from dom.desktop.files import drop_stale
from dom.desktop.sandbox import DEFAULT, GUEST, Host, LocalSandbox, RemoteSandbox, read_free_memory_mb
from dom.desktop.worker import ACTIVE, DesktopWorker

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
NAME = re.compile(HOST_NAME)
NAME_RULE = "a container name is up to 24 lowercase letters, digits and hyphens, starting with a letter or digit"


@dataclass
class Lane:
    """One container: where it runs, the sandbox that starts and stops it, and the worker that runs its tasks."""

    name: str
    host: Host
    sandbox: LocalSandbox | RemoteSandbox
    worker: DesktopWorker
    # A dashboard stop in flight: `docker stop` can take a while, so it runs apart from the request that asked.
    stopping: asyncio.Task | None = field(default=None, compare=False)

    def status(self) -> str:
        if self.stopping is not None and not self.stopping.done():
            return "stopping"
        return self.sandbox.status()

    def idle(self) -> bool:
        return not self.worker.busy() and self.status() in ("stopped", "error")


class DesktopFleet:
    """Routes desktop tasks to named containers.

    `default` is the container that existed before there could be several. A remote host is one
    container, named after the host; the default and each remote host have a lane from the start. Any other name is a container on the local host, created on first use
    and forgotten once it is stopped and has no task (its browser profile stays on disk, so logins last)."""

    def __init__(
        self, app: App, *, free_memory: Callable[[], int | None] = read_free_memory_mb,
        sandbox_factory: Callable[[Host, str], LocalSandbox | RemoteSandbox] | None = None, **worker_options,
    ):
        self.app = app
        self.hosts = [Host(config, free_memory=free_memory) for config in app.config.desktop_hosts]
        self._factory = sandbox_factory or self._build
        self._options = worker_options
        self.lanes: dict[str, Lane] = {}
        # A host kept for guests never runs the owner's tasks, so it is never the default container.
        first = next((host.config for host in self.hosts if not host.config.guest), None)
        self.default = first.name if first is not None and first.mode == "remote" else DEFAULT
        # The default container and every remote host (a remote host is one container) exist from the start, so
        # they are visible and usable without a first use. Starting any of them stays lazy.
        self.permanent = [
            *([self.default] if first is not None else []),
            *(h.config.name for h in self.hosts if h.config.mode == "remote" and h.config.name != self.default),
        ]
        for name in self.permanent:
            self.lane(name)

    def _build(self, host: Host, name: str) -> LocalSandbox | RemoteSandbox:
        config, settings = self.app.config, host.config
        if settings.mode == "local":
            return LocalSandbox(config, name=name, host=host)
        return RemoteSandbox(
            settings.url, config.secret(settings.api_key_env),
            isolation_confirmed=settings.remote_isolation_confirmed, name=name, host=host,
        )

    def _resolve(self, name: str | None) -> tuple[str, Host]:
        if name is None or name == DEFAULT:
            if not any(not host.config.guest for host in self.hosts):
                raise ValueError("every desktop host is kept for tasks that groups and friends ask for")
            name = self.default
        if not isinstance(name, str):
            raise ValueError(NAME_RULE)
        for host in self.hosts:
            if host.config.mode == "remote" and host.config.name == name:
                return name, host
        if not NAME.fullmatch(name):
            raise ValueError(NAME_RULE)
        for host in self.hosts:
            if host.config.mode == "local":
                return name, host
        raise ValueError(f"no local host is configured, so the only container is {self.default}")

    def lane(self, name: str | None = None) -> Lane:
        name, host = self._resolve(name)
        if name not in self.lanes:
            sandbox = self._factory(host, name)
            worker = DesktopWorker(self.app, sandbox, container=name, **self._options)
            self.lanes[name] = lane = Lane(name, host, sandbox, worker)
            if self.for_guests(lane):
                worker.guest_admit = partial(self._guest_refusal, lane)
        return self.lanes[name]

    @staticmethod
    def for_guests(lane: Lane) -> bool:
        """Whether tasks that groups and friends ask for run in this container, and the owner's never do."""
        return lane.name == GUEST or lane.host.config.guest

    def has_guest_host(self) -> bool:
        """Whether some host can run guest tasks: one kept for guests, or the local host."""
        return any(host.config.guest or host.config.mode == "local" for host in self.hosts)

    def guest_lane(self) -> Lane | None:
        """Where a guest task runs: a host kept for guests first, else a container of its own on the local host."""
        try:
            name = next((host.config.name for host in self.hosts if host.config.guest), GUEST)
            return self.lane(name)
        except ValueError:
            return None  # no guest host and no local host

    def containers(self) -> list[Lane]:
        """The containers worth showing: the default one and each remote host, and every other that is running or has a task."""
        return [lane for name, lane in self.lanes.items() if name in self.permanent or not lane.idle()]

    async def drop_idle(self) -> None:
        for name, lane in list(self.lanes.items()):
            if name not in self.permanent and lane.idle():
                del self.lanes[name]
                lane.host.sandboxes.pop(name, None)
                if (close := getattr(lane.sandbox, "close", None)) is not None:
                    await close()

    async def own_lane(self, name: str) -> tuple[Lane | None, str | None]:
        """The local container for a self-directed task, and why it may not start now (None when it may).
        A missing or remote lane is no lane: self-directed work runs only in a local container."""
        await self.drop_idle()
        try:
            lane = self.lane(name)
        except ValueError:
            lane = None
        if lane is None or lane.host.config.mode != "local":
            return None, "self-directed work needs a local desktop host"
        # Containers share the desktop bridge: wait until every other local one is stopped. The host's admission
        # check enforces the same at the start itself.
        others = sorted(
            other.name for other in self.lanes.values()
            if other is not lane and other.host.config.mode == "local" and other.status() not in ("stopped", "off")
        )
        if others:
            return lane, f"another desktop container is running ({', '.join(others)})"
        busy = [other.name for other in self.lanes.values() if other.host is lane.host and other is not lane and other.worker.busy()]
        return lane, await lane.host.refusal(lane.sandbox, busy=busy)

    def available(self) -> bool:
        return any(lane.worker.available() for lane in self.lanes.values())

    async def _guest_refusal(self, lane: Lane) -> str | None:
        """Like `_refusal`, but a guest task may stop an owner container that is only idling (no task, no handoff) to
        make room: its logins stay in its profile on disk, and waiting out its idle timer would outlast the queue."""
        reason = await self._refusal(lane)
        if reason is None:
            return None
        freed = False
        for other in list(self.lanes.values()):
            idle = other.status() == "ready" and not other.worker.busy()
            if other is not lane and other.host is lane.host and idle and (stop := getattr(other.sandbox, "stop", None)) is not None:
                try:
                    await stop()
                    freed = True
                except SandboxError as exc:
                    log.warning("could not stop an idle desktop container to make room for a guest task (%s)", exc)
        return await self._refusal(lane) if freed else reason

    async def _refusal(self, lane: Lane) -> str | None:
        if lane.host.config.mode == "local" and lane.name != GUEST and await workspace.check_cap(self.app, fresh=True):
            return workspace.FULL_MESSAGE  # the mounted folder is over its cap: no new desktop work until it is cleared
        if lane.status() == "ready":
            return None  # it already holds its slot
        busy = [other.name for other in self.lanes.values() if other.host is lane.host and other is not lane and other.worker.busy()]
        return await lane.host.refusal(lane.sandbox, busy=busy, memory=lane.host.config.mode == "local")

    async def enqueue_guest(self, brief: str, chat_id: int, title: str, asker: str) -> int:
        """Queue a task that a group or a friend asked for, once the owner allowed it."""
        await self.drop_idle()
        lane = self.guest_lane()
        if lane is None:
            raise ValueError("there is no desktop host for tasks like this")
        try:
            return await lane.worker.enqueue_guest(brief, chat_id, title, asker)
        finally:
            await self.drop_idle()

    async def enqueue(self, chat_id: int, brief: str, container: str | None = None) -> int:
        await self.drop_idle()
        lane = self.lane(container)
        if self.for_guests(lane):
            raise ValueError("that desktop is kept for tasks that groups and friends ask for")
        try:
            return await lane.worker.enqueue(chat_id, brief, admit=lambda: self._refusal(lane))
        finally:
            await self.drop_idle()

    async def _lane_of(self, chat_id: int, task_id: int) -> Lane | None:
        for row in await self.app.store.desktop_tasks(chat_id, active=True):
            if row["id"] == task_id:
                return self.lanes.get(row["container"])
        return None

    async def cancel(self, chat_id: int, task_id: int) -> bool:
        lane = await self._lane_of(chat_id, task_id)
        if lane is None:
            return False
        try:
            return await lane.worker.cancel(chat_id, task_id)
        finally:
            await self.drop_idle()

    async def resume(self, chat_id: int, task_id: int, steps: int) -> bool:
        lane = await self._lane_of(chat_id, task_id)
        return lane is not None and await lane.worker.resume(chat_id, task_id, steps)

    async def extend(self, chat_id: int, task_id: int, token: str, keep_going: bool) -> bool:
        lane = await self._lane_of(chat_id, task_id)
        return lane is not None and await lane.worker.extend(chat_id, task_id, token, keep_going)

    async def recover(self) -> None:
        """After a restart: remove every container the last process left, restart queued tasks, fail the rest."""
        store, chat_id = self.app.store, self.app.owner_id
        rows = await store.desktop_tasks(chat_id, active=True) if chat_id is not None else []
        drop_stale(self.app.home.root, {row["id"] for row in rows if row["status"] == "queued"})
        by_container: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_container[row["container"]].append(row)
        for name in {*self.lanes, *by_container, *await self._leftovers()}:
            try:
                lane = self.lane(name)
            except ValueError as exc:
                for row in by_container.get(name, []):
                    failed = await store.update_desktop_task(row["id"], chat_id, ACTIVE, "failed", error=str(exc))
                    if failed:
                        await self._report(failed)
                continue
            await lane.worker.recover(by_container.get(name, []))
        await self.drop_idle()

    async def _leftovers(self) -> list[str]:
        local = next((lane.sandbox for lane in self.lanes.values() if hasattr(lane.sandbox, "existing_names")), None)
        if local is None:
            return []
        try:
            return await local.existing_names()
        except SandboxError as exc:
            log.warning("could not list desktop containers left by a previous process (%s)", exc)
            return []

    async def _report(self, row: dict) -> None:
        try:
            await self.app.dispatch.report_desktop_task(row)
        except Exception as exc:
            log.warning("could not report desktop task %s (%s)", row["id"], type(exc).__name__)

    async def close(self) -> None:
        lanes = list(self.lanes.values())
        for lane in lanes:
            # A dashboard stop still running in the background finishes before the sandbox is closed under it.
            if lane.stopping is not None and not lane.stopping.done():
                await asyncio.gather(lane.stopping, return_exceptions=True)
        for lane in lanes:
            await lane.worker.close()
        for lane in lanes:
            if (close := getattr(lane.sandbox, "close", None)) is not None:
                await close()

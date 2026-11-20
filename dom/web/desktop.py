"""Owner controls and the same-origin noVNC proxy."""

from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from dom.desktop.client import SandboxError
from dom.desktop.sandbox import LocalSandbox
from dom.telegram import owner
from dom.web.auth import same_origin
from dom.web.pages import owner_app, panel, render

router = APIRouter(prefix="/desktop")
VNC_PATH = "/desktop/vnc/"
VNC_SOCKET = VNC_PATH + "websockify"
_FRAME_LIMIT = 16 * 1024 * 1024


def _query(name: str, default: str) -> str:
    """The query string that selects a container on the page; the default container needs none."""
    return "" if name == default else "?" + urlencode({"container": name})


def _truncate(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _memory_label(mb: int) -> str:
    return f"{mb // 1024} GB" if mb >= 1024 and mb % 1024 == 0 else f"{mb:,} MB"


def _display_state(status: str, task: dict | None, error: str | None) -> tuple[str, str]:
    """Map raw sandbox status, active task and error to the tile's state label and visual tone."""
    if task is not None and task.get("status") == "waiting_owner":
        return "waiting for you", "waiting"
    if status == "starting":
        return "starting", "starting"
    if status == "ready":
        return "running", "running"
    if status == "stopped" and error:
        return "refused", "refused"
    if status == "error":
        return "error", "error"
    return "off", "off"


def _lane(app, name: str | None):
    """The container a request names (the default one without a name), or None when the desktop is off."""
    fleet = app.desktop_worker
    if fleet is None:
        return None
    lane = fleet.lanes.get(name or fleet.default)
    if lane is None:
        raise HTTPException(404, "No such container.")
    return lane


async def _context(app, lane, message: str = "") -> dict:
    if lane is None:
        return {
            "enabled": False,
            "status": "off", "task": None, "handoff_host": None, "local": False, "message": message, "error": None,
            "container": None, "q": "", "containers": [], "hosts": [],
        }
    fleet = app.desktop_worker
    sandbox, status = lane.sandbox, lane.status()
    tasks = {task["container"]: task for task in await app.store.desktop_tasks(app.owner_id, active=True)}
    shown = list(fleet.containers())
    for other in fleet.lanes.values():
        if other not in shown and (
            other is lane
            or other.name in tasks
            or (other.status() in ("error", "stopped") and getattr(other.sandbox, "error", None))
        ):
            shown.append(other)
    containers = []
    for other in shown:
        other_status = other.status()
        other_task = tasks.get(other.name)
        other_error = getattr(other.sandbox, "error", None) if other_status in ("error", "stopped") else None
        state_label, state_tone = _display_state(other_status, other_task, other_error)
        brief = _truncate(other_task["brief"]) if other_task and other_task.get("brief") else ""
        containers.append({
            "name": other.name,
            "host": other.host.config.name,
            "status": other_status,
            "state": state_label,
            "tone": state_tone,
            "task": other_task,
            "brief": brief,
            "error": other_error,
            "detail": brief or (_truncate(other_error) if other_error else ""),
            "selected": other is lane,
            "q": _query(other.name, fleet.default),
        })
    hosts = [{
        "name": host.config.name,
        "mode": host.config.mode,
        "running": sum(1 for item in fleet.lanes.values() if item.host is host and item.status() in ("starting", "ready")),
        "max_containers": host.config.max_containers,
        "memory_floor_mb": host.config.memory_floor_mb,
        "memory_floor": _memory_label(host.config.memory_floor_mb) if host.config.memory_floor_mb else "",
    } for host in fleet.hosts]
    return {
        "enabled": True,
        "status": status,
        "task": tasks.get(lane.name),
        "handoff_host": lane.worker.handoff_host,
        "local": isinstance(sandbox, LocalSandbox),
        # A refused start leaves the container stopped with the reason in `error`.
        "error": getattr(sandbox, "error", None) if status in ("error", "stopped") else None,
        "message": message,
        "container": lane.name,
        "q": _query(lane.name, fleet.default),
        "containers": containers,
        "hosts": hosts,
    }


async def desktop_context(app, container: str = "", message: str = "") -> dict:
    """Compute the desktop state shared by the Desktop page, its state partial and the home tile."""
    return await _context(app, _lane(app, container), message)


@router.get("")
async def desktop(request: Request, container: str = ""):
    app = owner_app(request)
    return panel(request, "desktop", **await desktop_context(app, container))


@router.get("/state")
async def desktop_state(request: Request, container: str = ""):
    app = owner_app(request)
    return render("desktop_state.html", context=await desktop_context(app, container))


@router.get("/tile")
async def desktop_tile(request: Request):
    app = owner_app(request)
    return render("desktop_tile.html", context=await desktop_context(app))


async def _change_computer(app, sandbox, action: str) -> str:
    try:
        await (sandbox.ensure_running() if action == "start" else sandbox.stop())
    except SandboxError:
        await owner.record_event(app, f"Could not {action} the desktop from the dashboard")
        return f"Couldn't {action} the computer."
    await owner.record_event(app, f"Desktop {action} requested from the dashboard")
    return "Computer ready." if action == "start" else "Computer stopped."


@router.post("/computer/{action}")
async def computer_action(request: Request, action: Literal["start", "stop"], container: str = ""):
    app = owner_app(request)
    lane = _lane(app, container)
    if lane is None:
        raise HTTPException(409, "The desktop is off.")
    sandbox, worker = lane.sandbox, lane.worker
    if action == "start":
        # Booting takes a while; the worker's lock must stay free for cancel and resume meanwhile.
        message = await _change_computer(app, sandbox, action)
    else:
        if not isinstance(sandbox, LocalSandbox):
            raise HTTPException(409, "Manage the remote computer on its host.")
        async with worker.control:
            if await app.store.desktop_tasks(app.owner_id, active=True, container=lane.name):
                message = "Cancel the current task before stopping the computer."
            else:
                message = await _change_computer(app, sandbox, action)
    return render("desktop_state.html", context=await _context(app, lane, message))


@router.post("/tasks/{task_id:int}/{steps:int}/done")
async def desktop_done(request: Request, task_id: int, steps: int):
    app = owner_app(request)
    if app.desktop_worker is None:
        raise HTTPException(409, "The desktop is off.")
    resumed = await app.desktop_worker.resume(app.owner_id, task_id, steps)
    message = "Task resumed." if resumed else "The handoff has already ended."
    # The task id says which container; show that one.
    task = next((row for row in await app.store.desktop_tasks(app.owner_id) if row["id"] == task_id), None)
    lane = app.desktop_worker.lanes.get(task["container"]) if task is not None else None
    return render("desktop_state.html", context=await _context(app, lane or _lane(app, ""), message))


@router.get("/vnc/index.html")
async def vnc_viewer(request: Request, container: str = ""):
    # The page and its noVNC scripts are Dom's own files; only the websocket reaches the sandbox.
    lane = _lane(owner_app(request), container)
    return render("desktop_viewer.html", container=lane.name if lane is not None else "")


class NoRedirectConnection(connect):
    def process_redirect(self, exc):
        # Upstream authorization is only for the configured sandbox.
        return exc


connect_upstream = NoRedirectConnection


async def _from_browser(websocket: WebSocket, upstream, sandbox) -> None:
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            return
        sandbox.touch()
        data = frame.get("bytes")
        await upstream.send(data if data is not None else frame["text"])


async def _from_upstream(websocket: WebSocket, upstream, sandbox) -> None:
    async for frame in upstream:
        sandbox.touch()
        if isinstance(frame, bytes):
            await websocket.send_bytes(frame)
        else:
            await websocket.send_text(frame)


@router.websocket("/vnc/websockify")
async def vnc_websocket(websocket: WebSocket):
    app = websocket.app.state.dom
    identity = await websocket.app.state.auth.authenticate(websocket)
    if (
        app.owner_id is None or identity is None or len(websocket.headers.getlist("Origin")) != 1
        or not same_origin(websocket, identity)
    ):
        await websocket.close(code=1008)
        return
    fleet = app.desktop_worker
    lane = fleet.lanes.get(websocket.query_params.get("container") or fleet.default) if fleet is not None else None
    if lane is None:
        await websocket.close(code=1008)
        return
    sandbox = lane.sandbox
    if sandbox.status() != "ready" or sandbox.client is None:
        await websocket.close(code=1013)
        return
    client = sandbox.client
    url = httpx.URL(client.base_url + "/websockify")
    upstream_url = url.copy_with(scheme="wss" if url.scheme == "https" else "ws")
    subprotocols = [name for name in websocket.scope["subprotocols"] if name in ("binary", "base64")]
    sandbox.touch()
    code = 1000
    try:
        async with connect_upstream(
            str(upstream_url), additional_headers=client.auth_headers(), subprotocols=subprotocols or None,
            proxy=None, max_size=_FRAME_LIMIT, open_timeout=10, close_timeout=2,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            relays = {
                asyncio.create_task(_from_browser(websocket, upstream, sandbox)),
                asyncio.create_task(_from_upstream(websocket, upstream, sandbox)),
            }
            try:
                finished, _ = await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            finally:
                for task in relays:
                    task.cancel()
                await asyncio.gather(*relays, return_exceptions=True)
    except (OSError, RuntimeError, TimeoutError, WebSocketException):
        code = 1011
    except WebSocketDisconnect:
        return
    try:
        await websocket.close(code=code)
    except (OSError, RuntimeError, WebSocketDisconnect):
        pass

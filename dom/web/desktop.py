"""Owner controls and the same-origin noVNC proxy."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from dom.desktop.client import DEFAULT_RESOLUTION, PHONE_RESOLUTION, SandboxError, url_host
from dom.desktop.sandbox import LocalSandbox
from dom.desktop.worker import ACTIVE as DESKTOP_ACTIVE, ScreenBusy
from dom.telegram import owner
from dom.web.auth import same_origin
from dom.web.model_settings import is_json
from dom.web.shared import _bounded_stream, owner_app

log = logging.getLogger(__name__)
router = APIRouter(prefix="/desktop")
templates = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=select_autoescape(("html",)),
    trim_blocks=True, lstrip_blocks=True,
)
VNC_PATH = "/desktop/vnc/"
VNC_SOCKET = VNC_PATH + "websockify"
VIEWER_JSON_POSTS = frozenset({"/desktop/viewer/screen", "/desktop/viewer/done"})
_FRAME_LIMIT = 16 * 1024 * 1024
_JSON_BODY_LIMIT = 1024


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
    if status in ("starting", "stopping"):
        return status, "starting"
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


async def desktop_history_context(app, message: str = "") -> dict:
    tz = ZoneInfo(app.config.timezone)

    def local(stamp: str | None) -> str:
        return f"{datetime.fromisoformat(stamp).astimezone(tz):%a %d %b %H:%M}" if stamp else ""

    rows = [
        {
            **row,
            "finished_local": local(row.get("finished_at") or row.get("created_at")),
        }
        for row in await app.store.desktop_tasks(app.owner_id, limit=20)
        if row["status"] not in DESKTOP_ACTIVE
    ]
    return {"rows": rows, "message": message}


async def _context(app, lane, message: str = "") -> dict:
    history = await desktop_history_context(app)
    if lane is None:
        return {
            "enabled": False,
            "status": "off", "task": None, "handoff_host": None, "local": False, "message": message, "error": None,
            "container": None, "q": "", "containers": [], "hosts": [], "history": history,
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
        "running": sum(1 for item in fleet.lanes.values() if item.host is host and item.status() in ("starting", "ready", "stopping")),
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
        "history": history,
    }










async def _change_computer(app, sandbox, action: str) -> str:
    try:
        await (sandbox.ensure_running() if action == "start" else sandbox.stop())
    except SandboxError:
        await owner.record_event(app, f"Could not {action} the desktop from the dashboard")
        return f"Couldn't {action} the computer."
    await owner.record_event(app, f"Desktop {action} requested from the dashboard")
    return "Computer ready." if action == "start" else "Computer stopped."


async def run_computer_action(app, lane, action: str) -> str:
    """Start or stop a lane's computer from the dashboard and say what happened.

    A stop answers at once and runs in the background (`lane.stopping`, shown as "stopping"): `docker stop` waits for
    the container to exit, and the dashboard must not sit on that request meanwhile."""
    sandbox, worker = lane.sandbox, lane.worker
    if action == "start":
        if lane.status() == "stopping":
            return "The computer is still stopping."
        # Booting takes a while; the worker's lock must stay free for cancel and resume meanwhile.
        return await _change_computer(app, sandbox, action)
    if not isinstance(sandbox, LocalSandbox):
        raise HTTPException(409, "Manage the remote computer on its host.")
    async with worker.control:
        # Checked and claimed under the lock with no await in between, so two stops (a double tap, two tabs) start
        # one background stop, and a stop never lands on a computer that is still booting.
        if await app.store.desktop_tasks(app.owner_id, active=True, container=lane.name):
            return "Cancel the current task before stopping the computer."
        if lane.status() == "stopping":
            return "Stopping the computer."
        if sandbox.status() == "starting":
            return "The computer is still starting."
        lane.stopping = asyncio.create_task(_stop_computer(app, lane))
    return "Stopping the computer."


async def _stop_computer(app, lane) -> None:
    try:
        async with lane.worker.control:
            # A task may have been queued between the request's check and now; it wins.
            if await app.store.desktop_tasks(app.owner_id, active=True, container=lane.name):
                await owner.record_event(app, "Desktop stop skipped: a task started")
                return
            await _change_computer(app, lane.sandbox, "stop")
            if lane.sandbox.status() == "stopped":
                lane.worker.screen_reset()
    except Exception:
        # Nobody awaits this task; log here, or the failure is only seen at garbage collection.
        log.exception("background desktop stop failed")


async def resume_handoff(app, task_id: int, steps: int) -> tuple[str, object]:
    """Resume a task waiting for the owner. Returns the message and the lane the task runs in."""
    if app.desktop_worker is None:
        raise HTTPException(409, "The desktop is off.")
    resumed = await app.desktop_worker.resume(app.owner_id, task_id, steps)
    message = "Task resumed." if resumed else "The handoff has already ended."
    # The task id says which container; show that one.
    task = next((row for row in await app.store.desktop_tasks(app.owner_id) if row["id"] == task_id), None)
    lane = app.desktop_worker.lanes.get(task["container"]) if task is not None else None
    return message, lane or _lane(app, "")




async def _json_body(request: Request, max_bytes: int = _JSON_BODY_LIMIT) -> dict:
    if not is_json(request):
        raise HTTPException(415, "Expected application/json.")
    data = bytearray()
    async for chunk in _bounded_stream(request, max_bytes):
        data.extend(chunk)
    try:
        parsed = json.loads(data)
    except ValueError:
        raise HTTPException(400, "Invalid JSON.") from None
    if not isinstance(parsed, dict):
        raise HTTPException(422, "Expected a JSON object.")
    return parsed


async def _viewer_status_payload(lane) -> dict:
    if lane is None:
        return {
            "enabled": False,
            "status": "off",
            "container": "",
            "resolution": DEFAULT_RESOLUTION,
            "phone_mode": False,
            "can_resize": False,
            "live_host": None,
            "handoff": None,
        }
    status = lane.status()
    worker = lane.worker
    handoff = worker.handoff_state()
    live_host = handoff["host"] if handoff else None
    client = getattr(lane.sandbox, "client", None)
    if status == "ready" and client is not None:
        read_url = getattr(client, "current_url", None)
        if read_url is not None:
            try:
                async with asyncio.timeout(3):
                    current = url_host(await read_url())
                if current:
                    live_host = current
            except Exception:
                pass
    resolution = worker.screen_resolution if status == "ready" else DEFAULT_RESOLUTION
    return {
        "enabled": True,
        "status": status,
        "container": lane.name,
        "resolution": resolution,
        "phone_mode": resolution == PHONE_RESOLUTION,
        "can_resize": worker.can_resize_screen(),
        "live_host": live_host,
        "handoff": handoff,
    }


@router.get("/vnc/index.html")
async def vnc_viewer(request: Request, container: str = ""):
    # The page and its noVNC scripts are Dom's own files; only the websocket reaches the sandbox.
    app = owner_app(request)
    lane = _lane(app, container)
    fleet = app.desktop_worker
    q = _query(lane.name, fleet.default) if lane is not None and fleet is not None else ""
    back_url = "/app/work" + q
    status = lane.status() if lane is not None else "off"
    handoff = lane.worker.handoff_state() if lane is not None else None
    resolution = lane.worker.screen_resolution if lane is not None and status == "ready" else DEFAULT_RESOLUTION
    return HTMLResponse(templates.get_template("desktop_viewer.html").render(
        container=lane.name if lane is not None else "",
        q=q,
        back_url=back_url,
        status=status,
        resolution=resolution,
        phone_mode=resolution == PHONE_RESOLUTION,
        can_resize=lane.worker.can_resize_screen() if lane is not None else False,
        handoff=handoff,
    ))


@router.get("/viewer/status")
async def viewer_status(request: Request, container: str = ""):
    lane = _lane(owner_app(request), container)
    return JSONResponse(await _viewer_status_payload(lane))


@router.get("/viewer/password-focus")
async def viewer_password_focus(request: Request, container: str = ""):
    """Report strictly whether the sandbox browser currently focuses input[type=password]."""
    lane = _lane(owner_app(request), container)
    focused = False
    if lane is not None and lane.status() == "ready":
        client = getattr(lane.sandbox, "client", None)
        checker = getattr(client, "is_password_focused", None)
        if checker is not None:
            try:
                async with asyncio.timeout(3):
                    focused = (await checker(timeout=3)) is True
            except Exception:
                focused = False
    return JSONResponse({"password": focused is True})


@router.post("/viewer/screen")
async def viewer_screen(request: Request, container: str = ""):
    """Toggle between phone mode (800x600) and desktop (1280x1024) via the sandbox API."""
    lane = _lane(owner_app(request), container)
    if lane is None:
        raise HTTPException(409, "The desktop is off.")
    body = await _json_body(request)
    if set(body) != {"mode"} or body["mode"] not in ("phone", "desktop"):
        raise HTTPException(422, "Choose phone or desktop mode.")
    if not lane.worker.can_resize_screen():
        raise HTTPException(409, "Screen size can only change while idle or during a handoff.")
    try:
        resolution = await lane.worker.set_screen_mode(body["mode"] == "phone")
    except ScreenBusy:
        raise HTTPException(409, "Screen size can only change while idle or during a handoff.") from None
    except (SandboxError, TimeoutError, ValueError):
        raise HTTPException(502, "Could not resize the remote screen.") from None
    return JSONResponse({
        "ok": True,
        "mode": body["mode"],
        "resolution": resolution,
        "phone_mode": resolution == PHONE_RESOLUTION,
    })


@router.post("/viewer/done")
async def viewer_done(request: Request):
    """Resume a waiting handoff from the full-page viewer."""
    app = owner_app(request)
    if app.desktop_worker is None:
        raise HTTPException(409, "The desktop is off.")
    body = await _json_body(request)
    task_id = body.get("task_id")
    steps = body.get("steps")
    if set(body) != {"task_id", "steps"} or type(task_id) is not int or not 0 < task_id < 2**63 or type(steps) is not int or steps < 0:
        raise HTTPException(422, "Invalid handoff confirmation.")
    resumed = await app.desktop_worker.resume(app.owner_id, task_id, steps)
    return JSONResponse({
        "ok": resumed,
        "message": "Task resumed." if resumed else "The handoff has already ended.",
    })


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

"""Owner controls and the same-origin noVNC proxy."""

from __future__ import annotations

import asyncio
from typing import Literal

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


async def _context(app, message: str = "") -> dict:
    sandbox = app.sandbox
    status = sandbox.status() if sandbox is not None else "off"
    tasks = await app.store.desktop_tasks(app.owner_id, active=True)
    return {
        "status": status,
        "task": tasks[0] if tasks else None,
        "handoff_host": app.desktop_worker.handoff_host if app.desktop_worker is not None else None,
        "local": isinstance(sandbox, LocalSandbox),
        "message": message,
        "error": sandbox.error if status == "error" else None,
    }


@router.get("")
async def desktop(request: Request):
    return panel(request, "desktop", **await _context(owner_app(request)))


@router.get("/state")
async def desktop_state(request: Request):
    return render("desktop_state.html", context=await _context(owner_app(request)))


async def _change_computer(app, sandbox, action: str) -> str:
    try:
        await (sandbox.ensure_running() if action == "start" else sandbox.stop())
    except SandboxError:
        await owner.record_event(app, f"Could not {action} the desktop from the dashboard")
        return f"Couldn't {action} the computer."
    await owner.record_event(app, f"Desktop {action} requested from the dashboard")
    return "Computer ready." if action == "start" else "Computer stopped."


@router.post("/computer/{action}")
async def computer_action(request: Request, action: Literal["start", "stop"]):
    app = owner_app(request)
    sandbox, worker = app.sandbox, app.desktop_worker
    if sandbox is None or worker is None:
        raise HTTPException(409, "The desktop is off.")
    if action == "start":
        # Booting takes a while; the worker's lock must stay free for cancel and resume meanwhile.
        message = await _change_computer(app, sandbox, action)
    else:
        if not isinstance(sandbox, LocalSandbox):
            raise HTTPException(409, "Manage the remote computer on its host.")
        async with worker.control:
            if await app.store.desktop_tasks(app.owner_id, active=True):
                message = "Cancel the current task before stopping the computer."
            else:
                message = await _change_computer(app, sandbox, action)
    return render("desktop_state.html", context=await _context(app, message))


@router.post("/tasks/{task_id:int}/{steps:int}/done")
async def desktop_done(request: Request, task_id: int, steps: int):
    app = owner_app(request)
    if app.desktop_worker is None:
        raise HTTPException(409, "The desktop is off.")
    resumed = await app.desktop_worker.resume(app.owner_id, task_id, steps)
    message = "Task resumed." if resumed else "The handoff has already ended."
    return render("desktop_state.html", context=await _context(app, message))


@router.get("/vnc/index.html")
async def vnc_viewer(request: Request):
    # The page and its noVNC scripts are Dom's own files; only the websocket reaches the sandbox.
    owner_app(request)
    return render("desktop_viewer.html")


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
    sandbox = app.sandbox
    if sandbox is None or sandbox.status() != "ready" or sandbox.client is None:
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
    except (OSError, TimeoutError, WebSocketException):
        code = 1011
    except WebSocketDisconnect:
        return
    try:
        await websocket.close(code=code)
    except (OSError, RuntimeError, WebSocketDisconnect):
        pass

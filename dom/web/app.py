"""The authenticated owner dashboard and its in-process Uvicorn server."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from dom.web.api import router as api_router
from dom.web.api_access import JSON_POSTS as ACCESS_POSTS, router as access_router
from dom.web.api_chats import JSON_POSTS as CHAT_POSTS, router as chats_router
from dom.web.api_google import JSON_POSTS as GOOGLE_POSTS, router as google_router
from dom.web.api_goals import JSON_POSTS as GOALS_POSTS, router as goals_router
from dom.web.api_media import GUARDED_PREFIX as MEDIA_GET_PREFIX, JSON_POSTS as MEDIA_POSTS, router as media_router
from dom.web.api_world import GUARDED_PREFIX as WORLD_GET_PREFIX, JSON_POSTS as WORLD_POSTS, router as world_router
from dom.web.api_memory import GUARDED_GETS as MEMORY_GETS, JSON_POSTS as MEMORY_POSTS, router as memory_router
from dom.web.api_noticings import JSON_POSTS as NOTICINGS_POSTS, router as noticings_router
from dom.web.api_search import GUARDED_GETS as SEARCH_GETS, router as search_router
from dom.web.api_system import JSON_POSTS as SYSTEM_POSTS, router as system_router
from dom.web.api_talk import (
    GUARDED_GETS as TALK_GETS, GUARDED_PREFIX as TRACE_GET_PREFIX, JSON_POSTS as TALK_POSTS, router as talk_router,
)
from dom.web.api_waiting import JSON_POSTS as WAITING_POSTS, router as waiting_router
from dom.web.api_work import JSON_POSTS as WORK_POSTS, router as work_router
from dom.web.auth import Auth, fetch_site_ok, request_origin, same_origin
from dom.web.desktop import VIEWER_JSON_POSTS, VNC_PATH, VNC_SOCKET, router as desktop_router
from dom.web.model_settings import JSON_POSTS, is_json, router as model_settings_router
from dom.web.provider_settings import JSON_POSTS as PROVIDER_POSTS, router as provider_settings_router
from dom.web.services_settings import JSON_POSTS as SERVICES_POSTS, router as services_settings_router
from dom.web.spend_settings import JSON_POSTS as SPEND_POSTS, router as spend_settings_router
from dom.web.studio_settings import JSON_POSTS as STUDIO_POSTS, router as studio_settings_router
from dom.web.redirects import router as redirects_router

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.web")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
VNC_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; frame-src 'none'; worker-src 'none'; "
        "img-src 'self' data: blob:; media-src 'self' data: blob:"
    ),
    "X-Frame-Options": "SAMEORIGIN",
}


def create_app(dom: App, auth: Auth) -> FastAPI:
    web = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)

    @web.middleware("http")
    async def protect(request: Request, call_next):
        identity = await auth.authenticate(request)
        if identity is None:
            response = PlainTextResponse("Forbidden", status_code=403)
        elif (
            request.url.path in MEMORY_GETS | SEARCH_GETS | TALK_GETS
            or request.url.path.startswith((MEDIA_GET_PREFIX, TRACE_GET_PREFIX, WORLD_GET_PREFIX))
        ) and not fetch_site_ok(request, identity):
            response = PlainTextResponse("Forbidden", status_code=403)
        elif request.method not in ("GET", "HEAD") and not (
            request.method == "POST"
            and request.url.path in (
                JSON_POSTS | PROVIDER_POSTS | STUDIO_POSTS | SPEND_POSTS | SERVICES_POSTS | VIEWER_JSON_POSTS | CHAT_POSTS
                | MEMORY_POSTS | WORK_POSTS | WAITING_POSTS | SYSTEM_POSTS | GOALS_POSTS | NOTICINGS_POSTS | MEDIA_POSTS
                | WORLD_POSTS | TALK_POSTS | ACCESS_POSTS | GOOGLE_POSTS
            )
            and is_json(request) and "Origin" in request.headers
            and same_origin(request, identity)
        ):
            response = PlainTextResponse("Forbidden", status_code=403)
        else:
            response = await call_next(request)
        if identity and request.url.path.startswith(VNC_PATH) and response.status_code < 300:
            response.headers.update(VNC_HEADERS)
            origin = request_origin(request, identity)
            if origin is not None:
                scheme, host, port = origin
                if re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
                    authority = f"[{host}]" if ":" in host else host
                    socket_scheme = "wss" if scheme == "https" else "ws"
                    response.headers["Content-Security-Policy"] += (
                        f"; connect-src 'self' {socket_scheme}://{authority}:{port}{VNC_SOCKET}"
                    )
        else:
            response.headers.update(SECURITY_HEADERS)
        return response

    @web.exception_handler(Exception)
    async def server_error(request: Request, error: Exception):
        return PlainTextResponse("Internal server error", status_code=500, headers=SECURITY_HEADERS)

    web.state.dom = dom
    web.state.auth = auth
    web.include_router(redirects_router)
    web.include_router(api_router)
    web.include_router(access_router)
    web.include_router(google_router)
    web.include_router(chats_router)
    web.include_router(memory_router)
    web.include_router(media_router)
    web.include_router(world_router)
    web.include_router(search_router)
    web.include_router(work_router)
    web.include_router(waiting_router)
    web.include_router(goals_router)
    web.include_router(noticings_router)
    web.include_router(system_router)
    web.include_router(talk_router)
    web.include_router(desktop_router)
    web.include_router(model_settings_router)
    web.include_router(provider_settings_router)
    web.include_router(studio_settings_router)
    web.include_router(spend_settings_router)
    web.include_router(services_settings_router)
    web.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

    return web


class DashboardServer(uvicorn.Server):
    # Telegram polling owns SIGINT and SIGTERM in the shared process.
    def capture_signals(self):
        return nullcontext()


async def _serve(server: DashboardServer, listener) -> None:
    try:
        await server.serve(sockets=[listener])
    except SystemExit as exit_:  # uvicorn calls sys.exit when it fails to start; that must not stop Dom
        raise RuntimeError(f"uvicorn exited with status {exit_.code}") from None


def _report_exit(server: DashboardServer, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    if (error := task.exception()) is not None:
        log.error("Dashboard stopped: %s: %s. Dom keeps running without it.", type(error).__name__, error)
    elif not server.should_exit:
        log.error("Dashboard stopped unexpectedly. Dom keeps running without it.")


@asynccontextmanager
async def serving(dom: App, auth: Auth | None):
    if auth is None:
        yield
        return
    settings = dom.config.web
    if not settings.bind.strip():
        raise ValueError("web.bind must name a listening address.")
    server = DashboardServer(uvicorn.Config(
        create_app(dom, auth), host=settings.bind, port=settings.port,
        proxy_headers=False, access_log=False, timeout_graceful_shutdown=10,
        server_header=False, date_header=False,
    ))
    try:
        bound = server.config.bind_socket()  # uvicorn exits the process on failure
    except (OSError, SystemExit) as error:
        log.error(
            "Dashboard not started: cannot listen on %s:%s (%s). Dom keeps running without it.",
            settings.bind, settings.port, error,
        )
        yield
        return
    with bound as listener:
        task = asyncio.create_task(_serve(server, listener))
        task.add_done_callback(partial(_report_exit, server))
        try:
            yield
        finally:
            server.should_exit = True
            await asyncio.wait({task})

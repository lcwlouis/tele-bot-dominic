"""The authenticated owner dashboard and its in-process Uvicorn server."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from dom.web.auth import Auth, same_origin
from dom.web.pages import router

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


def create_app(dom: App, auth: Auth) -> FastAPI:
    web = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)

    @web.middleware("http")
    async def protect(request: Request, call_next):
        identity = await auth.authenticate(request)
        if identity is None:
            response = PlainTextResponse("Forbidden", status_code=403)
        elif request.method not in ("GET", "HEAD") and not (
            request.method == "POST" and request.headers.get("HX-Request") == "true"
            and same_origin(request, identity)
        ):
            response = PlainTextResponse("Forbidden", status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @web.exception_handler(Exception)
    async def server_error(request: Request, error: Exception):
        return PlainTextResponse("Internal server error", status_code=500, headers=SECURITY_HEADERS)

    web.state.dom = dom
    web.include_router(router)
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

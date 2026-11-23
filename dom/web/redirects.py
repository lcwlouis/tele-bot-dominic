"""The old server-rendered dashboard's addresses, kept so old bookmarks and old Telegram messages still land.

Each one answers a GET with a 307 to a fixed /app path. Only a plugin's name and hash come from the request, and
they are checked the way the API checks them before they reach the path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from dom.plugins.manifest import validate_name
from dom.plugins.registry import PLUGIN_HASH

router = APIRouter()

MOVED = {
    "/": "/app",
    "/chats": "/app/chats",
    "/memory": "/app/memory",
    "/desktop": "/app/work",
    "/own-work": "/app/work/own-work",
    "/reminders": "/app/work/reminders",
    "/briefs": "/app/work/briefs",
    "/settings": "/app/system",
    "/plugins": "/app/system/plugins",
    "/usage": "/app/system/usage",
    "/turns": "/app/system/turns",
    "/proposals": "/app/waiting",
}


def _mover(target: str):
    async def move() -> RedirectResponse:
        return RedirectResponse(target, status_code=307)
    return move


for old, new in MOVED.items():
    router.add_api_route(old, _mover(new), methods=["GET", "HEAD"], include_in_schema=False)


@router.api_route("/plugins/{name}/{digest}", methods=["GET", "HEAD"], include_in_schema=False)
async def moved_plugin_review(name: str, digest: str) -> RedirectResponse:
    try:
        validate_name(name)
    except ValueError:
        raise HTTPException(404) from None
    if not PLUGIN_HASH.fullmatch(digest):
        raise HTTPException(404)
    return RedirectResponse(f"/app/system/plugins/{name}/{digest}", status_code=307)

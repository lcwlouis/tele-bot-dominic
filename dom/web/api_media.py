"""JSON API for what Dom has sent and what his models take: the kept media files (for the Library page), their disk
use, what each chat model accepts as input, and a re-run of the one-off image probe.

Everything here is owner-only like the rest of the dashboard. A file is read through its own message's chat id, so
a path can never reach another chat's folder. Reads are for same-site browsers only (fetch_site_ok).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from dom.media_files import MIME
from dom.web.model_settings import _input
from dom.web.shared import _title, owner_app

log = logging.getLogger("dom.web")
router = APIRouter()
JSON_POSTS = {"/api/media/inputs/probe"}
GUARDED_PREFIX = "/api/media/"  # reads under it are same-site only
PAGE = 50
MAX_FETCH_BYTES = 20_000_000  # Telegram's own limit for a bot's file download


class Nothing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _item(row: dict, titles: dict[str, str]) -> dict[str, Any]:
    media = row["media"]
    chat_id = row["chat_id"]
    return {
        "chat_id": int(chat_id), "chat": titles.get(chat_id, chat_id), "msg_id": row["msg_id"], "kind": media.get("kind"),
        "special": bool(media.get("special")), "text": media.get("scene") or media.get("text") or "",
        "at": row["created_at"], "bytes": media.get("bytes"), "stored": bool(media.get("file")),
        "url": f"/api/media/files/{chat_id}/{row['msg_id']}",
    }


@router.get("/api/media/files")
async def list_files(
    request: Request, chat_id: int | None = None, before: str | None = Query(None, max_length=80),
) -> dict:
    """Newest first, every chat or one. Messages whose file was removed past the cap are listed too (`stored` is
    false): they can still be fetched from Telegram. `before` is the `next` of the previous page."""
    app = owner_app(request)
    cursor = None
    if before:
        stamp, _, row_id = before.rpartition("|")
        try:
            datetime.fromisoformat(stamp)
            cursor = (stamp, int(row_id))
        except ValueError:
            raise HTTPException(422, "Unknown page.") from None
    rows = await app.store.media_library(chat_id=chat_id, limit=PAGE + 1, before=cursor)
    titles = {row["chat_id"]: _title(row, app.owner_id) for row in await app.store.list_approved_chats()}
    page = rows[:PAGE]
    return {
        "files": [_item(row, titles) for row in page],
        "next": f"{page[-1]['created_at']}|{page[-1]['id']}" if len(rows) > PAGE else None,
    }


@router.get("/api/media/files/{chat_id}/{msg_id}")
async def get_file(request: Request, chat_id: int, msg_id: int) -> Response:
    app = owner_app(request)
    row = await app.store.get_message(chat_id, msg_id)
    media = _media(row)
    if row is None or row.role != "assistant" or not (media.get("file") or media.get("file_id")):
        raise HTTPException(404, "No such file.")
    data = await app.media_files.read(chat_id, str(media["file"])) if media.get("file") else None
    mime = MIME.get(str(media.get("file", "")).rpartition(".")[2])
    if data is None and media.get("file_id"):  # removed past the cap: Telegram still has it
        try:
            downloaded = await app.bot.download(media["file_id"])
            with downloaded:
                data = downloaded.read(MAX_FETCH_BYTES + 1)
        except Exception as exc:
            log.warning("could not fetch a kept file from Telegram (%s)", type(exc).__name__)
            data = None
        if data is not None and len(data) > MAX_FETCH_BYTES:
            data = None
        mime = {"photo": "image/jpeg", "voice": "audio/ogg", "video_note": "video/mp4"}.get(media.get("kind"))
    if data is None or mime is None:
        raise HTTPException(404, "The file is no longer available.")
    return Response(data, media_type=mime, headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})


def _media(row) -> dict:
    try:
        value = json.loads(row.media_json) if row is not None and row.media_json else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@router.get("/api/media/usage")
async def usage(request: Request) -> dict:
    return await owner_app(request).media_files.usage()


@router.get("/api/media/inputs")
async def inputs(request: Request) -> dict:
    """What each chat provider takes as input, and where that was learned (config, model list, probe or unknown)."""
    app = owner_app(request)
    native = app.dispatch.native
    return {
        "providers": [await native.modalities.explain(provider.endpoint) for provider in native._chat_providers()],
        "route_accepts": sorted(await native.accepted()),
    }


@router.post("/api/media/inputs/probe")
async def probe(request: Request) -> dict:
    """Test the chat providers again with a generated image (those without an `input` setting), and report."""
    app = owner_app(request)
    await _input(request, Nothing)
    results = await app.dispatch.native.reprobe()
    return {"providers": results, "route_accepts": sorted(await app.dispatch.native.accepted())}

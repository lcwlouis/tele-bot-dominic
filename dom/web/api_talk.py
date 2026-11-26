"""JSON API for the Talk page (the owner DM, written to from the dashboard) and the debug traces it shows.

`GET /api/talk` is the page's poll: the newest messages on the first call, then only those after the cursor, plus
whether Dom is composing. `POST /api/talk/send` sends one message through dom.talk. The trace reads (`/api/traces/...`)
serve what dom.tracer stored for any chat: the dashboard is the owner's, and E102 makes traces full everywhere. Every
read here is same-site only (GUARDED_GETS, GUARDED_PREFIX), and the POST sits in JSON_POSTS.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from dom import talk
from dom.web.shared import owner_app

router = APIRouter()
log = logging.getLogger("dom.web")

GUARDED_GETS = {"/api/talk"}
GUARDED_PREFIX = "/api/traces/"  # reads under it are same-site only: they hold full prompts and replies
JSON_POSTS = {"/api/talk/send"}
FIRST_PAGE = 40
MEDIA_KINDS = ("photo", "voice", "video_note", "special")


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field("", max_length=talk.MAX_TEXT + 100)
    photo: str | None = Field(None, max_length=11_000_000)  # base64, at most 8 MB decoded
    mime: str | None = Field(None, max_length=40)


def _media(row) -> dict | None:
    try:
        media = json.loads(row.media_json) if row.media_json else None
    except ValueError:
        return None
    if not isinstance(media, dict) or not media.get("kind"):
        return None
    kept = row.role == "assistant" and media["kind"] in MEDIA_KINDS and (media.get("file") or media.get("file_id"))
    return {
        "kind": media["kind"],
        "url": f"/api/media/files/{row.chat_id}/{row.msg_id}" if kept and row.msg_id is not None else None,
        "description": media.get("description") or None,
    }


def _message(row) -> dict:
    return {
        "id": row.id, "msg_id": row.msg_id, "mine": row.role == "assistant",
        "who": "Dom" if row.role == "assistant" else "You", "text": row.text, "at": row.created_at,
        "origin": row.origin, "edited": row.is_edited, "media": _media(row),
    }


@router.get("/api/talk")
async def read_talk(
    request: Request,
    after: int | None = Query(None, ge=0),
    before: int | None = Query(None, ge=1),
    trace: bool = False,
) -> dict:
    """The owner DM. No cursor: the newest page. `after`: what came since (the poll). `before`: an older page."""
    app = owner_app(request)
    owner = app.owner_id
    has_more = False
    if after is not None:
        rows = await app.store.get_chat_messages_after(owner, after)
    else:
        newest, cursor = await app.store.get_recent_chat_messages(owner, FIRST_PAGE, before_id=before)
        rows, has_more = list(reversed(newest)), cursor is not None
    data: dict = {
        "messages": [_message(row) for row in rows],
        "has_more": has_more,
        "typing": app.dispatch.turn_running(owner),
        "traced": app.traces is not None,
    }
    if trace and app.traces is not None:
        since = datetime.fromisoformat(rows[0].created_at) - timedelta(minutes=5) if rows else (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        data["turns"] = await app.traces.store.summaries(owner, since)
    return data


@router.post("/api/talk/send")
async def send(request: Request, body: Send) -> dict:
    app = owner_app(request)
    photo = None
    if body.photo:
        try:
            photo = base64.b64decode(body.photo, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "That photo could not be read.") from None
    try:
        msg_id = await talk.send_from_dashboard(app, body.text, photo=photo, mime=body.mime)
    except talk.TalkRefused as refusal:
        raise HTTPException(400, str(refusal)) from None
    except Exception as exc:
        log.warning("could not send the owner's dashboard message (%s)", type(exc).__name__, exc_info=True)
        raise HTTPException(502, "Telegram didn't take the message. Try again in a moment.") from None
    return {"msg_id": msg_id}


def _traces(request: Request):
    app = owner_app(request)
    if app.traces is None:
        raise HTTPException(503, "The tracer is off: Dom could not open its trace store.")
    return app.traces.store


@router.get("/api/traces/turn/{turn_id}")
async def read_turn_trace(request: Request, turn_id: int) -> dict:
    parts = await _traces(request).turn(turn_id)
    if not parts:
        raise HTTPException(404, "No trace for that turn: it is older than the trace retention, or ran before tracing.")
    return {"turn_id": turn_id, "parts": parts}


@router.get("/api/traces/part/{trace_id}")
async def read_trace_part(request: Request, trace_id: int) -> dict:
    part = await _traces(request).part(trace_id)
    if part is None:
        raise HTTPException(404, "That trace is gone.")
    return {"parts": [part]}


def _download(data: object, name: str) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"},
    )


@router.get("/api/traces/turn/{turn_id}/export")
async def export_turn(request: Request, turn_id: int) -> Response:
    parts = await _traces(request).turn(turn_id)
    if not parts:
        raise HTTPException(404, "No trace for that turn.")
    return _download({"turn_id": turn_id, "parts": parts}, f"dom-trace-turn-{turn_id}.json")


def _when(value: str, name: str) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"`{name}` must be an ISO date or time.") from None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@router.get("/api/traces/export")
async def export_range(
    request: Request, chat: str = Query("", max_length=32), start: str = Query("", alias="from", max_length=40),
    end: str = Query("", alias="to", max_length=40),
) -> Response:
    store = _traces(request)
    parts, truncated = await store.export(chat or None, _when(start, "from"), _when(end, "to"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return _download(
        {"chat": chat or None, "from": start or None, "to": end or None, "truncated": truncated, "parts": parts},
        f"dom-traces-{stamp}.json",
    )

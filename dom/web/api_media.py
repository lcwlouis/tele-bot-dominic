"""JSON API for what Dom has sent and what his models take: the kept media files (for the Library page), their disk
use, what each chat model accepts as input, and a re-run of the one-off image probe.

Everything here is owner-only like the rest of the dashboard. A file is read through its own message's chat id, so
a path can never reach another chat's folder. Reads are for same-site browsers only (fetch_site_ok).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from time import monotonic
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dom import provenance
from dom.config import STT_ENV_PATTERN, STT_ENV_RULE, validate_local_stt_url
from dom.media_files import MIME
from dom.stt import SttClient
from dom.web.model_settings import REVISION, _input, _snapshot, save_values
from dom.web.shared import _title, owner_app

log = logging.getLogger("dom.web")
router = APIRouter()
JSON_POSTS = {"/api/media/inputs/probe", "/api/media/stt", "/api/media/stt/test"}
GUARDED_PREFIX = "/api/media/"  # reads under it are same-site only
PAGE = 50
MAX_FETCH_BYTES = 20_000_000  # Telegram's own limit for a bot's file download


class Nothing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _item(row: dict, titles: dict[str, str], costs: dict[int, float]) -> dict[str, Any]:
    media = row["media"]
    chat_id = row["chat_id"]
    return {
        "chat_id": int(chat_id), "chat": titles.get(chat_id, chat_id), "msg_id": row["msg_id"], "kind": media.get("kind"),
        "special": bool(media.get("special")), "text": media.get("scene") or media.get("text") or "",
        "at": row["created_at"], "bytes": media.get("bytes"), "stored": bool(media.get("file")),
        "url": f"/api/media/files/{chat_id}/{row['msg_id']}",
        # a video note keeps the still it was filmed from beside it
        "still_url": f"/api/media/files/{chat_id}/{row['msg_id']}?still=true" if media.get("still_file") else None,
        # where it came from and what it cost, from the usage rows linked when it was made (None for older media)
        "made": provenance.describe(media.get("made"), costs),
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
    costs = await app.store.usage_costs([i for row in page for i in provenance.usage_ids(row["media"].get("made"))])
    return {
        "files": [_item(row, titles, costs) for row in page],
        "next": f"{page[-1]['created_at']}|{page[-1]['id']}" if len(rows) > PAGE else None,
    }


@router.get("/api/media/files/{chat_id}/{msg_id}")
async def get_file(request: Request, chat_id: int, msg_id: int, still: bool = False) -> Response:
    app = owner_app(request)
    row = await app.store.get_message(chat_id, msg_id)
    media = _media(row)
    if still:  # the picture a video note was filmed from, only while it is kept
        data = await app.media_files.read(chat_id, str(media["still_file"])) if media.get("still_file") else None
        if row is None or row.role != "assistant" or data is None:
            raise HTTPException(404, "No such file.")
        return Response(data, media_type="image/png", headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})
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


class _SttFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_url: str = Field(max_length=512)
    model: str = Field("whisper-1", min_length=1, max_length=120)
    language: str = Field("", max_length=16)
    api_key_env: str = Field("", max_length=64)

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_local_stt_url(value)

    @field_validator("model", "language")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, value: str) -> str:
        clean = value.strip()
        if clean and not STT_ENV_PATTERN.fullmatch(clean):
            raise ValueError(STT_ENV_RULE)
        return clean


class SttUpdate(_SttFields):
    revision: str = Field(pattern=REVISION)


class SttTest(_SttFields):
    @field_validator("base_url")
    @classmethod
    def _require_url(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter a server address to test.")
        return value


def _stt_view(snapshot) -> dict:
    stt = snapshot.config.stt
    key_set = bool(snapshot.config.secret(stt.api_key_env)) if stt.api_key_env else False
    return {
        "revision": snapshot.revision,
        "base_url": stt.base_url,
        "model": stt.model,
        "api_key_env": stt.api_key_env,
        "key_set": key_set,
        "language": stt.language,
    }


@router.get("/api/media/stt")
async def read_stt(request: Request) -> dict:
    app = owner_app(request)
    return await asyncio.to_thread(lambda: _stt_view(_snapshot(app.config.home)))


@router.post("/api/media/stt")
async def update_stt(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, SttUpdate)
    changes: dict[tuple[str, ...], bool | int | float | str | None] = {
        ("stt", "base_url"): update.base_url,
        ("stt", "model"): update.model or "whisper-1",
        ("stt", "language"): update.language,
        ("stt", "api_key_env"): update.api_key_env,
    }
    saved = await save_values(
        app, update.revision, changes,
        lambda config: {"stt": config.stt},
        "Could not save local speech-to-text settings.",
        after=app.refresh_stt,
    )
    return await asyncio.to_thread(_stt_view, saved)


@router.post("/api/media/stt/test")
async def test_stt(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SttTest)
    key = await asyncio.to_thread(app.config.secret, body.api_key_env) if body.api_key_env else None
    if body.api_key_env and not key:
        return {"ok": False, "latency_ms": 0, "reply": "", "error": f"{body.api_key_env} is not set in .env."}
    client = SttClient(app._primary_client().http, body.base_url, body.model, key, body.language, timeout=15.0)
    started = monotonic()
    try:
        reply = await client.probe()
        return {"ok": True, "latency_ms": int((monotonic() - started) * 1000), "reply": reply, "error": ""}
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "latency_ms": int((monotonic() - started) * 1000),
            "reply": "",
            "error": f"HTTP {exc.response.status_code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((monotonic() - started) * 1000),
            "reply": "",
            "error": type(exc).__name__,
        }

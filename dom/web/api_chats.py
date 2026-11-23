"""JSON API for the Chats page: every approved chat with its presence, preset and sliders, the everywhere preset
and vibe, and the sticker sets.

Each write runs the same dom.telegram.owner function the old /chats page and the Telegram buttons use, so every
check and owner event is shared. A write answers with its message and the fresh page data. A write that did nothing
(an unknown chat, a preset that is gone) is a 4xx carrying that message. Only counts and titles leave here: no chat's
message text is read.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.telegram import owner
from dom.traits import NAMES
from dom.web.api import _when
from dom.web.model_settings import _input
from dom.web.pages import WHO, _chats, owner_app

router = APIRouter()
JSON_POSTS = {"/api/chats/action", "/api/chats/preset", "/api/chats/traits", "/api/chats/traits/reset", "/api/stickers", "/api/stickers/refresh"}
ChatId = int


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatAction(Strict):
    chat_id: ChatId
    action: Literal["wake", "sleep", "leave"]


class PresetUpdate(Strict):
    chat_id: ChatId | Literal["all"]
    preset: str = Field(max_length=64)  # "" clears it; "auto" is for everywhere only


class TraitsUpdate(Strict):
    chat_id: ChatId
    traits: dict[str, float] = Field(min_length=1, max_length=len(NAMES))


class TraitsReset(Strict):
    chat_id: ChatId


class StickerChange(Strict):
    action: Literal["add", "remove"]
    name: str = Field(min_length=1, max_length=200)


class Nothing(Strict):
    pass


async def _view(app) -> dict:
    data = await _chats(app)
    now = app.dispatch.now()
    tz = app.dispatch.presence.tz
    vibe = data["vibe"]
    return {
        "chats": [
            {
                "id": row["id"], "title": row["title"], "scope": row["scope"], "state": row["state"], "until": row["when"],
                "unread": row["unread"],
                "preset": {"name": row["preset"].name, "title": row["preset"].title} if row["preset"] else None,
                "own_preset": row["own_preset"],
                "task": {"reason": row["task"].reason, "until": row["task_until"]} if row["task"] else None,
                "sliders": [{"name": name, "value": round(value, 2), "custom": custom} for name, value, custom in row["sliders"]],
            }
            for row in data["rows"]
        ],
        "presets": [{"name": item.name, "title": item.title, "description": item.description} for item in data["presets"]],
        "everywhere": data["everywhere"],
        "pending": data["pending"],
        "vibe": {
            "mode": vibe.mode, "title": vibe.preset.title if vibe.preset else "",
            "description": vibe.preset.description if vibe.preset else "", "reason": vibe.reason, "source": vibe.source,
            "since": _when(vibe.since.astimezone(tz), now.astimezone(tz)) if vibe.since else "",
        },
        "stickers": data["stickers"],
    }


@router.get("/api/chats")
async def read_chats(request: Request) -> dict:
    return await _view(owner_app(request))


async def _done(app, message: str) -> dict:
    return {"message": message, **await _view(app)}


@router.post("/api/chats/action")
async def chat_action(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ChatAction)
    if update.action == "leave":
        message, left = await owner.leave_chat(app, update.chat_id, WHO)
        if not left:
            raise HTTPException(409, message[:1].upper() + message[1:])
        return await _done(app, message)
    result = await owner.override_chat(app, update.chat_id, f"/{update.action}", WHO)
    if result is None:
        raise HTTPException(404, "Unknown chat.")
    return await _done(app, result.message)


@router.post("/api/chats/preset")
async def set_preset(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, PresetUpdate)
    if update.chat_id == "all":
        message = await owner.set_global_preset(app, update.preset, WHO)
    else:
        chat = await owner.approved_chat(app, update.chat_id)
        message = await owner.set_preset(app, chat, update.preset) if chat is not None else None
    if message is None:
        raise HTTPException(422, "Preset or chat unavailable.")
    return await _done(app, message)


@router.post("/api/chats/traits")
async def set_traits(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, TraitsUpdate)
    chat = await owner.approved_chat(app, update.chat_id)
    message = await owner.set_traits(app, chat, update.traits) if chat is not None else None
    if message is None:
        raise HTTPException(422, "Traits or chat unavailable.")
    return await _done(app, message)


@router.post("/api/chats/traits/reset")
async def reset_traits(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, TraitsReset)
    chat = await owner.approved_chat(app, update.chat_id)
    message = await owner.reset_traits(app, chat) if chat is not None else None
    if message is None:
        raise HTTPException(422, "Traits or chat unavailable.")
    return await _done(app, message)


@router.post("/api/stickers")
async def change_stickers(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, StickerChange)
    before = app.stickers.names()
    message = await owner.change_sticker_set(app, update.action, update.name)
    return {**await _done(app, message), "changed": app.stickers.names() != before}


@router.post("/api/stickers/refresh")
async def refresh_stickers(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, Nothing)
    return await _done(app, await owner.refresh_sticker_descriptions(app))

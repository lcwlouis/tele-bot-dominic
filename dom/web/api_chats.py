"""JSON API for the Chats page: every approved chat with its presence, preset and sliders, the everywhere preset
and vibe, and the sticker sets.

Each write runs the same dom.telegram.owner function the Telegram buttons use, so every
check and owner event is shared. A write answers with its message and the fresh page data. A write that did nothing
(an unknown chat, a preset that is gone) is a 4xx carrying that message. The chat list carries only counts and titles.
A chat's recent messages are their own read: the owner's DM in full, every other chat one line per message, as Home
shows Dom's replies; the dashboard is not a place to read friends' chats.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.reachability import check_reachability
from dom.telegram import owner
from dom.traits import NAMES
from dom.web.api import _local, _preview, _when
from dom.web.model_settings import _input
from dom.web.shared import WHO, _chats, _title, owner_app
from dom.web.studio_settings import MediaStanding, media_standing

router = APIRouter()
JSON_POSTS = {
    "/api/chats/action", "/api/chats/preset", "/api/chats/traits", "/api/chats/traits/reset",
    "/api/chats/reachability", "/api/chats/hide", "/api/chats/forget", "/api/chats/fresh",
    "/api/stickers", "/api/stickers/refresh",
}
ChatId = int


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # no coercion: true is not a slider value


class ChatAction(Strict):
    chat_id: ChatId
    action: Literal["wake", "sleep", "leave"]


class PresetUpdate(Strict):
    chat_id: ChatId | Literal["all"]
    preset: str = Field(max_length=64)  # "" clears it; "auto" is for everywhere only


class TraitsUpdate(Strict):
    chat_id: ChatId
    traits: dict[str, int | float] = Field(min_length=1, max_length=len(NAMES))


class TraitsReset(Strict):
    chat_id: ChatId


class FreshStart(Strict):
    chat_id: ChatId
    undo: bool = False


class StickerChange(Strict):
    action: Literal["add", "remove"]
    name: str = Field(min_length=1, max_length=200)


class Nothing(Strict):
    pass


class ChatHide(Strict):
    chat_id: ChatId
    hidden: bool = True


class ChatForget(Strict):
    chat_id: ChatId
    delete_history: bool = False
    confirm: bool = False


async def _unreachable_rows(app, *, include_hidden: bool = False) -> list[dict]:
    now = app.dispatch.now()
    tz = app.dispatch.presence.tz
    rows = await app.store.list_unreachable_chats(include_hidden=include_hidden)
    return [
        {
            "id": int(row["chat_id"]),
            "title": _title(row, app.owner_id),
            "scope": app.store.scope_for(
                row["chat_id"], row["chat_type"] or ("group" if int(row["chat_id"]) < 0 else "private"),
            ),
            "prior_status": row["authorization_status"],
            "reason": row["unreachable_reason"],
            "since": _when(_local(row["unreachable_at"], tz), now.astimezone(tz)) if row["unreachable_at"] else "",
            "hidden": row["hidden"],
        }
        for row in rows
    ]


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
        "unreachable": await _unreachable_rows(app),
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


@router.get("/api/chats/unreachable")
async def read_unreachable_chats(request: Request, include_hidden: bool = Query(False)) -> dict:
    app = owner_app(request)
    return {"unreachable": await _unreachable_rows(app, include_hidden=include_hidden)}


@router.post("/api/chats/reachability")
async def run_reachability(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, Nothing)
    summary = await check_reachability(app)
    message = f"Checked {summary['checked']} chat{'s' * (summary['checked'] != 1)}; {summary['unreachable']} not reachable."
    return {"message": message, "summary": summary, **await _view(app)}


@router.post("/api/chats/hide")
async def hide_chat(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ChatHide)
    if update.chat_id == app.owner_id:
        raise HTTPException(409, "Cannot hide your own DM.")
    if not await app.store.hide_chat(update.chat_id, update.hidden):
        raise HTTPException(404, "Unknown chat, or one Dom can still reach.")
    label = "Hid" if update.hidden else "Restored"
    await owner.record_event(app, f"{WHO} {label.lower()} chat {update.chat_id}")
    return await _done(app, f"{label} chat.")


@router.post("/api/chats/forget")
async def forget_chat(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ChatForget)
    if update.chat_id == app.owner_id:
        raise HTTPException(409, "Cannot forget your own DM.")
    if update.delete_history and not update.confirm:
        raise HTTPException(422, "Confirm history deletion before forgetting this chat's messages.")
    if not await app.store.forget_chat(update.chat_id, delete_history=update.delete_history):
        raise HTTPException(404, "Unknown chat, or one Dom can still reach.")
    detail = " and deleted its history" if update.delete_history else " (history kept)"
    await owner.record_event(app, f"{WHO} forgot chat {update.chat_id}{detail}")
    return await _done(app, f"Forgot chat{detail}.")


RECENT_MESSAGES = 20


async def _scope(app, chat_id: int) -> str:
    """The scope of an approved or unreachable chat; 404 for one the bot has no row for."""
    chat = next((row for row in (await _chats(app))["rows"] if row["id"] == chat_id), None)
    if chat is not None:
        return chat["scope"]
    unreachable = await app.store.list_unreachable_chats(include_hidden=True)
    match = next((row for row in unreachable if int(row["chat_id"]) == chat_id), None)
    if match is None:
        raise HTTPException(404, "Unknown chat.")
    return app.store.scope_for(chat_id, match["chat_type"] or ("group" if chat_id < 0 else "private"))


@router.get("/api/chats/{chat_id}/media")
async def read_media_standing(request: Request, chat_id: int) -> MediaStanding:
    """What studio media this chat has used and has left, against the limits of its own scope (owner DM, friend DM or
    group) and its own deliveries. Owner only, like every dashboard route."""
    app = owner_app(request)
    return await media_standing(app, await _scope(app, chat_id), chat_id)


@router.get("/api/chats/{chat_id}/messages")
async def read_messages(
    request: Request,
    chat_id: int,
    limit: int = Query(RECENT_MESSAGES, ge=1, le=100),
    before: int | None = Query(None, ge=1),
) -> dict:
    app = owner_app(request)
    scope = await _scope(app, chat_id)
    full = scope == "owner_dm"
    tz = app.dispatch.presence.tz
    now = app.dispatch.now().astimezone(tz)
    rows, next_before = await app.store.get_recent_chat_messages(chat_id, limit, before_id=before)
    return {
        "chat_id": chat_id,
        "full": full,
        "messages": [{
            "id": row.id,
            "who": "Dom" if row.role == "assistant" else "You" if full else (row.sender_name or "Someone"),
            "mine": row.role == "assistant",
            "text": row.text if full else _preview(row.text),
            "at": _when(_local(row.created_at, tz), now),
        } for row in rows],
        "next_before": next_before,
        "has_more": next_before is not None,
    }


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


@router.post("/api/chats/fresh")
async def fresh_start(request: Request) -> dict:
    """Start any approved chat's window from now, or undo the last start. Nothing is deleted; Dom is told in that chat."""
    app = owner_app(request)
    update = await _input(request, FreshStart)
    message = await owner.fresh_start(app, update.chat_id, WHO, undo=update.undo)
    if message is None:
        raise HTTPException(404, "Unknown chat.")
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

"""JSON API for the Memory page: the file list, a file's text and its save, the soul request, memory imports and
the memory proposals waiting for review.

Every read and check lives in dom/web/shared.py: a file opens only when it is listed, a private
file only in the private scope, HABITS.md never saves. Every POST sits in JSON_POSTS, so it needs the dashboard's
auth, a same-origin Origin and a JSON body, like the settings routes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from dom import memory_export, memory_import
from dom.home import MAX_SOUL_EDIT, content_version
from dom.telegram import owner
from dom.web.model_settings import _input
from dom.web.shared import (
    ARCHIVE,
    HABITS,
    IMPORT_SERVICES,
    MAX_SOUL_CHAT,
    MAX_SOUL_TURNS,
    OwnerError,
    _title,
    _memory_file,
    _memory_sections,
    _proposals,
    decide_import_batch,
    owner_app,
    soul_chat,
    run_memory_import,
)

log = logging.getLogger(__name__)


def _today(app) -> date:
    """The owner's local date: a hand edit's pin carries the day on the owner's clock, not the server's."""
    return app.dispatch.now().astimezone(ZoneInfo(app.config.timezone)).date()
router = APIRouter()
PREFIX = "/api/memory"
GUARDED_GETS = {f"{PREFIX}/export", f"{PREFIX}/index/estimate", f"{PREFIX}/index/progress"}
JSON_POSTS = {
    f"{PREFIX}/save", f"{PREFIX}/soul", f"{PREFIX}/import", f"{PREFIX}/import-review", f"{PREFIX}/proposal",
    f"{PREFIX}/index/confirm", f"{PREFIX}/index/cancel",
}
MAX_SAVE = 1024 * 1024 + 4096  # a 1 MB file with JSON around it
MAX_IMPORT = memory_import.MAX_DOCUMENT_BYTES * 4 // 3 + 4096  # the document as base64 with JSON around it
EDIT_KINDS = ("soul_edit", "memory_edit")  # imports are decided in batches, through /import-review


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Save(Strict):
    file: str = Field(max_length=200)
    version: str = Field(max_length=64)
    content: str


class SoulTurn(Strict):
    role: Literal["owner", "dom"]
    text: str


class SoulChat(Strict):
    messages: list[SoulTurn] = Field(max_length=100)  # soul_chat holds the real caps, with a sentence for each


class ImportDocument(Strict):
    service: str = Field(max_length=20)
    filename: str = Field(max_length=255)
    data: str  # the file's bytes, base64


class ImportReview(Strict):
    id: int = Field(gt=0, lt=2**63)
    decision: Literal["selected", "all", "reject"]
    selected: list[int] = Field(default_factory=list, max_length=1000)


class ProposalDecision(Strict):
    id: int = Field(gt=0, lt=2**63)
    decision: Literal["approve", "reject"]


class IndexAction(Strict):
    pass


async def _waiting(app) -> dict:
    """The memory proposals pending: imports grouped by batch, and soul and note edits with their diff."""
    cards = (await _proposals(app))["cards"]
    imports = [
        {"id": card["id"], "service": card["service"], "facts": [{"id": fact["id"], "text": fact["body"]["text"]} for fact in card["facts"]]}
        for card in cards if "facts" in card
    ]
    edits = []
    for card in cards:
        if card.get("kind") == "soul_edit":
            edits.append({"id": card["id"], "kind": "soul_edit", "label": card["label"], "title": card["title"],
                          "diff": [f"- {card['body']['old']}", f"+ {card['body']['new']}"]})
        elif card.get("kind") == "memory_edit":
            edits.append({"id": card["id"], "kind": "memory_edit", "label": card["label"], "title": card["title"],
                          "diff": card["changes"]})
    return {"imports": imports, "edits": edits}


async def _chat_names(app) -> dict[str, str]:
    known = {c["chat_id"]: c for c in await app.store.list_chats()}
    known.update({c["chat_id"]: c for c in await app.store.list_approved_chats()})
    return {str(chat_id): _title(chat, app.owner_id) for chat_id, chat in known.items()}


@router.get(PREFIX)
async def read_memory(request: Request) -> dict:
    app = owner_app(request)
    return {
        "sections": [
            {"heading": heading, "archive": heading == ARCHIVE, "files": [{"path": path, "label": label} for path, label in files]}
            for heading, files in await _memory_sections(app) if files
        ],
        # Source tags ({chat -100123}) show in the preview as the chat's name: every chat Dom knows, by ID.
        "chat_names": await _chat_names(app),
        "services": [{"id": key, "label": label} for key, label in IMPORT_SERVICES.items()],
        "export_prompt": memory_import.export_prompt().strip(),  # what the owner pastes into the other assistant
        "max_soul_request": MAX_SOUL_EDIT,
        "max_soul_turns": MAX_SOUL_TURNS,
        "max_soul_chat": MAX_SOUL_CHAT,
        **await _waiting(app),
    }


@router.get(PREFIX + "/core")
async def core_memories(request: Request) -> dict:
    """Every core memory across Dom's notes, private ones included (owner only), with who pinned it and why. The
    owner deletes one by editing its file (/file, /save)."""
    app = owner_app(request)
    items = await asyncio.to_thread(app.home.scoped(allow_private=True).core_memories)
    return {"cap": app.config.core_memory_cap, "items": items}


@router.get(PREFIX + "/index/estimate")
async def index_estimate(request: Request) -> dict:
    """A staged model never starts work until the owner calls /confirm."""
    return await owner_app(request).knowledge.estimate()


@router.get(PREFIX + "/index/progress")
async def index_progress(request: Request) -> dict:
    return await owner_app(request).knowledge.progress()


@router.post(PREFIX + "/index/confirm")
async def index_confirm(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, IndexAction, 1024)
    estimate = await app.knowledge.estimate()
    if not estimate["staged"]:
        raise HTTPException(409, "No embedding model change is staged.")
    app.dispatch._spawn(app.knowledge.reindex())
    return {"started": True, **estimate}


@router.post(PREFIX + "/index/cancel")
async def index_cancel(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, IndexAction, 1024)
    await app.knowledge.cancel()
    return {"cancelled": True, **await app.knowledge.progress()}


@router.get(PREFIX + "/file")
async def read_file(request: Request, file: str) -> dict:
    file_ = await _memory_file(owner_app(request), file)
    return {key: file_[key] for key in ("file", "label", "readonly", "limit", "content", "version")}


@router.post(PREFIX + "/save")
async def save_file(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Save, MAX_SAVE)
    if update.file == HABITS:
        raise HTTPException(403, "HABITS.md is read-only.")
    listed = await _memory_file(app, update.file)
    if listed["readonly"]:  # the journal archive and the former files are the record: read, never rewritten here
        raise HTTPException(403, f"{listed['label']} is read-only.")
    content = update.content.replace("\r\n", "\n").replace("\r", "\n")
    home = app.home.scoped(allow_private=update.file.startswith("private/"))
    try:
        result = home.save_memory(update.file, content, update.version, today=_today(app))
    except (OSError, ValueError):
        log.exception("could not save a dashboard memory file")
        raise HTTPException(500, "Couldn't save this file. Check that Dom can write to it.") from None
    if not result.ok:
        raise HTTPException(422 if result.over_limit else 409, result.error)
    await owner.record_event(app, f"Saved {update.file} in the dashboard")
    return {"message": f"Saved {listed['label']}.", "content": result.content, "version": content_version(result.content)}


@router.post(PREFIX + "/soul")
async def soul_conversation(request: Request) -> dict:
    """Dom's next reply in the owner's conversation about SOUL.md. The page keeps the conversation and sends it whole;
    an edit Dom proposes waits as a soul_edit proposal, decided through /proposal."""
    app = owner_app(request)
    update = await _input(request, SoulChat, MAX_SOUL_CHAT * 6 + 16_384)
    try:
        reply, edit, proposal_id = await soul_chat(app, [(turn.role, turn.text) for turn in update.messages])
    except OwnerError as exc:
        raise HTTPException(422, str(exc)) from None
    proposal = None if edit is None else {"id": proposal_id, "diff": [f"- {edit['old']}", f"+ {edit['new']}"]}
    return {"reply": reply, "proposal": proposal, **await _waiting(app)}


@router.post(PREFIX + "/import")
async def import_document(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ImportDocument, MAX_IMPORT)
    try:
        raw = base64.b64decode(update.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Couldn't read that file. Choose it again.") from None
    try:
        message, count = await run_memory_import(app, update.service.strip().lower(), update.filename, raw)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "Could not read the import or existing notes.") from None
    return {"message": message, "count": count, **await _waiting(app)}


@router.post(PREFIX + "/import-review")
async def review_import(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ImportReview)
    message, settled = await decide_import_batch(app, update.id, update.decision, {str(item) for item in update.selected})
    return {"message": message, "settled": settled, **await _waiting(app)}


@router.post(PREFIX + "/proposal")
async def decide_memory_proposal(request: Request) -> dict:
    """Approve or reject one soul or note edit through the proposals store, the way /proposals does."""
    app = owner_app(request)
    update = await _input(request, ProposalDecision)
    row = next((row for row in await app.proposals.pending() if row["id"] == update.id), None)
    if row is None or row["kind"] not in EDIT_KINDS:
        raise HTTPException(404, "Already handled.")
    message = await app.proposals.decide(update.id, update.decision == "approve")
    if message not in ("Approved", "Rejected"):
        raise HTTPException(409, message)
    return {"message": message, **await _waiting(app)}


@router.get(PREFIX + "/export")
async def export_memory(request: Request) -> Response:
    """All of Dom's memory as a zip: owner-only, built off the event loop, and noted as an owner event."""
    app = owner_app(request)
    try:
        data = await asyncio.to_thread(memory_export.build_export, app.home.root)
    except memory_export.ExportTooLarge as exc:
        raise HTTPException(413, str(exc)) from None
    except OSError:
        log.exception("could not build the memory export")
        raise HTTPException(500, "Couldn't build the export. Check that Dom can read its notes.") from None
    await owner.record_event(app, "Downloaded a memory export from the dashboard")
    name = memory_export.export_name(app.dispatch.now())
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

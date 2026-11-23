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
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from dom import memory_export, memory_import
from dom.home import MAX_SOUL_EDIT, content_version
from dom.telegram import owner
from dom.web.model_settings import _input
from dom.web.shared import (
    HABITS,
    IMPORT_SERVICES,
    OwnerError,
    _memory_file,
    _memory_sections,
    _proposals,
    decide_import_batch,
    owner_app,
    propose_soul_edit,
    run_memory_import,
)

log = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/memory"
GUARDED_GETS = {f"{PREFIX}/export"}  # reads that build a download and leave a record: same-site browsers only
JSON_POSTS = {f"{PREFIX}/save", f"{PREFIX}/soul", f"{PREFIX}/import", f"{PREFIX}/import-review", f"{PREFIX}/proposal"}
MAX_SAVE = 1024 * 1024 + 4096  # a 1 MB file with JSON around it
MAX_IMPORT = memory_import.MAX_DOCUMENT_BYTES * 4 // 3 + 4096  # the document as base64 with JSON around it
EDIT_KINDS = ("soul_edit", "memory_edit")  # imports are decided in batches, through /import-review


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Save(Strict):
    file: str = Field(max_length=200)
    version: str = Field(max_length=64)
    content: str


class SoulRequest(Strict):
    message: str


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


@router.get(PREFIX)
async def read_memory(request: Request) -> dict:
    app = owner_app(request)
    return {
        "sections": [
            {"heading": heading, "files": [{"path": path, "label": label} for path, label in files]}
            for heading, files in await _memory_sections(app) if files
        ],
        "services": [{"id": key, "label": label} for key, label in IMPORT_SERVICES.items()],
        "export_prompt": memory_import.export_prompt().strip(),  # what the owner pastes into the other assistant
        "max_soul_request": MAX_SOUL_EDIT,
        **await _waiting(app),
    }


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
    content = update.content.replace("\r\n", "\n").replace("\r", "\n")
    home = app.home.scoped(allow_private=update.file.startswith("private/"))
    try:
        result = home.save_memory(update.file, content, update.version)
    except (OSError, ValueError):
        log.exception("could not save a dashboard memory file")
        raise HTTPException(500, "Couldn't save this file. Check that Dom can write to it.") from None
    if not result.ok:
        raise HTTPException(422 if result.over_limit else 409, result.error)
    await owner.record_event(app, f"Saved {update.file} in the dashboard")
    return {"message": f"Saved {listed['label']}.", "content": result.content, "version": content_version(result.content)}


@router.post(PREFIX + "/soul")
async def soul_request(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, SoulRequest, MAX_SOUL_EDIT * 12 + 1024)
    try:
        edit, proposal_id = await propose_soul_edit(app, update.message)
    except OwnerError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"id": proposal_id, "diff": [f"- {edit['old']}", f"+ {edit['new']}"]}


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

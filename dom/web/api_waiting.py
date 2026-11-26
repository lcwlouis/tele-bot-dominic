"""JSON API for the "Waiting for you" page: every pending proposal, chat approval and import, and the settled ones.

Every read and decision lives in dom/web/shared.py: the same proposals store decides, a
stranger's chat is approved or ignored through dom.telegram.owner, an import is decided in batches, and a settled
proposal is deleted only with its created-at stamp. Every POST sits in JSON_POSTS, so it needs the dashboard's auth, a
same-origin Origin and a JSON body. Each write answers `{ok, message, ...fresh page data}`; the page filter
(kind, source) rides along so the answer keeps it. Only what the page shows leaves here.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.plugins.manifest import NETWORK_NOTICE
from dom.plugins.trial import STORAGE_NOTICE, test_run_detail
from dom.telegram import owner
from dom.web.model_settings import _input
from dom.web.shared import WHO, _proposals, decide_import_batch, owner_app

router = APIRouter()
PREFIX = "/api/waiting"
JSON_POSTS = {f"{PREFIX}/decide", f"{PREFIX}/chat", f"{PREFIX}/import", f"{PREFIX}/delete"}
ID = Field(gt=0, lt=2**63)
KINDS = ("soul_edit", "skill", "plugin", "memory_fact", "memory_edit", "lan_address", "cross_chat", "chat_approval")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filtered(Strict):
    kind: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=80)


class Decide(Filtered):
    id: int = ID
    decision: Literal["approve", "reject"]


class ChatDecision(Filtered):
    chat_id: int = Field(gt=-(2**63), lt=2**63)
    decision: Literal["approve", "ignore"]


class ImportDecision(Filtered):
    id: int = ID
    decision: Literal["selected", "all", "reject"]
    selected: list[int] = Field(default_factory=list, max_length=1000)


class Delete(Filtered):
    id: int = ID
    expected: str = Field(max_length=64)  # the settled proposal's created-at stamp


def _trial(result: dict | None) -> dict | None:
    if not result:
        return None
    return {
        "label": result["label"], "elapsed_ms": result["elapsed_ms"],
        "failed": bool(result["error"]), "detail": test_run_detail(result),
    }


def _card(row: dict) -> dict:
    """One pending proposal: its label and title, and the part of its body that kind shows."""
    body, kind = row["body"], row["kind"]
    card = {"type": "proposal", "id": row["id"], "kind": kind, "label": row["label"], "title": row["title"]}
    if kind == "soul_edit":
        card["diff"] = [f"- {body['old']}", f"+ {body['new']}"]
    elif kind == "skill":
        card |= {"description": body["description"], "text": body["body"], "diff": row.get("changes", [])}
    elif kind == "plugin":
        card |= {
            "description": body["description"], "network": bool(body.get("network")), "storage": bool(body.get("storage")),
            "test_run": _trial(body.get("test_run")), "review": row["review_path"],
        }
    elif kind == "lan_address":
        card |= {
            "old": body["old"], "new": body["new"], "address_name": body.get("name") or "",
            "places": body.get("places") or [], "macs": body.get("macs") or [],
        }
    elif kind == "cross_chat":
        card |= {"text": body["text"], "reason": body["reason"], "leak_score": body.get("leak_score")}
    elif kind == "memory_edit":
        card["diff"] = row.get("changes", [])
    else:
        card["text"] = f"{body.get('file', '')}: {body.get('text', '')}"
    return card


async def _view(app, kind: str = "", source: str = "", linked: int | None = None) -> dict:
    data = await _proposals(app, kind, source, linked)
    cards = [
        {"type": "import", "id": card["id"], "service": card["service"], "facts": [{"id": fact["id"], "text": fact["body"]["text"]} for fact in card["facts"]]}
        if "facts" in card else _card(card)
        for card in data["cards"]
    ]
    desktop_permissions = await app.desktop_permissions.pending_requests() if not kind and not source else []
    secret_requests = await app.secret_requests.pending_list() if not kind and not source else []
    return {
        "count": data["count"] + len(desktop_permissions) + len(secret_requests),
        "kind": kind, "source": source, "kinds": list(KINDS), "sources": data["sources"],
        "chats": data["pending_chats"], "cards": cards,
        "desktop_permissions": desktop_permissions,
        "secret_requests": secret_requests,
        "settled": [
            {"id": row["id"], "label": row["label"], "status": row["status"], "title": row["title"], "created_at": row["created_at"]}
            for row in data["settled"]
        ],
        "notices": {"network": NETWORK_NOTICE, "storage": STORAGE_NOTICE},
    }


@router.get(PREFIX)
async def read_waiting(
    request: Request, kind: str = "", source: str = "", id: int | None = Query(default=None, gt=0, lt=2**63),
) -> dict:
    """The page; `id` (from a ?proposal= link) also brings that proposal under Settled when it is older than the latest."""
    return await _view(owner_app(request), kind[:40], source[:80], id)


@router.post(PREFIX + "/decide")
async def decide(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Decide)
    message = await app.proposals.decide(update.id, update.decision == "approve")
    return {"ok": message in ("Approved", "Rejected"), "message": message, **await _view(app, update.kind, update.source)}


@router.post(PREFIX + "/chat")
async def decide_chat(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ChatDecision)
    chat, label = await owner.decide_pending_chat(app, update.chat_id, update.decision, WHO)
    message = f"{label}: {owner.display_title(chat, app.owner_id)}" if chat is not None else "Already handled."
    return {"ok": chat is not None, "message": message, **await _view(app, update.kind, update.source)}


@router.post(PREFIX + "/import")
async def decide_import(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, ImportDecision)
    message, _ = await decide_import_batch(app, update.id, update.decision, {str(item) for item in update.selected})
    ok = message != "Already handled." and "still pending" not in message
    return {"ok": ok, "message": message, **await _view(app, update.kind, update.source)}


@router.post(PREFIX + "/delete")
async def delete_settled(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Delete)
    ok, message = await app.proposals.delete(update.id, update.expected)
    if not ok:
        raise HTTPException(404 if "not found" in message else 409, message)
    return {"ok": True, "message": message, **await _view(app, update.kind, update.source)}

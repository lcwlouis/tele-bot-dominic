"""Owner-only JSON API for Noticings and Ideas (R7)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.noticings import KINDS, STATUSES
from dom.web.model_settings import _input
from dom.web.shared import owner_app

router = APIRouter()
PREFIX = "/api/noticings"
JSON_POSTS = {f"{PREFIX}/decide", f"{PREFIX}/delete"}
ID = Field(gt=0, lt=2**63)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecideNoticing(Strict):
    id: int = ID
    decision: Literal["accept", "dismiss"]
    status: str = Field(default="", max_length=24)
    kind: str = Field(default="", max_length=24)


class DeleteNoticing(Strict):
    id: int = ID
    expected: str = Field(max_length=64)
    status: str = Field(default="", max_length=24)
    kind: str = Field(default="", max_length=24)


def _public_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "day": row["day"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "action_kind": row["action_kind"],
        "source_kind": row["source_kind"],
        "source_chat_id": row["source_chat_id"],
        "source_msg_id": row["source_msg_id"],
        "source_label": row["source_label"],
        "status": row["status"],
        "outcome": row["outcome"],
        "sent_as_opener_at": row["sent_as_opener_at"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


async def _listing(app, status: str | None = None, kind: str | None = None) -> dict:
    rows = await app.noticings.list_all(status=status, kind=kind)
    pending = await app.noticings.pending()
    return {
        "noticings": [_public_row(r) for r in rows],
        "pending_count": len(pending),
        "kinds": list(KINDS),
        "statuses": list(STATUSES),
    }


@router.get(PREFIX)
async def read_noticings(request: Request, status: str = "", kind: str = "") -> dict:
    app = owner_app(request)
    try:
        return await _listing(app, status=status or None, kind=kind or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post(PREFIX + "/decide")
async def decide_noticing(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, DecideNoticing)
    ok, message, row = await app.noticings.decide(body.id, accept=body.decision == "accept")
    return {
        "ok": ok,
        "message": message,
        "noticing": _public_row(row) if row else None,
        **await _listing(app, status=body.status or None, kind=body.kind or None),
    }


@router.post(PREFIX + "/delete")
async def delete_noticing(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, DeleteNoticing)
    ok, message = await app.noticings.delete(body.id, body.expected)
    if not ok:
        raise HTTPException(404 if "not found" in message.lower() else 409, message)
    return {"ok": True, "message": message, **await _listing(app, status=body.status or None, kind=body.kind or None)}

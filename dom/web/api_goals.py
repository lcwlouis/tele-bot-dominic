"""Owner-only JSON API for Goals (R6)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.goals import CATEGORIES, MAX_NOTE, MAX_TEXT, RHYTHMS, STATUSES
from dom.web.model_settings import _input
from dom.web.shared import owner_app

router = APIRouter()
PREFIX = "/api/goals"
JSON_POSTS = {
    f"{PREFIX}/create",
    f"{PREFIX}/update",
    f"{PREFIX}/status",
    f"{PREFIX}/note",
    f"{PREFIX}/link-reminder",
    f"{PREFIX}/unlink-reminder",
    f"{PREFIX}/delete",
}
ID = Field(gt=0, lt=2**63)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalCreate(Strict):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    category: str = Field(default="", max_length=32)
    target_date: str | None = Field(default=None, max_length=16)
    check_in_rhythm: str = Field(default="", max_length=32)


class GoalUpdate(Strict):
    id: int = ID
    text: str | None = Field(default=None, max_length=MAX_TEXT)
    category: str | None = Field(default=None, max_length=32)
    target_date: str | None = Field(default=None, max_length=16)
    clear_target_date: bool = False
    check_in_rhythm: str | None = Field(default=None, max_length=32)


class GoalStatus(Strict):
    id: int = ID
    status: Literal["active", "paused", "done", "dropped"]
    note: str = Field(default="", max_length=MAX_NOTE)


class GoalNote(Strict):
    id: int = ID
    note: str = Field(min_length=1, max_length=MAX_NOTE)


class GoalReminderLink(Strict):
    goal_id: int = ID
    reminder_id: int = ID


class GoalDelete(Strict):
    id: int = ID
    expected: str = Field(max_length=64)


def _error_status(message: str) -> int:
    if "not found" in message.lower():
        return 404
    if "at most" in message.lower() or "changed" in message.lower():
        return 409
    return 400


async def _listing(app, status: str | None = None) -> dict:
    return {
        "goals": await app.goals.list_all(status=status),
        "categories": [c for c in CATEGORIES if c],
        "rhythms": [r for r in RHYTHMS if r],
        "statuses": list(STATUSES),
    }


@router.get(PREFIX)
async def read_goals(request: Request, status: str = "") -> dict:
    app = owner_app(request)
    try:
        return await _listing(app, status=status or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get(PREFIX + "/{goal_id:int}")
async def read_goal(request: Request, goal_id: int) -> dict:
    app = owner_app(request)
    goal = await app.goals.get(goal_id)
    if goal is None:
        raise HTTPException(404, f"Goal #{goal_id} not found.")
    return {"goal": goal}


@router.post(PREFIX + "/create")
async def create_goal(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalCreate)
    try:
        goal = await app.goals.create(
            body.text,
            category=body.category,
            target_date=body.target_date,
            check_in_rhythm=body.check_in_rhythm,
            source="api",
        )
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Added goal #{goal['id']}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/update")
async def update_goal(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalUpdate)
    target = None if body.clear_target_date else (body.target_date if body.target_date is not None else ...)
    try:
        goal = await app.goals.update(
            body.id,
            text=body.text,
            category=body.category,
            target_date=target,
            check_in_rhythm=body.check_in_rhythm,
            source="api",
        )
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Updated goal #{goal['id']}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/status")
async def set_goal_status(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalStatus)
    try:
        goal = await app.goals.set_status(body.id, body.status, note=body.note, source="api")
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Goal #{goal['id']} marked {body.status}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/note")
async def add_goal_note(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalNote)
    try:
        goal = await app.goals.add_progress(body.id, body.note, source="api")
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Noted progress on goal #{goal['id']}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/link-reminder")
async def link_reminder(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalReminderLink)
    try:
        goal = await app.goals.link_reminder(body.goal_id, body.reminder_id, source="api")
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Linked reminder #{body.reminder_id} to goal #{body.goal_id}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/unlink-reminder")
async def unlink_reminder(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalReminderLink)
    try:
        goal = await app.goals.unlink_reminder(body.goal_id, body.reminder_id, source="api")
    except ValueError as exc:
        raise HTTPException(_error_status(str(exc)), str(exc)) from None
    return {"ok": True, "message": f"Unlinked reminder #{body.reminder_id} from goal #{body.goal_id}.", "goal": goal, **await _listing(app)}


@router.post(PREFIX + "/delete")
async def delete_goal(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, GoalDelete)
    ok, message = await app.goals.delete(body.id, body.expected)
    if not ok:
        raise HTTPException(404 if "not found" in message.lower() else 409, message)
    return {"ok": True, "message": message, **await _listing(app)}

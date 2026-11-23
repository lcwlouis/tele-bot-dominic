"""JSON API for the Work destination: the desk, Dom's own work and skills, reminders and briefs.

Every read and check is the old page's own (dom/web/desktop.py, dom/web/pages.py): the same store, worker and owner
functions, so a delete still needs the row's stamp or hash, a stopped computer stays stopped while a task runs, and
a skill name is validated. Every POST sits in JSON_POSTS, so it needs the dashboard's auth, a same-origin Origin and a
JSON body. Each write answers `{ok, message, ...fresh page data}` so the page never shows stale rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom import skills
from dom.briefs import list_briefs, markdown_blocks
from dom.telegram import owner
from dom.web.desktop import _context, _lane, resume_handoff, run_computer_action
from dom.web.model_settings import _input
from dom.web.pages import _own_work, _reminders, _skills, owner_app, own_work_file

log = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/work"
JSON_POSTS = {
    f"{PREFIX}/desk/computer", f"{PREFIX}/desk/done", f"{PREFIX}/desk/delete",
    f"{PREFIX}/own/cancel", f"{PREFIX}/own/delete", f"{PREFIX}/skills/action",
    f"{PREFIX}/reminders/cancel", f"{PREFIX}/reminders/delete",
}
ID = Field(gt=0, lt=2**63)
SHOWN_RESULT = 4000  # a task's result and error are short by construction; this only bounds a hostile row


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Computer(Strict):
    action: Literal["start", "stop"]
    container: str = Field(default="", max_length=64)


class Done(Strict):
    task_id: int = ID
    steps: int = Field(ge=0, lt=2**31)


class Remove(Strict):
    id: int = ID
    expected: str = Field(max_length=64)


class Cancel(Strict):
    id: int = ID


class SkillAction(Strict):
    name: str = Field(max_length=64)
    action: Literal["retire", "restore", "delete"]
    expected: str = Field(default="", max_length=128)  # the skill's hash, for delete


def _status(message: str) -> int:
    """The old routes' failure codes: a missing row is a 404, any other refusal a 409."""
    return 404 if "not found" in message else 409


def _short(text: str | None) -> str:
    return (text or "")[:SHOWN_RESULT]


# --- desk -------------------------------------------------------------------------------------------------------


def _task(row: dict | None) -> dict | None:
    return None if row is None else {key: row[key] for key in ("id", "status", "steps", "brief", "error")}


async def _desk(app, lane) -> dict:
    state = await _context(app, lane)
    history = state["history"]["rows"]  # _context already read it
    return {
        "enabled": state["enabled"],
        "status": state["status"],
        "container": state["container"],
        "on_this_machine": state["local"],
        "error": state["error"],
        "handoff_host": state["handoff_host"],
        "task": _task(state["task"]),
        "viewer": f"/desktop/vnc/index.html{state['q']}" if state["enabled"] else "",
        "containers": [{
            "name": item["name"], "host": item["host"], "status": item["status"], "state": item["state"],
            "tone": item["tone"], "detail": item["detail"], "selected": item["selected"],
            "task": None if item["task"] is None else {"id": item["task"]["id"], "status": item["task"]["status"]},
        } for item in state["containers"]],
        "history": [{
            "id": row["id"], "status": row["status"], "container": row["container"], "steps": row["steps"],
            "brief": row["brief"], "result": _short(row.get("result")), "error": _short(row.get("error")),
            "finished": row["finished_local"], "created_at": row["created_at"],
        } for row in history],
    }


@router.get(PREFIX + "/desk")
async def read_desk(request: Request, container: str = "") -> dict:
    app = owner_app(request)
    return await _desk(app, _lane(app, container))


@router.post(PREFIX + "/desk/computer")
async def desk_computer(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Computer)
    lane = _lane(app, update.container)
    if lane is None:
        raise HTTPException(409, "The desktop is off.")
    message = await run_computer_action(app, lane, update.action)
    ok = message in ("Computer ready.", "Computer stopped.")
    return {"ok": ok, "message": message, **await _desk(app, lane)}


@router.post(PREFIX + "/desk/done")
async def desk_done(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Done)
    message, lane = await resume_handoff(app, update.task_id, update.steps)
    return {"ok": message == "Task resumed.", "message": message, **await _desk(app, lane)}


@router.post(PREFIX + "/desk/delete")
async def desk_delete(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Remove)
    ok, message = await owner.delete_desktop_task(app, update.id, update.expected)
    if not ok:
        raise HTTPException(_status(message), message)
    return {"ok": True, "message": message, **await _desk(app, _lane(app, ""))}


# --- own work and skills ----------------------------------------------------------------------------------------


async def _own(app) -> dict:
    state = await _own_work(app)
    settings = state["settings"]
    guard = state["guard"]
    return {
        "enabled": settings.enabled,
        "active": state["active"],
        "delay_hours": settings.delay_hours,
        "guard": None if guard is None else {"ok": bool(guard.get("ok")), "detail": str(guard.get("detail", "")), "at": guard["at_local"]},
        "tasks": [{
            "id": row["id"], "goal": row["goal"], "why": row["why"], "status": row["status"], "tools": row["tools"],
            "sites": row["sites"], "minutes": row["minutes"], "steps_budget": row["steps_budget"], "steps": row["steps"],
            "announced": row["announced_local"], "starts": row["not_before_local"], "finished": row["finished_local"],
            "result": _short(row.get("result")), "error": _short(row.get("error")), "files": row["files"],
            "announced_at": row["announced_at"],
        } for row in state["rows"]],
        "skills": [{key: skill[key] for key in ("name", "description", "body", "hash", "retired")} for skill in (await _skills(app))["rows"]],
    }


@router.get(PREFIX + "/own")
async def read_own(request: Request) -> dict:
    return await _own(owner_app(request))


@router.get(PREFIX + "/own/{task_id:int}/files/{name}")
async def own_file(request: Request, task_id: int, name: str):
    return await own_work_file(request, task_id, name)


@router.post(PREFIX + "/own/cancel")
async def cancel_own(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Cancel)
    cancelled = await app.self_work.cancel(update.id)
    message = f"Cancelled task #{update.id}." if cancelled else f"Task #{update.id} had already finished."
    return {"ok": cancelled, "message": message, **await _own(app)}


@router.post(PREFIX + "/own/delete")
async def delete_own(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Remove)
    ok, message = await app.self_work.delete(update.id, update.expected)
    if not ok:
        raise HTTPException(_status(message), message)
    return {"ok": True, "message": message, **await _own(app)}


@router.post(PREFIX + "/skills/action")
async def skill_action(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, SkillAction)
    try:
        skills._validate_name(update.name)
    except ValueError:
        raise HTTPException(404, "Skill not found.") from None
    if update.action == "delete":
        ok, message = await owner.delete_skill(app, update.name, update.expected)
    else:
        handler = {"retire": owner.retire_skill, "restore": owner.restore_skill}[update.action]
        ok, message = await handler(app, update.name)
    if not ok:
        raise HTTPException(_status(message), message)
    return {"ok": True, "message": message, **await _own(app)}


# --- reminders --------------------------------------------------------------------------------------------------


async def _reminder_rows(app) -> dict:
    return {"reminders": [{
        "id": row["id"], "due": row["due_local"], "importance": row["importance"], "repeat": row["repeat"],
        "status": row["status"], "undeliverable": bool(row["undeliverable"]), "text": row["text"],
        "created_at": row["created_at"],
    } for row in (await _reminders(app))["rows"]]}


@router.get(PREFIX + "/reminders")
async def read_reminders(request: Request) -> dict:
    return await _reminder_rows(owner_app(request))


@router.post(PREFIX + "/reminders/cancel")
async def cancel_reminder(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Cancel)
    changed = await app.dispatch.reminders.cancel(update.id)
    message = f"Cancelled reminder #{update.id}." if changed else f"Reminder #{update.id} is no longer active."
    return {"ok": bool(changed), "message": message, **await _reminder_rows(app)}


@router.post(PREFIX + "/reminders/delete")
async def delete_reminder(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Remove)
    ok, message = await app.dispatch.reminders.delete(update.id, update.expected)
    if not ok:
        raise HTTPException(_status(message), message)
    return {"ok": True, "message": message, **await _reminder_rows(app)}


# --- briefs -----------------------------------------------------------------------------------------------------


@router.get(PREFIX + "/briefs")
async def read_briefs(request: Request) -> dict:
    app = owner_app(request)
    try:
        briefs = await asyncio.to_thread(list_briefs, app.home)
    except (OSError, ValueError, RuntimeError):
        log.exception("could not read the briefs folder")
        raise HTTPException(500, "Could not read the briefs folder.") from None
    return {"briefs": [{
        "filename": brief.filename, "title": brief.title, "markdown": brief.markdown,
        "blocks": [{"kind": kind, "lines": lines} for kind, lines in markdown_blocks(brief.markdown)],
    } for brief in briefs]}

"""JSON API for System's pages: plugins and their review, plugin panels, usage and the turn log.

Every read and check is the old page's own (dom/web/pages.py): the registry reviews and decides a plugin by name and
digest, "I've read this code" is recorded only by its own POST, a delete needs the plugin's hash, and a panel is run
only when it is approved. Plugin content is only ever data here: file text, a manifest's declared surfaces and a
panel's schema-checked values, which the page prints as text and never as markup. Every POST sits in JSON_POSTS, so it
needs the dashboard's auth, a same-origin Origin and a JSON body.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.plugins import surfaces
from dom.plugins.manifest import NETWORK_NOTICE, validate_name
from dom.plugins.trial import STORAGE_NOTICE, test_run_detail
from dom.telegram import owner
from dom.web.model_settings import _input
from dom.web.pages import _plugin_panels, _turns, _usage, owner_app

router = APIRouter()
PREFIX = "/api/system"
JSON_POSTS = {f"{PREFIX}/plugins/reviewed", f"{PREFIX}/plugins/decide", f"{PREFIX}/plugins/delete", f"{PREFIX}/plugins/panel"}
MODEL_CALL_KEYS = (
    "kind", "model", "host", "fallback", "duration_ms", "status", "input_tokens", "output_tokens", "cached_tokens",
    "gap_ms", "between_tokens", "system_hash", "tools_hash", "user_hash", "user_prefix_hash",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Review(Strict):
    name: str = Field(max_length=64)
    digest: str = Field(max_length=128)


class PluginDecision(Review):
    decision: Literal["approve", "reject", "disable"]


class PluginDelete(Strict):
    name: str = Field(max_length=64)
    expected: str = Field(max_length=128)  # the plugin's hash


class PanelRun(Strict):
    name: str = Field(max_length=64)
    panel_id: str = Field(max_length=64)


def _name(name: str) -> str:
    try:
        validate_name(name)
    except ValueError:
        raise HTTPException(404, "Unknown plugin.") from None
    return name


def _trial(result: dict | None) -> dict | None:
    if not result:
        return None
    return {
        "label": result["label"], "elapsed_ms": result["elapsed_ms"],
        "failed": bool(result["error"]), "detail": test_run_detail(result),
    }


# --- plugins ----------------------------------------------------------------------------------------------------


async def _plugins(app) -> dict:
    return {
        "plugins": [{
            "name": row["name"], "status": row["status"], "hash": row["hash"], "available": row["available"],
            "network": row["network"], "storage": row["storage"], "test_run": _trial(row["test_run"]),
        } for row in await app.plugins.listing()],
        "panels": [{"plugin": name, "id": panel.id, "title": panel.title} for name, panel in await _plugin_panels(app)],
        "notices": {"network": NETWORK_NOTICE, "storage": STORAGE_NOTICE},
    }


async def _review(app, name: str, digest: str) -> dict:
    try:
        review = await app.plugins.review(_name(name), digest)
    except ValueError:
        raise HTTPException(404, "Unknown plugin.") from None
    if review is None:
        raise HTTPException(404, "This version of the plugin is no longer on disk.")
    manifest = review["plugin"].manifest
    return {
        "name": manifest.name, "description": manifest.description, "status": review["status"],
        "hash": review["plugin"].hash, "network": manifest.network, "storage": manifest.storage,
        "surfaces_changed": review["surfaces_changed"], "reviewed": review["reviewed"],
        "test_run": _trial(review["test_run"]),
        "tools": [{"name": tool.name, "scopes": list(tool.scopes)} for tool in manifest.tools],
        "commands": [{"name": item.name, "description": item.description, "scopes": list(item.scopes)} for item in manifest.commands],
        "panels": [{"id": panel.id, "title": panel.title} for panel in manifest.panels],
        "documents": [
            {"path": file["path"], "removed": file["removed"], "text": "" if file["removed"] else file["text"], "changes": file["changes"]}
            for file in review["documents"]
        ],
        "notices": {"network": NETWORK_NOTICE, "storage": STORAGE_NOTICE},
    }


@router.get(PREFIX + "/plugins")
async def read_plugins(request: Request) -> dict:
    return await _plugins(owner_app(request))


@router.get(PREFIX + "/plugins/{name}/{digest}")
async def read_review(request: Request, name: str, digest: str) -> dict:
    return await _review(owner_app(request), name, digest)


@router.post(PREFIX + "/plugins/reviewed")
async def plugin_reviewed(request: Request) -> dict:
    """Record that the owner pressed "I've read this code". Opening the page records nothing."""
    app = owner_app(request)
    update = await _input(request, Review)
    await _review(app, update.name, update.digest)
    await app.plugins.mark_reviewed(update.name, update.digest)
    return {"ok": True, "message": "Review recorded.", "review": await _review(app, update.name, update.digest)}


@router.post(PREFIX + "/plugins/decide")
async def plugin_decide(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, PluginDecision)
    _name(update.name)
    message = await app.plugins.decide(update.name, update.digest, update.decision)
    await app.proposals.settle_cards()  # the plugin's Telegram card, if any, stops offering Approve
    if message not in ("Approved", "Rejected", "Disabled", "already handled"):
        raise HTTPException(409, message)
    return {"ok": message != "already handled", "message": message, **await _plugins(app)}


@router.post(PREFIX + "/plugins/delete")
async def plugin_delete(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, PluginDelete)
    _name(update.name)
    message = await owner.delete_plugin(app, update.name, update.expected)
    if message != "Deleted":
        raise HTTPException(404 if message == "Unknown plugin" else 409, message)
    return {"ok": True, "message": message, **await _plugins(app)}


@router.post(PREFIX + "/plugins/panel")
async def plugin_panel(request: Request) -> dict:
    """Run one approved panel. POST, so the same-origin check guards every plugin run."""
    app = owner_app(request)
    update = await _input(request, PanelRun)
    found = await surfaces.find_panel(app, update.name, update.panel_id)
    if found is None:
        raise HTTPException(404, "This panel is not available.")
    plugin, declared = found
    data, error = await surfaces.run_panel(app, plugin, declared)
    return {
        "plugin": plugin.manifest.name, "id": declared.id, "error": error,
        "title": data.title if data and data.title else declared.title,
        "paragraphs": list(data.paragraphs) if data else [],
        "pairs": [list(pair) for pair in data.pairs] if data else [],
        "columns": list(data.columns) if data else [],
        "rows": [list(row) for row in data.rows] if data else [],
    }


# --- usage ------------------------------------------------------------------------------------------------------


def _breakdown(rows: list[tuple[str, int, float]]) -> list[dict]:
    return [{"name": name, "calls": calls, "spend": spend} for name, calls, spend in rows]


@router.get(PREFIX + "/usage")
async def read_usage(request: Request) -> dict:
    data = await _usage(owner_app(request))
    return {
        "spent": data["spent"], "cap": data["cap"], "ratio": data["ratio"], "tone": data["tone"], "kinds": data["kinds"],
        "fallbacks": data["fallbacks"],
        "today_kinds": _breakdown(data["today_kinds"]), "today_models": _breakdown(data["today_models"]),
        "month_kinds": _breakdown(data["month_kinds"]), "month_models": _breakdown(data["month_models"]),
        "bars": [{
            "date": bar["date"], "spent": bar["spent"], "fallbacks": bar["fallbacks"],
            "parts": [{"kind": kind, "y": y, "height": height} for kind, y, height in bar["parts"]],
        } for bar in data["bars"]],
    }


# --- turn log ---------------------------------------------------------------------------------------------------


@router.get(PREFIX + "/turns")
async def read_turns(request: Request, chat: str = "") -> dict:
    data = await _turns(owner_app(request), chat[:64])
    return {
        "chat": data["chat"], "stages": data["stages"], "choices": [{"id": key, "title": title} for key, title in data["choices"]],
        "visible_stages": [stage for stage in data["stages"] if stage in data["visible_stages"]],
        "visits": [{
            "title": visit["title"], "duration_ms": visit["duration_ms"], "depth": visit["depth"],
            "left": visit["left"], "width": visit["width"], "time": f"{visit['time']:%d %b %H:%M:%S}",
        } for visit in data["visits"]],
        "cache": [{"gap": gap, "returns": count, "hit": hit, "between": between} for gap, count, hit, between in data["cache"]],
        "cross_chat": [{
            "kind": item["kind"], "source": item["source_title"], "target": item["target_title"], "status": item["status"],
            "drop_reason": item["drop_reason"], "created_at": item["created_at"], "queued_at": item["queued_at"],
            "sending_at": item["sending_at"], "updated_at": item["updated_at"], "text": item["text"],
        } for item in data["cross_chat"]],
        "turns": [{
            "at": row["at"], "time": f"{row['time']:%H:%M}", "full_time": f"{row['time']:%Y-%m-%d %H:%M %Z}",
            "title": row["title"], "outcome": row["outcome"], "first_bubble_ms": row["first_bubble_ms"],
            "last_bubble_ms": row["last_bubble_ms"], "duration_ms": row["duration_ms"], "model": row["model"],
            "fallback": bool(row["fallback"]), "calls": row["calls"], "steps": row["steps"],
            "visits": [{
                "reason": visit["reason"], "behind": visit["behind_title"], "capped": visit["capped_title"],
            } for visit in row["visits"]],
            "timing": [{"label": label, "ms": ms} for label, ms in row["timing"]],
            "segments": [{"stage": stage, "label": label, "x": x, "ms": ms} for stage, label, x, ms in row["segments"]],
            "bar_ms": row["bar_ms"],
            "model_calls": [{key: call.get(key) for key in MODEL_CALL_KEYS} for call in row["model_calls"]],
        } for row in data["rows"]],
    }

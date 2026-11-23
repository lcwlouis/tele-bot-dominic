"""JSON API for the new dashboard app (served at /app), one endpoint per page as each page is ported.

Read-only for now. Every route sits behind the dashboard's auth middleware in app.py, like the server-rendered pages.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from dom.web.pages import _chats, _proposals, presence_line

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
APP_INDEX = Path(__file__).with_name("static") / "app" / "index.html"
Activity = Literal["idle", "replying", "desktop", "asleep"]


def _greeting(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "Good morning"
    if 12 <= now.hour < 18:
        return "Good afternoon"
    return "Good evening"


def _when(at: datetime, now: datetime) -> str:
    return f"{at:%H:%M}" if at.date() == now.date() else f"{at:%a %H:%M}"


def _local(stamp: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(tz)


def activity(*, replying: bool, desktop_task: bool, presence: str) -> Activity:
    """What the Home orbit animates: a turn in flight wins, then a desktop task, then sleep."""
    if replying:
        return "replying"
    if desktop_task:
        return "desktop"
    return "asleep" if presence == "asleep" else "idle"


def _waiting(proposals: dict) -> dict:
    items = []
    for card in proposals["cards"]:
        if "facts" in card:
            count = len(card["facts"])
            items.append({"label": f"Memory imported from {card['service']}", "detail": f"{count} fact{'s' * (count != 1)}"})
        else:
            items.append({"label": card["label"], "detail": card["title"]})
    items.extend({"label": chat["label"], "detail": chat["title"]} for chat in proposals["pending_chats"])
    return {"count": proposals["count"], "items": items[:3], "href": "/app/waiting"}


async def home(app: App) -> dict:
    tz = ZoneInfo(app.config.timezone)
    now = app.dispatch.now().astimezone(tz)
    chats = await _chats(app)
    state, title, detail = presence_line(chats["rows"], now)
    vibe = chats["vibe"]
    start = datetime.combine(now.date(), time.min, tz)
    turns, replies = await app.store.turn_counts(start, start + timedelta(days=1))
    _, spent = await app.dispatch.usage.today(tz)
    reminders = [row for row in await app.dispatch.reminders.active() if not row.get("undeliverable")]
    titles = {str(row["id"]): row["title"] for row in chats["rows"]}
    desktop_tasks = (
        await app.store.desktop_tasks(app.owner_id, active=True) if app.desktop_worker is not None else []
    )
    return {
        "now": {"greeting": _greeting(now), "date": f"{now:%A} {now.day} {now:%b}", "clock": f"{now:%H:%M}"},
        "presence": {"state": state, "title": title, "detail": detail},
        "activity": activity(replying=app.dispatch.turn_running(), desktop_task=bool(desktop_tasks), presence=state),
        "vibe": {
            "mode": vibe.mode,
            "title": vibe.preset.title if vibe.preset else "",
            "description": vibe.preset.description if vibe.preset else "",
            "reason": vibe.reason,
            "since": _when(vibe.since.astimezone(tz), now) if vibe.since else "",
        },
        "waiting": _waiting(await _proposals(app)),
        "today": {
            "turns": turns, "replies": replies, "spend": round(spent, 4), "cap": app.config.daily_spend_cap_usd,
            "hourly": await app.store.hourly_message_counts(tz=tz, now=now),
            "next_reminder": {
                "when": _when(_local(reminders[0]["due_at"], tz), now), "text": reminders[0]["text"],
            } if reminders else None,
        },
        "recent": [
            {"chat": titles[row["chat_id"]], "text": _preview(row["text"]), "at": _when(_local(row["created_at"], tz), now)}
            for row in await app.store.recent_replies(3) if row["chat_id"] in titles
        ],
        "desktop": {
            "enabled": app.desktop_worker is not None,
            "task": {"brief": desktop_tasks[0]["brief"], "status": desktop_tasks[0]["status"]} if desktop_tasks else None,
        },
    }


def _preview(text: str, limit: int = 120) -> str:
    """One line of a reply, enough to recognise it: the dashboard is not a place to read friends' chats."""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


@router.get("/api/home")
async def home_api(request: Request) -> dict:
    app: App = request.app.state.dom
    if app.owner_id is None:
        return {"setup": True}
    return {"setup": False, **await home(app)}


@router.api_route("/app", methods=["GET", "HEAD"])
@router.api_route("/app/{path:path}", methods=["GET", "HEAD"])
async def new_app(path: str = "") -> FileResponse:
    """The Svelte app routes in the browser; every /app page path gets the same built page. A missing file
    (a path ending in an extension) is a 404, not the page, so a broken asset link fails plainly."""
    if "." in path.rsplit("/", 1)[-1]:
        raise HTTPException(status_code=404)
    return FileResponse(APP_INDEX, media_type="text/html")

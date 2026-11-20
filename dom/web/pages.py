"""Server-rendered dashboard pages and the owner's htmx actions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from dom import memory_import, proposals, traits
from dom.agent import unfence
from dom.briefs import list_briefs, markdown_blocks
from dom.home import MAX_SOUL, MAX_SOUL_EDIT, content_version, limit_for
from dom.plugins import surfaces
from dom.plugins.manifest import NETWORK_NOTICE
from dom.presence import read_habits
from dom.skills import skill_diff
from dom.telegram import owner
from dom.usage import check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
router = APIRouter()
templates = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(("html",)),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates.filters.update(money=lambda amount: f"${amount:,.2f}", urlencode_query=urlencode)
templates.globals["NETWORK_NOTICE"] = NETWORK_NOTICE
WHO = "The owner"
KINDS = {
    "chat": "Replies", "triage": "Triage", "media": "Photos and voice", "memory": "Memory",
    "search": "Web search", "reflection": "Reflection", "guard": "Leak check", "desktop": "Desktop",
}
IMPORT_SERVICES = {"chatgpt": "ChatGPT", "claude": "Claude", "muse": "Muse"}


def render(template: str, **context) -> HTMLResponse:
    return HTMLResponse(templates.get_template(template).render(**context))


def panel(request: Request, name: str, **context) -> HTMLResponse:
    if request.headers.get("HX-Request") == "true":
        return render(f"{name}.html", context=context)
    return render("dashboard.html", active=name, **{name: context})


def owner_app(request: Request) -> App:
    app = request.app.state.dom
    if app.owner_id is None:
        raise HTTPException(409, "Claim Dom in Telegram first.")
    return app


async def form(request: Request, *, max_bytes: int = 8192) -> dict[str, list[str]]:
    """Read htmx URL-encoded forms without a multipart parser."""
    if request.headers.get("Content-Type", "").partition(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected a form.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(413, "The form is too large.")
        data.extend(chunk)
    try:
        return parse_qs(data.decode("utf-8"), keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=310)
    except ValueError:
        raise HTTPException(400, "Invalid form.") from None


async def _bounded_stream(request: Request, max_bytes: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, "The upload is too large.")
        yield chunk


async def multipart_form(request: Request, *, max_bytes: int) -> FormData:
    """Read one multipart upload with Starlette's parser, stopping at max_bytes of body whatever the headers say."""
    if request.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Expected a form.")
    declared = request.headers.get("Content-Length", "")
    if declared.isdecimal() and int(declared) > max_bytes:
        raise HTTPException(413, "The upload is too large.")
    parser = MultiPartParser(request.headers, _bounded_stream(request, max_bytes), max_files=1, max_fields=4, max_part_size=1024)
    try:
        return await parser.parse()
    except MultiPartException:
        raise HTTPException(400, "Invalid form.") from None


def _title(chat: dict, owner_id: int | None) -> str:
    title = owner.display_title(chat, owner_id)
    return "Your DM" if chat["chat_id"] == str(owner_id) else title


async def _chats(app: App) -> dict:
    rows = await app.store.list_approved_chats()
    tz = ZoneInfo(app.config.timezone)
    chats = []
    for row, (state, until) in zip(rows, app.dispatch.peek_presence(rows)):
        chat_id = int(row["chat_id"])
        sliders, preset = await traits.for_chat(app.home, app.store, chat_id, app.dispatch.now())
        unread, _ = await app.store.get_unread(chat_id)
        own, raw_traits = await app.store.get_chat_personality(chat_id)
        own_traits = traits.normalise(traits.decode(raw_traits))
        chats.append({
            "id": chat_id, "title": _title(row, app.owner_id), "state": state, "until": until,
            "when": until.astimezone(tz).strftime("%H:%M") if until else "",
            "scope": app.store.scope_for(chat_id, "group" if chat_id < 0 else "private"),
            "unread": unread, "traits": sliders, "own_traits": own_traits,
            "sliders": [(name, sliders.get(name, 0.5), name in own_traits) for name in traits.NAMES],
            "preset": preset, "own_preset": own,
        })
    presets = [preset for name in app.home.preset_names() if (preset := traits.load_preset(app.home, name))]
    pending = await app.store.list_pending_chats()
    return {
        "rows": chats, "presets": presets, "everywhere": await traits.global_preset_name(app.store), "pending": len(pending),
        "vibe": await traits.vibe(app.home, app.store, app.dispatch.now()),
        "stickers": app.stickers.names(),
    }


def _point(hour: float, radius: float) -> tuple[float, float]:
    angle = hour * math.tau / 24 - math.pi / 2
    return round(math.cos(angle) * radius, 2), round(math.sin(angle) * radius, 2)


def _arc(start: float, end: float, radius: int = 128) -> str:
    x, y = _point(start, radius)
    x2, y2 = _point(end, radius)
    large = int((end - start) % 24 > 12)
    return f"M{x} {y} A{radius} {radius} 0 {large} 1 {x2} {y2}"


def _hour(value: time | datetime) -> float:
    return value.hour + value.minute / 60 + value.second / 3600


MIN_ARC_HOURS = 0.25  # a 3-minute wake still reads as a mark on the dial


def _online_arcs(spans: list[tuple[float, float]]) -> list[list[float]]:
    """Give each session a drawn length of at least MIN_ARC_HOURS inside the day, then merge overlaps."""
    arcs: list[list[float]] = []
    for a, b in sorted(spans):
        b = min(24, max(b, a + MIN_ARC_HOURS))
        a = min(a, b - MIN_ARC_HOURS)
        if arcs and a < arcs[-1][1]:
            arcs[-1][1] = max(arcs[-1][1], b)
        else:
            arcs.append([a, b])
    return arcs


def _day_label(due: datetime, now: datetime) -> str:
    return f"{due:%H:%M}" if due.date() == now.date() else f"{due:%a %H:%M}"


async def _today(app: App, now: datetime, start: datetime, end: datetime) -> list[tuple[str, str]]:
    """The usage page's spend, the turn log's replies and the owner's next reminder, for the day dial's column."""
    tz = now.tzinfo
    turns, replies = await app.store.turn_counts(start, end)
    _, spent = await app.dispatch.usage.today(ZoneInfo(app.config.timezone))
    reminders = await app.dispatch.reminders.active()
    if reminders:
        due = datetime.fromisoformat(reminders[0]["due_at"]).astimezone(tz)
        text = reminders[0]["text"]
        upcoming = f"{_day_label(due, now)} {text if len(text) <= 40 else text[:39] + '…'}"
    else:
        upcoming = "No reminders"
    return [("Turns today", str(turns)), ("Replies sent", str(replies)), ("Spend today", f"${spent:,.2f}"), ("Next reminder", upcoming)]


async def _dial(app: App, chats: list[dict]) -> dict:
    now = app.dispatch.now().astimezone(ZoneInfo(app.config.timezone))
    habits = read_habits(app.home)
    start = datetime.combine(now.date(), time.min, now.tzinfo)
    end = start + timedelta(days=1)
    sessions = await app.store.online_sessions(start, end)
    spans = []
    for entered, exited in sessions:
        a = _hour(entered.astimezone(now.tzinfo)) if entered > start else 0
        b = _hour(exited.astimezone(now.tzinfo)) if exited < end else 24
        if b > a:
            spans.append((a, b))
    arcs = _online_arcs(spans)
    counts = await app.store.hourly_message_counts(tz=now.tzinfo, now=now)
    maximum = max(counts)
    ticks = [
        (hour, count, _point(hour + .5, 142), _point(hour + .5, 146 + 14 * count / maximum))
        for hour, count in enumerate(counts) if count
    ]
    online = next((row for row in chats if row["state"] == "online"), None)
    sleeping = [row for row in chats if row["state"] == "asleep"]
    if online:
        title = "Online in " + ("your DM" if online["scope"] == "owner_dm" else online["title"])
        detail = f"until {online['when']}"
    elif sleeping:
        soonest = min(sleeping, key=lambda row: row["until"])
        title, detail = "Asleep", f"until {soonest['when']}"
    else:
        title, detail = "Offline", f"Local time {now:%H:%M}"
    return {
        "sleep_window": f"{habits.sleep_start:%H:%M} to {habits.sleep_end:%H:%M}",
        "sleep_arc": _arc(_hour(habits.sleep_start), _hour(habits.sleep_end)),
        "arcs": [_arc(a, b) for a, b in arcs if b - a < 24],
        "all_day": arcs == [[0, 24]],
        "ticks": ticks, "hours": [(h, _point(h, 104)) for h in (0, 6, 12, 18)],
        "hand": (_point(_hour(now), 116), _point(_hour(now), 152)),
        "clock": f"{now:%H:%M}", "title": title, "detail": detail, "today": await _today(app, now, start, end),
    }


def _import_key(row: dict) -> tuple[str, str] | None:
    if row["kind"] != "memory_fact" or not row["source"].startswith("import:"):
        return None
    return row["source"], row["body"].get("import_batch", "")


def _proposal_body(row: dict) -> dict:
    return {**row, "body": json.loads(row["body_json"])}


def _proposal_label(kind: str, source: str) -> str:
    if kind == "soul_edit":
        return {
            "reflection": "Soul edit, suggested in reflection", "dashboard": "Soul edit, asked for in the dashboard",
        }.get(source, "Soul edit")
    if kind == "memory_fact":
        service = source.partition("import:")[2]
        return f"Memory imported from {IMPORT_SERVICES.get(service, service)}" if service else "Memory fact"
    if kind == "skill":
        return "Skill proposed after a desktop task" if source == "desktop" else "Skill proposal"
    if kind == "lan_address":
        return "LAN address change"
    return "Plugin draft" if kind == "plugin" else kind.replace("_", " ").capitalize()


async def _proposals(app: App, kind: str = "", source: str = "") -> dict:
    rows = await app.proposals.pending()
    cards, imports = [], {}
    for raw in rows:
        if (kind and raw["kind"] != kind) or (source and raw["source"] != source):
            continue
        row = _proposal_body(raw) | {"label": _proposal_label(raw["kind"], raw["source"])}
        if row["kind"] == "plugin":
            row["review_path"] = proposals.plugin_review_path(row["body"])
        if row["kind"] == "skill" and row["body"]["old"]:
            row["changes"] = skill_diff(row["body"]["old"], row["body"]["new"])[2:]
        if (key := _import_key(row)) is not None:
            if key not in imports:
                service = key[0].partition(":")[2]
                imports[key] = {"id": row["id"], "service": IMPORT_SERVICES.get(service, service), "facts": []}
                cards.append(imports[key])
            imports[key]["facts"].append(row)
        else:
            cards.append(row)
    pending_chats = []
    if (not kind or kind == "chat_approval") and not source:
        for chat in await app.store.list_pending_chats():
            chat_id = int(chat["chat_id"])
            is_group = chat_id < 0
            who = chat.get("invited_by_handle") or ""
            pending_chats.append({
                "id": chat_id,
                "label": "Group approval" if is_group else "Stranger DM approval",
                "title": owner.display_title(chat, app.owner_id),
                "detail": (f"Invited by {who}" if is_group else f"From {who}") if who else "",
            })
    count = len(pending_chats) + sum(len(card["facts"]) if "facts" in card else 1 for card in cards)
    return {
        "cards": cards, "pending_chats": pending_chats, "count": count,
        "kind": kind, "source": source, "sources": sorted({row["source"] for row in rows}),
    }


def _usage_rows(rows: list[dict], field: str) -> list[tuple[str, int, float]]:
    counts, costs = defaultdict(int), defaultdict(float)
    for row in rows:
        counts[row[field]] += row["calls"]
        costs[row[field]] += row["cost"]
    return [(name, counts[name], costs[name]) for name in sorted(costs, key=lambda name: (-costs[name], name))]


async def _usage(app: App) -> dict:
    days, rows = await app.dispatch.usage.last_30_days(ZoneInfo(app.config.timezone))
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["day"]].append(row)
    today = by_day[days[-1]]
    sums = [sum(row["cost"] for row in by_day[day]) for day in days]
    peak = max(sums) or 1
    cap = app.config.daily_spend_cap_usd
    ratio = min(sums[-1] / cap, 1) if cap else 0
    bars = []
    for day, spent in zip(days, sums):
        height = 68
        parts = []
        for kind, _, cost in _usage_rows(by_day[day], "kind"):
            size = round(62 * (cost / peak), 3)
            height -= size
            parts.append((kind, round(height, 3), size))
        bars.append({
            "date": day, "parts": parts, "fallbacks": sum(row["fallbacks"] for row in by_day[day]),
            "spent": spent,
        })
    return {
        "spent": sums[-1], "cap": cap, "ratio": ratio,
        "tone": "signal" if ratio >= 1 else "amber" if ratio >= .7 else "arc",
        "kinds": KINDS, "today_kinds": _usage_rows(today, "kind"), "today_models": _usage_rows(today, "model"),
        "month_kinds": _usage_rows(rows, "kind"), "month_models": _usage_rows(rows, "model"),
        "bars": bars, "fallbacks": sum(day["fallbacks"] for day in bars),
    }


async def _turns(app: App, chat: str = "") -> dict:
    tz = ZoneInfo(app.config.timezone)
    known = {row["chat_id"]: row for row in await app.store.list_chats()}
    known.update({row["chat_id"]: row for row in await app.store.list_approved_chats()})
    rows = await app.store.recent_turns(chat or None)
    for row in rows:
        key = row["chat_id"]
        known.setdefault(key, {"chat_id": key, "title": ""})
        row["time"] = datetime.fromisoformat(row["at"]).astimezone(tz)
        row["title"] = _title(known[key], app.owner_id)
        row["steps"] = json.loads(row["trace"])
    return {
        "rows": rows, "chat": chat, "choices": [(key, _title(row, app.owner_id)) for key, row in known.items()],
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from dom.web import desktop as web_desktop

    app = request.app.state.dom
    if app.owner_id is None:
        return render("dashboard.html", active="setup")
    chats = await _chats(app)
    return render(
        "dashboard.html", active="now", dial=await _dial(app, chats["rows"]), chats=chats,
        desktop=await web_desktop.desktop_context(app),
        proposals=await _proposals(app), usage=await _usage(app), memory=await _memory(app), turns=await _turns(app),
        plugin_panels=await _plugin_panels(app),
    )


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request):
    return panel(request, "chats", **await _chats(owner_app(request)))


@router.post("/chats/all/preset", response_class=HTMLResponse)
async def set_everywhere_preset(request: Request):
    app = owner_app(request)
    name = (await form(request)).get("preset", [""])[0]
    message = await owner.set_global_preset(app, name, WHO) or "Preset unavailable."
    return render("chats.html", context=await _chats(app) | {"message": message})


@router.post("/chats/{chat_id}/traits/reset", response_class=HTMLResponse)
async def reset_chat_traits(request: Request, chat_id: int):
    app = owner_app(request)
    chat = await owner.approved_chat(app, chat_id)
    message = await owner.reset_traits(app, chat) if chat is not None else None
    return render("chats.html", context=await _chats(app) | {
        "message": message or "Traits or chat unavailable.", "open_traits": chat_id,
    })


@router.post("/chats/{chat_id}/{action}", response_class=HTMLResponse)
async def chat_action(request: Request, chat_id: int, action: Literal["wake", "sleep", "leave", "preset", "traits"]):
    app = owner_app(request)
    wake = None
    open_traits = None
    if action == "leave":
        message, _ = await owner.leave_chat(app, chat_id, WHO)
    elif action == "preset":
        chat = await owner.approved_chat(app, chat_id)
        name = (await form(request)).get("preset", [""])[0]
        message = await owner.set_preset(app, chat, name) if chat is not None else None
        message = message or "Preset or chat unavailable."
    elif action == "traits":
        chat = await owner.approved_chat(app, chat_id)
        data = await form(request)
        raw = {key: values[0] for key, values in data.items() if values}
        message = await owner.set_traits(app, chat, raw) if chat is not None else None
        message = message or "Traits or chat unavailable."
        open_traits = chat_id
    else:
        result = await owner.override_chat(app, chat_id, f"/{action}", WHO)
        message = result.message if result else "Unknown chat."
        if action == "wake" and result:
            wake = chat_id
    return render("chats.html", context=await _chats(app) | {
        "message": message, "woke": wake, "open_traits": open_traits,
    })


@router.post("/stickers", response_class=HTMLResponse)
async def edit_stickers(request: Request):
    app = owner_app(request)
    data = await form(request)
    try:
        message = await owner.change_sticker_set(app, data.get("action", [""])[0], data.get("name", [""])[0])
    except ValueError as exc:
        message = str(exc)
    return render("chats.html", context=await _chats(app) | {"message": message})


@router.get("/proposals", response_class=HTMLResponse)
async def proposals_page(request: Request, kind: str = "", source: str = ""):
    return panel(request, "proposals", **await _proposals(owner_app(request), kind, source))


@router.post("/proposals/chat/{chat_id}/{decision}", response_class=HTMLResponse)
async def decide_pending_chat(
    request: Request, chat_id: int, decision: Literal["approve", "ignore"], kind: str = "", source: str = "",
):
    app = owner_app(request)
    chat, label = await owner.decide_pending_chat(app, chat_id, decision, WHO)
    message = f"{label}: {owner.display_title(chat, app.owner_id)}" if chat is not None else "Already handled."
    response = render("proposals.html", context=await _proposals(app, kind, source) | {"message": message})
    if chat is not None:
        response.headers["X-Dom-Decided"] = "true"
    return response


@router.post("/proposals/{proposal_id:int}/{decision}", response_class=HTMLResponse)
async def decide_proposal(
    request: Request, proposal_id: int, decision: Literal["approve", "reject"], kind: str = "", source: str = "",
):
    app = owner_app(request)
    if not 0 < proposal_id < 2**63:
        raise HTTPException(404)
    message = await app.proposals.decide(proposal_id, decision == "approve")
    response = render("proposals.html", context=await _proposals(app, kind, source) | {"message": message})
    if message in ("Approved", "Rejected"):
        response.headers["X-Dom-Decided"] = "true"
    return response


@router.post("/proposals/import/{proposal_id:int}", response_class=HTMLResponse)
async def decide_import(request: Request, proposal_id: int, kind: str = "", source: str = ""):
    app = owner_app(request)
    rows = [_proposal_body(row) for row in await app.proposals.pending()]
    anchor = next((row for row in rows if row["id"] == proposal_id), None)
    if anchor is None or (key := _import_key(anchor)) is None:
        return render("proposals.html", context=await _proposals(app, kind, source) | {"message": "Already handled."})
    data = await form(request)
    decision = data.get("decision", [""])[0]
    if decision not in ("selected", "all", "reject"):
        raise HTTPException(400, "Choose an import action.")
    selected = set(data.get("selected", []))
    results = [
        await app.proposals.decide(row["id"], decision != "reject")
        for row in rows if _import_key(row) == key and (decision != "selected" or str(row["id"]) in selected)
    ]
    label = "Rejected" if decision == "reject" else "Approved"
    errors = [result for result in results if result not in (label, "already handled")]
    message = f"{label} {results.count(label)}."
    if errors:
        message += f" {len(errors)} still pending. {errors[0]}"
    response = render("proposals.html", context=await _proposals(app, kind, source) | {"message": message})
    if len(results) == sum(_import_key(row) == key for row in rows) and not errors:
        response.headers["X-Dom-Decided"] = "true"
    return response


async def _plugin_panels(app: App) -> list[tuple[str, object]]:
    return [(plugin.manifest.name, declared) for plugin, declared in await app.plugins.panel_entries()]


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    app = owner_app(request)
    return panel(request, "plugins", rows=await app.plugins.listing(), panels=await _plugin_panels(app))


@router.post("/plugin-panels/{name}/{panel_id}", response_class=HTMLResponse)
async def plugin_panel(request: Request, name: str, panel_id: str):
    """Run one approved panel. POST, so the dashboard's same-origin htmx check guards every plugin run."""
    app = owner_app(request)
    found = await surfaces.find_panel(app, name, panel_id)
    if found is None:
        raise HTTPException(404)
    plugin, declared = found
    data, error = await surfaces.run_panel(app, plugin, declared)
    return render("plugin_panel.html", name=plugin.manifest.name, panel=declared, data=data, error=error)


@router.get("/plugins/{name}/{digest}", response_class=HTMLResponse)
async def plugin_review(request: Request, name: str, digest: str):
    app = owner_app(request)
    try:
        review = await app.plugins.review(name, digest)
    except ValueError:
        raise HTTPException(404) from None
    if review is None:
        raise HTTPException(404)
    return panel(request, "plugins", review=review)


@router.post("/plugins/{name}/{digest}/reviewed", response_class=HTMLResponse)
async def plugin_reviewed(request: Request, name: str, digest: str):
    """Record that the owner pressed "I've read this code". Opening the page records nothing."""
    app = owner_app(request)
    try:
        review = await app.plugins.review(name, digest)
        if review is None:
            raise HTTPException(404)
        await app.plugins.mark_reviewed(name, digest)
        review = await app.plugins.review(name, digest)
    except ValueError:
        raise HTTPException(404) from None
    if review is None:
        raise HTTPException(404)
    return render("plugins.html", context={"review": review, "message": "Review recorded."})


@router.post("/plugins/{name}/{digest}/{decision}", response_class=HTMLResponse)
async def plugin_action(request: Request, name: str, digest: str, decision: Literal["approve", "reject", "disable"]):
    app = owner_app(request)
    message = await app.plugins.decide(name, digest, decision)
    response = render("plugins.html", context={
        "rows": await app.plugins.listing(), "panels": await _plugin_panels(app), "message": message,
    })
    if message not in ("Approved", "Rejected", "Disabled", "already handled"):
        response.status_code = 409
    return response


@router.get("/briefs", response_class=HTMLResponse)
async def briefs_page(request: Request):
    app = owner_app(request)
    try:
        briefs = await asyncio.to_thread(list_briefs, app.home)
    except (OSError, ValueError, RuntimeError):
        return panel(request, "briefs", rows=[], message="Could not read the briefs folder.")
    return panel(request, "briefs", rows=[(brief, markdown_blocks(brief.markdown)) for brief in briefs])


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    return panel(request, "usage", **await _usage(owner_app(request)))


@router.get("/turns", response_class=HTMLResponse)
async def turns_page(request: Request, chat: str = ""):
    return panel(request, "turns", **await _turns(owner_app(request), chat))


MEMORY = "memory/MEMORY.md"
HABITS = "memory/HABITS.md"
SOUL = "SOUL.md"
SOUL_INSTRUCTIONS = (
    "Suggest one small change to Dom's SOUL.md in response to the owner's request. "
    "Return only a JSON object with old and new string fields. The old text must occur "
    "exactly once in SOUL.md. The new text replaces just those characters. "
    f"Each string may contain at most {MAX_SOUL_EDIT} characters. "
    "Keep private details from the request out of the proposed text, since SOUL.md is shared with other chats."
)


async def _memory(app: App, file: str = MEMORY, *, soul_chat: bool = False) -> dict:
    people, chats = app.home.list_memory_notes()
    known = {c["chat_id"]: c for c in await app.store.list_chats()}
    known.update({c["chat_id"]: c for c in await app.store.list_approved_chats()})
    chat_notes, owner_notes = [], []
    for rel in chats:
        chat_id = Path(rel).stem
        if chat_id == str(app.owner_id):
            owner_notes.append((rel, "Your DM notes"))
        else:
            chat = known.get(chat_id, {"chat_id": chat_id, "title": ""})
            label = _title(chat, app.owner_id) if chat_id.startswith("-") else f"Friend DM · {_title(chat, app.owner_id)}"
            chat_notes.append((rel, label))
    sections = [
        ("Shared memory", [(SOUL, SOUL), (MEMORY, "MEMORY.md"), (HABITS, "HABITS.md"), ("memory/JOURNAL.md", "Journal")]),
        ("People", [(rel, Path(rel).name) for rel in people]),
        ("Chats", chat_notes),
        ("Only in your DM", [
            ("private/USER.md", "USER.md"), ("private/NOTES.md", "NOTES.md"),
            ("private/JOURNAL.md", "Private journal"), *owner_notes,
        ]),
    ]
    labels = {rel: label for _, files in sections for rel, label in files}
    if file not in labels:
        raise HTTPException(404, "Unknown memory file.")
    home = app.home.scoped(allow_private=file.startswith("private/"))
    try:
        content = home.read_memory_file(file)
    except (OSError, ValueError):
        raise HTTPException(404, "Memory file unavailable.") from None
    return {
        "sections": sections, "file": file, "label": labels[file], "readonly": file == HABITS,
        "limit": MAX_SOUL if file == SOUL else limit_for(file), "soul_chat": soul_chat,
        "content": content, "version": content_version(content),
        "import_services": IMPORT_SERVICES,
    }


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request, file: str = MEMORY, talk: bool = False):
    return panel(request, "memory", **await _memory(owner_app(request), SOUL if talk else file, soul_chat=talk))


@router.post("/memory/save", response_class=HTMLResponse)
async def save_memory(request: Request):
    app = owner_app(request)
    data = await form(request, max_bytes=1024 * 1024)
    file = data.get("file", [""])[0]
    if file == HABITS:
        raise HTTPException(403, "HABITS.md is read-only.")
    context = await _memory(app, file)
    if "content" not in data or "version" not in data:
        raise HTTPException(400, "Expected the edited text and file version.")
    content = data["content"][0].replace("\r\n", "\n").replace("\r", "\n")
    version = data["version"][0]
    home = app.home.scoped(allow_private=file.startswith("private/"))
    try:
        result = home.save_memory(file, content, version)
    except (OSError, ValueError):
        log.exception("could not save a dashboard memory file")
        return render("memory.html", context=context | {
            "error": "Couldn't save this file. Check that Dom can write to it.",
            "content": content, "version": version,
        })
    if not result.ok:
        return render("memory.html", context=context | {"error": result.error, "content": content, "version": version})
    label = context["label"]
    await owner.record_event(app, f"Saved {file} in the dashboard")
    return render("memory.html", context=context | {
        "message": f"Saved {label}.", "content": result.content, "version": content_version(result.content),
    })


@router.post("/memory/soul", response_class=HTMLResponse)
async def soul_chat(request: Request):
    app = owner_app(request)
    context = await _memory(app, SOUL, soul_chat=True)
    data = await form(request, max_bytes=MAX_SOUL_EDIT * 12 + 1024)
    text = data.get("message", [""])[0].strip()
    if not text or len(text) > MAX_SOUL_EDIT:
        return render("memory.html", context=context | {
            "error": f"Enter a message of 1 to {MAX_SOUL_EDIT:,} characters.",
        })
    if len(context["content"]) > MAX_SOUL:
        return render("memory.html", context=context | {"error": "Shorten SOUL.md before asking for a proposal."})
    messages = [
        {"role": "system", "content": SOUL_INSTRUCTIONS},
        {"role": "user", "content": json.dumps({"soul": context["content"], "request": text}, ensure_ascii=False)},
    ]
    chat_id = app.owner_id
    try:
        async with asyncio.timeout(30):
            with model_request("chat", chat_id, partial(app.dispatch.turn_allowed, chat_id, "owner_dm")):
                await check_model_allowed()
                reply = await app.client.complete(messages, [], once=True)
        edit = json.loads(unfence(reply.get("content")))
        if (
            not isinstance(edit, dict) or edit.keys() != {"old", "new"}
            or any(not isinstance(value, str) for value in edit.values()) or edit["old"] == edit["new"]
        ):
            raise ValueError("Dom didn't return a replacement. Try asking for one specific change.")
        proposal_id = await app.proposals.create("soul_edit", "Soul edit from the dashboard", edit, "dashboard", notify=False)
    except ValueError as exc:
        error = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Dom didn't return an edit. Try again."
        return render("memory.html", context=context | {"error": error, "request_text": text})
    except Exception as exc:
        log.warning("dashboard soul chat failed (%s)", type(exc).__name__)
        return render("memory.html", context=context | {
            "error": "Couldn't get a proposal from Dom. Try again.", "request_text": text,
        })
    await owner.record_event(app, f"Dom proposed a soul edit (#{proposal_id}) from the dashboard")
    return render("memory.html", context=context | {
        "request_text": text, "edit": edit, "proposal_id": proposal_id,
    })


IMPORT_FORM_OVERHEAD = 16 * 1024  # multipart boundaries and the service field around the file


@router.post("/memory/import", response_class=HTMLResponse)
async def import_memories(request: Request):
    """The dashboard's /import: the same MemoryImport.run and the same document limits as Telegram."""
    app = owner_app(request)
    context = await _memory(app, MEMORY) | {"import_open": True}
    service = ""
    try:
        try:
            data = await multipart_form(request, max_bytes=memory_import.MAX_DOCUMENT_BYTES + IMPORT_FORM_OVERHEAD)
        except HTTPException as exc:
            if exc.status_code != 413:
                raise
            raise ValueError(memory_import.DOCUMENT_LIMIT_ERROR) from None
        try:
            service = str(data.get("service") or "").strip().lower()
            upload = data.get("file")
            if service not in memory_import.SERVICES:
                raise ValueError("Choose ChatGPT, Claude or Muse.")
            if not isinstance(upload, UploadFile):
                raise ValueError("Send a .txt or .md document for the import.")
            memory_import.validate_document_meta(upload.filename, upload.size)
            text = memory_import.decode_document(await upload.read(memory_import.MAX_DOCUMENT_BYTES + 1))
        finally:
            await data.close()
        parsed = await app.memory_import.run(service, text)
    except (OSError, ValueError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else "Could not read the import or existing notes."
        return render("memory.html", context=context | {"import_service": service, "import_error": error})
    message = memory_import.summary(service, parsed)
    await owner.record_event(app, message)
    return render("memory.html", context=context | {
        "import_service": service, "import_message": message, "import_count": len(parsed.facts),
    })

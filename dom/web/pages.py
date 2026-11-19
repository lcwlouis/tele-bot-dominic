"""Server-rendered dashboard pages and the owner's htmx actions."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dom import traits
from dom.presence import read_habits
from dom.telegram import owner

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
templates = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(("html",)),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates.filters.update(money=lambda amount: f"${amount:,.2f}", urlencode_query=urlencode)
WHO = "The owner"
KINDS = {
    "chat": "Replies", "triage": "Triage", "media": "Photos and voice", "memory": "Memory",
    "search": "Web search", "reflection": "Reflection", "guard": "Leak check",
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


def _title(chat: dict, owner_id: int | None) -> str:
    title = owner.display_title(chat, owner_id)
    return "Your DM" if chat["chat_id"] == str(owner_id) else title


async def _chats(app: App) -> dict:
    rows = await app.store.list_approved_chats()
    tz = ZoneInfo(app.config.timezone)
    chats = []
    for row, (state, until) in zip(rows, app.dispatch.peek_presence(rows)):
        chat_id = int(row["chat_id"])
        sliders, preset = await traits.for_chat(app.home, app.store, chat_id)
        unread, _ = await app.store.get_unread(chat_id)
        chats.append({
            "id": chat_id, "title": _title(row, app.owner_id), "state": state, "until": until,
            "when": until.astimezone(tz).strftime("%H:%M") if until else "",
            "scope": app.store.scope_for(chat_id, "group" if chat_id < 0 else "private"),
            "unread": unread, "traits": sliders, "preset": preset,
        })
    presets = [preset for name in app.home.preset_names() if (preset := traits.load_preset(app.home, name))]
    pending = await app.store.list_pending_chats()
    return {
        "rows": chats, "presets": presets, "pending": len(pending),
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
    arcs: list[list[float]] = []
    for a, b in sorted(spans):
        if arcs and a < arcs[-1][1]:
            arcs[-1][1] = max(arcs[-1][1], b)
        else:
            arcs.append([a, b])
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
        "clock": f"{now:%H:%M}", "title": title, "detail": detail,
    }


def _import_key(row: dict) -> tuple[str, str] | None:
    if row["kind"] != "memory_fact" or not row["source"].startswith("import:"):
        return None
    return row["source"], row["body"].get("import_batch", "")


def _proposal_body(row: dict) -> dict:
    return {**row, "body": json.loads(row["body_json"])}


async def _proposals(app: App, kind: str = "", source: str = "") -> dict:
    rows = await app.proposals.pending()
    cards, imports = [], {}
    for raw in rows:
        if (kind and raw["kind"] != kind) or (source and raw["source"] != source):
            continue
        row = _proposal_body(raw)
        if (key := _import_key(row)) is not None:
            if key not in imports:
                service = key[0].partition(":")[2]
                imports[key] = {"id": row["id"], "service": IMPORT_SERVICES.get(service, service), "facts": []}
                cards.append(imports[key])
            imports[key]["facts"].append(row)
        else:
            cards.append(row)
    return {
        "cards": cards, "count": sum(len(card["facts"]) if "facts" in card else 1 for card in cards),
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
    app = request.app.state.dom
    if app.owner_id is None:
        return render("dashboard.html", active="setup")
    chats = await _chats(app)
    return render(
        "dashboard.html", active="now", dial=await _dial(app, chats["rows"]), chats=chats,
        proposals=await _proposals(app), usage=await _usage(app), turns=await _turns(app),
    )


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request):
    return panel(request, "chats", **await _chats(owner_app(request)))


@router.post("/chats/{chat_id}/{action}", response_class=HTMLResponse)
async def chat_action(request: Request, chat_id: int, action: Literal["wake", "sleep", "leave", "preset"]):
    app = owner_app(request)
    wake = None
    if action == "leave":
        message, _ = await owner.leave_chat(app, chat_id, WHO)
    elif action == "preset":
        chat = next((c for c in await app.store.list_chats("approved") if c["chat_id"] == str(chat_id)), None)
        name = (await form(request)).get("preset", [""])[0]
        message = await owner.set_preset(app, chat, name) if chat is not None else None
        message = message or "Preset or chat unavailable."
    else:
        result = await owner.override_chat(app, chat_id, f"/{action}", WHO)
        message = result.message if result else "Unknown chat."
        if action == "wake" and result:
            wake = chat_id
    return render("chats.html", context=await _chats(app) | {"message": message, "woke": wake})


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


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    return panel(request, "usage", **await _usage(owner_app(request)))


@router.get("/turns", response_class=HTMLResponse)
async def turns_page(request: Request, chat: str = ""):
    return panel(request, "turns", **await _turns(owner_app(request), chat))

"""Owner settings JSON contract: spend limits and the leak scan level (owner decisions E86 and E77).

GET /api/spend returns SpendView: today against the daily cap, the calendar month so far against the monthly cap
(both in the owner's timezone), each area with its own limit, what is left, and a 30-day daily series split by area
for a chart. Every figure is read from the recorded calls, so a cost that is reported late shows once it is set.
POST /api/spend/caps takes SpendCapsSave (every field, with the GET revision), validates it, writes only the values
that changed to config.toml like every other settings save, and applies at once; it returns SpendView.

GET /api/leak-scan returns LeakScanView and POST /api/leak-scan takes LeakScanSave: strict, standard, relaxed or
off. Choosing a level drops a legacy `leak_threshold` from config.toml, and any level but off also turns the leak
check on. Both routes are owner only, and every POST is a same-origin JSON write like the other settings routes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.config import LEAK_SCAN_CUTOFFS, SPEND_AREAS, LeakScan, SpendArea
from dom.usage import area_of
from dom.web.model_settings import REVISION, Snapshot, _input, _snapshot, save_values
from dom.web.shared import owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
SPEND_PATH = "/api/spend"
CAPS_PATH = SPEND_PATH + "/caps"
LEAK_PATH = "/api/leak-scan"
JSON_POSTS = {CAPS_PATH, LEAK_PATH}
MAX_CAP_USD = 100_000.0
AREA_LABELS: dict[SpendArea, str] = {
    "chat": "Chat and replies", "media": "Photos, voice and video", "search": "Web search",
    "desktop": "Desktop tasks", "memory": "Memory and summaries",
}
AREA_HELP: dict[SpendArea, str] = {
    "chat": "Replies, triage, the leak check and escalations.",
    "media": "Reading attachments, and voice notes, photos and video made by the studio or its cloud backups.",
    "search": "Web search, and the overnight news Dom reads from it.",
    "desktop": "Everything the desktop worker's model calls cost.",
    "memory": "The memory pass, reflection, summaries and embeddings.",
}
FIELD_HELP = {
    "daily_spend_cap_usd": "The normal daily pace. Once today's paid calls reach it, friends' chats and background work "
                           "pause until midnight. Zero turns the daily cap off.",
    "monthly_spend_cap_usd": "A hard ceiling for the calendar month, over every paid call: chat, media, search, desktop "
                             "tasks, embeddings, summaries and studio cloud backups. One-offs such as event days draw only "
                             "on the month's unused budget and never pass it. Zero turns the monthly cap off.",
    "cap_owner_dm": "Off, your own DM is never paused by a cap, though what it spends still counts. On, your DM obeys "
                    "both caps like everyone else's.",
    "area_caps": "Optional monthly limit for one area. An area at its limit pauses that kind of work until the 1st "
                 "while the others go on. Leave it empty for no limit.",
}


class Money(BaseModel):
    spent: float
    cap: float | None  # None: no cap set
    left: float | None


class AreaView(Money):
    area: SpendArea
    label: str
    help: str


class DayView(BaseModel):
    date: str
    spent: float
    areas: dict[SpendArea, float]


class SpendView(BaseModel):
    revision: str
    timezone: str
    today: Money
    month: Money
    month_label: str  # YYYY-MM
    month_ends: str  # the first day of next month, where the monthly cap resets
    cap_owner_dm: bool
    areas: list[AreaView]
    daily: list[DayView]  # the last 30 days, oldest first
    help: dict[str, str]
    # What one-offs such as event days may still draw this month; None while no limit applies.
    one_off_room: float | None


def _money(spent: float, cap: float) -> Money:
    return Money(spent=round(spent, 6), cap=cap or None, left=round(max(0.0, cap - spent), 6) if cap else None)


async def _view(app: App, snapshot: Snapshot) -> SpendView:
    config = snapshot.config
    usage = app.dispatch.usage
    tz = ZoneInfo(config.timezone)
    _, today = await usage.today(tz)
    by_area = await usage.month_by_area(tz)
    days, rows = await usage.last_30_days(tz)
    series: dict[str, dict[SpendArea, float]] = {day: dict.fromkeys(SPEND_AREAS, 0.0) for day in days}
    for row in rows:
        series[row["day"]][area_of(row["kind"])] += row["cost"]
    start, end = usage.month_bounds(tz)
    return SpendView(
        revision=snapshot.revision, timezone=config.timezone,
        today=_money(today, config.daily_spend_cap_usd),
        month=_money(sum(by_area.values()), config.monthly_spend_cap_usd),
        month_label=start.strftime("%Y-%m"), month_ends=end.date().isoformat(),
        cap_owner_dm=config.cap_owner_dm,
        areas=[
            AreaView(
                area=area, label=AREA_LABELS[area], help=AREA_HELP[area],
                **_money(by_area[area], config.area_spend_caps_usd.get(area, 0)).model_dump(),
            ) for area in SPEND_AREAS
        ],
        daily=[DayView(date=day, spent=round(sum(series[day].values()), 6),
                       areas={area: round(cost, 6) for area, cost in series[day].items()}) for day in days],
        help=FIELD_HELP, one_off_room=await usage.one_off_room(config),
    )


Cap = Field(ge=0, le=MAX_CAP_USD, allow_inf_nan=False)  # dollars; zero turns a cap off


class SpendCapsSave(BaseModel):
    """Every cap, so an omitted one cannot silently reset a saved value. A zero or null area limit means none."""

    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    daily_spend_cap_usd: float = Cap
    monthly_spend_cap_usd: float = Cap
    cap_owner_dm: bool
    area_caps: dict[SpendArea, Annotated[float, Cap] | None]

    def check(self) -> None:
        if set(self.area_caps) != set(SPEND_AREAS):
            raise HTTPException(422, "Give every area a limit, or null for none.")
        if self.monthly_spend_cap_usd and self.daily_spend_cap_usd > self.monthly_spend_cap_usd:
            raise HTTPException(422, "The monthly cap must be at least the daily cap.")


@router.get(SPEND_PATH)
async def read_spend(request: Request) -> SpendView:
    app = owner_app(request)
    return await _view(app, await asyncio.to_thread(_snapshot, app.config.home))


@router.post(CAPS_PATH)
async def save_caps(request: Request) -> SpendView:
    """Save the caps. Every spend check reads them from the running config, so a save applies at once."""
    app = owner_app(request)
    edit = await _input(request, SpendCapsSave)
    edit.check()
    changes: dict[tuple[str, ...], bool | float | None] = {
        ("daily_spend_cap_usd",): edit.daily_spend_cap_usd,
        ("monthly_spend_cap_usd",): edit.monthly_spend_cap_usd,
        ("cap_owner_dm",): edit.cap_owner_dm,
        **{("area_spend_caps_usd", area): cap or None for area, cap in edit.area_caps.items()},
    }
    saved = await save_values(
        app, edit.revision, changes,
        lambda config: {key: getattr(config, key) for key in (
            "daily_spend_cap_usd", "monthly_spend_cap_usd", "cap_owner_dm", "area_spend_caps_usd",
        )},
        "Could not save the spend limits. Check config.toml and its permissions.",
    )
    return await _view(app, saved)


LEVELS = (
    ("strict", "Strict", "Holds a message that only hints at something private."),
    ("standard", "Standard", "Holds a message that goes past a vague allusion. The default."),
    ("relaxed", "Relaxed", "Holds only a message that reveals a private fact."),
    ("off", "Off", "Skips the Jev check on outgoing messages and online plugin calls."),
)
LEAK_HELP = (
    "Before a reply to a friend or group, or an online plugin call, leaves, Jev scores 0 to 3 how much private "
    "information it reveals: 1 is a vague allusion, 2 reveals a private fact, 3 reveals several or sensitive facts. "
    "The level sets the score that holds the message back (Strict 0.7, Standard 1.2, Relaxed 2.0). Off skips the Jev "
    "check only: the local check for credentials and verbatim private notes in public files and plugin inputs always "
    "stays on."
)


class LeakLevel(BaseModel):
    id: LeakScan
    label: str
    help: str
    cutoff: float | None


class LeakScanView(BaseModel):
    revision: str
    level: Literal["strict", "standard", "relaxed", "off", "custom"]  # custom: a legacy `leak_threshold` between levels
    cutoff: float | None  # the score that holds a message back now; None while the Jev check is off
    levels: list[LeakLevel]
    available: bool  # Jev (the triage key) is set up, so the check can run
    help: str


class LeakScanSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    level: LeakScan


def _leak_view(app: App, snapshot: Snapshot) -> LeakScanView:
    config = snapshot.config
    return LeakScanView(
        revision=snapshot.revision, level=config.leak_level, cutoff=config.leak_cutoff,
        levels=[LeakLevel(id=id_, label=label, help=help_, cutoff=LEAK_SCAN_CUTOFFS.get(id_)) for id_, label, help_ in LEVELS],
        available=app.jev is not None, help=LEAK_HELP,
    )


@router.get(LEAK_PATH)
async def read_leak_scan(request: Request) -> LeakScanView:
    app = owner_app(request)
    return _leak_view(app, await asyncio.to_thread(_snapshot, app.config.home))


@router.post(LEAK_PATH)
async def save_leak_scan(request: Request) -> LeakScanView:
    """Set the level. Both leak checks read it from the running config on each message, so it applies at once."""
    app = owner_app(request)
    edit = await _input(request, LeakScanSave)
    changes: dict[tuple[str, ...], bool | str | None] = {("leak_scan",): edit.level, ("leak_threshold",): None}
    if edit.level != "off":
        changes[("leak_check",)] = True
    saved = await save_values(
        app, edit.revision, changes,
        lambda config: {key: getattr(config, key) for key in ("leak_scan", "leak_threshold", "leak_check")},
        "Could not save the leak scan level. Check config.toml and its permissions.",
    )
    return _leak_view(app, saved)

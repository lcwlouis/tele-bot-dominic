"""Event days: a special day in Dom's own life on which he may raise his media limits.

Dom declares one through his owner-DM tool (a trip, a concert, his birthday). That multiplies every per-chat media limit
and the studio-wide daily total for that calendar day (Dom's time zone) by up to the owner's ceiling
`studio.event_day_max_multiplier`. The owner sets how many a week are allowed, and can cancel one. Each item beyond the
normal limits must also fit `Usage.may_spend_one_off`, so an event day draws only on the month's unused budget and never
passes the monthly cap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dom.studio.policy import Kind

if TYPE_CHECKING:
    from dom.config import Config
    from dom.store import Store
    from dom.usage import Usage

log = logging.getLogger(__name__)
# What one extra item is assumed to cost against the month's unused budget, before its real cost is known. A local
# studio costs nothing; these cover the cloud backups.
EXTRA_COST_USD: dict[Kind, float] = {"voice": 0.02, "photo": 0.05, "video_note": 0.5, "special": 1.0}
MAX_AHEAD_DAYS = 14
TITLE_MAX = 80


class EventDays:
    def __init__(self, store: Store, config: Callable[[], Config], usage: Callable[[], Usage]):
        self.store, self._config, self._usage = store, config, usage

    def local_date(self, now: datetime) -> date:
        return now.astimezone(ZoneInfo(self._config().timezone)).date()

    def ceiling(self) -> float:
        """The most an event day may multiply the limits by; 1 means event days are off."""
        return max(1.0, self._config().studio.event_day_max_multiplier)

    async def today(self, now: datetime) -> dict | None:
        """Today's event day, if one is on."""
        day = self.local_date(now).isoformat()
        if self.ceiling() <= 1:
            return None
        return next((row for row in await self.store.event_days(day, cancelled=False) if row["day"] == day), None)

    async def multiplier(self, now: datetime) -> float:
        """How much today's event day raises the limits: 1 when none is on. The owner's current ceiling caps it."""
        try:
            row = await self.today(now)
        except Exception:
            log.exception("could not read the event days; the normal limits apply")
            return 1.0
        return 1.0 if row is None else max(1.0, min(float(row["multiplier"]), self.ceiling()))

    async def may_extra(self, kind: Kind) -> bool:
        """May one more item of `kind`, beyond its normal limit, be made? Only on the month's unused budget."""
        return await self._usage().may_spend_one_off(self._config(), EXTRA_COST_USD[kind], area="media")

    async def declare(self, title: str, now: datetime, day: str | None = None, multiplier: float | None = None) -> str:
        """Declare an event day (today unless `day` is a date up to two weeks ahead); returns what to tell Dom."""
        ceiling = self.ceiling()
        if ceiling <= 1:
            return "error: event days are turned off"
        title = " ".join(title.split())[:TITLE_MAX]
        if not title:
            return "error: say what the day is for"
        today = self.local_date(now)
        try:
            when = date.fromisoformat(day) if day else today
        except ValueError:
            return "error: day must be a date like 2026-12-24"
        if not today <= when <= today + timedelta(days=MAX_AHEAD_DAYS):
            return f"error: the day must be today or within the next {MAX_AHEAD_DAYS} days"
        factor = ceiling if multiplier is None else max(1.0, min(float(multiplier), ceiling))
        per_week = self._config().studio.event_days_per_week
        event_id, why = await self.store.declare_event_day(when.isoformat(), title, factor, now, per_week)
        if event_id is None:
            return f"error: {why}"
        log.info("event day declared for %s", when)
        await self.store.record_owner_event(f"Dom declared an event day on {when}: {title}")
        return f"ok: {when} is an event day ({title}); your media limits are up to {factor:g}× that day"

    async def listing(self, now: datetime) -> list[dict]:
        """Event days from a week ago on, for the dashboard, with whether each is on now."""
        since = (self.local_date(now) - timedelta(days=7)).isoformat()
        today = self.local_date(now).isoformat()
        return [
            {**row, "active": row["cancelled_at"] is None and row["day"] == today}
            for row in await self.store.event_days(since)
        ]

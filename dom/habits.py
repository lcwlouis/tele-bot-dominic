"""Learn a sleep window from hourly message counts without reading chat text."""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dom.presence import Habits, clamp_sleep_end, read_habits

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
DAY_MINUTES = 24 * 60
CHRONOTYPE_SHIFT = {"early": -60, "normal": 0, "late": 60}


def _distance(current: int, target: int) -> int:
    half_day = DAY_MINUTES // 2
    return (target - current + half_day) % DAY_MINUTES - half_day


def _move(current: int, target: int) -> int:
    return (current + min(30, max(-30, _distance(current, target)))) % DAY_MINUTES


def learn(counts: list[int], current: Habits, chronotype: str) -> tuple[time, time] | None:
    if sum(counts) < 50:
        return None

    start = current.sleep_start.hour * 60 + current.sleep_start.minute
    end = current.sleep_end.hour * 60 + current.sleep_end.minute
    hours = ((end - start) % DAY_MINUTES + 30) // 60
    circular_counts = counts + counts

    quietest = min(range(24), key=lambda hour: (
        sum(circular_counts[hour:hour + hours]), abs(_distance(start, hour * 60)),
    ))
    bias = CHRONOTYPE_SHIFT.get(chronotype, 0) if isinstance(chronotype, str) else 0
    target = quietest * 60 + bias
    new_start = _move(start, target)
    new_end = clamp_sleep_end(new_start, _move(end, target + hours * 60))
    return time(*divmod(new_start, 60)), time(*divmod(new_end, 60))


def due(local: datetime, habits: Habits) -> bool:
    return local.hour >= 12


async def run(app: App) -> None:
    home = app.home.scoped(allow_private=False)
    current = read_habits(home)
    meta, _ = home.soul()
    counts = await app.store.hourly_message_counts(tz=ZoneInfo(app.config.timezone), now=app.dispatch.now())
    window = learn(counts, current, meta.get("chronotype", "normal"))
    if window is None or window == (current.sleep_start, current.sleep_end):
        return
    start, end = window
    home.write_habits_line(start, end)
    if app.owner_id is not None:
        try:
            await app.store.record_owner_event(f"Dom's sleep moved to {start:%H:%M}–{end:%H:%M}")
        except Exception:
            log.exception("could not record the sleep change")

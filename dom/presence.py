"""Per-chat presence and the habits that govern its transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from random import Random
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from dom.store import Store

State = Literal["asleep", "offline", "online"]


@dataclass(frozen=True)
class Habits:
    sleep_start: time
    sleep_end: time
    online_minutes: tuple[int, int]
    offline_minutes: tuple[int, int]
    wake_after_messages: int
    kapoh_wake: float


def _range(value: str, default: tuple[int, int]) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", value)
    if match:
        lo, hi = map(int, match.groups())
        if 0 < lo <= hi:
            return lo, hi
    return default


def parse_habits(text: str) -> Habits:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.split("#", 1)[0].strip().strip("\"'")

    start, end = time(23, 59), time(7, 30)
    sleep = re.fullmatch(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", values.get("sleep", ""))
    if sleep:
        try:
            start, end = map(time.fromisoformat, sleep.groups())
        except ValueError:
            pass
    start_min = start.hour * 60 + start.minute
    duration = (end.hour * 60 + end.minute - start_min) % (24 * 60)
    end_min = (start_min + min(max(duration, 4 * 60), 11 * 60)) % (24 * 60)
    end = time(*divmod(end_min, 60))

    try:
        wake_after = int(values.get("wake_after_messages", "5"))
    except ValueError:
        wake_after = 5
    try:
        kapoh = float(values.get("kapoh_wake", "2.0"))
    except ValueError:
        kapoh = 2.0
    return Habits(
        start, end,
        _range(values.get("online_minutes", ""), (5, 25)),
        _range(values.get("offline_minutes", ""), (15, 90)),
        wake_after if wake_after > 0 else 5,
        kapoh if 0 <= kapoh <= 3 else 2.0,
    )


def _deadline(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return None
    return deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)


class Presence:
    def __init__(self, store: Store, tz: ZoneInfo, rng: Random, now: Callable[[], datetime]):
        self.store, self.tz, self.rng, self.now = store, tz, rng, now

    def _sleep_end(self, now: datetime, habits: Habits) -> datetime | None:
        local = now.astimezone(self.tz)
        start = datetime.combine(local.date(), habits.sleep_start, self.tz)
        end = datetime.combine(local.date(), habits.sleep_end, self.tz)
        if end <= start:
            if local >= start:
                end += timedelta(days=1)
            else:
                start -= timedelta(days=1)
        return end if start <= local < end else None

    async def state(self, chat_id, habits: Habits) -> tuple[State, datetime | None]:
        now = self.now()
        row = await self.store.get_presence(chat_id)
        online = _deadline(row["online_until"])
        sleep = _deadline(row["sleep_until"])
        offline = _deadline(row["offline_until"])

        # A matching sleep deadline on a non-sleeping row marks an owner wake.
        if online and online > now and sleep == online and not row["is_sleeping"]:
            return "online", online
        sleep_end = sleep if row["is_sleeping"] and sleep and sleep > now else self._sleep_end(now, habits)
        if sleep_end:
            if not row["is_sleeping"] or sleep != sleep_end:
                await self.store.set_presence(
                    chat_id, is_sleeping=True, sleep_until=sleep_end.isoformat(),
                    is_offline=False, offline_until=None, online_until=None,
                )
            return "asleep", sleep_end
        if online and online > now:
            return "online", online
        if online:
            offline = await self._go_offline(chat_id, habits, online)
        elif not row["is_offline"] or row["is_sleeping"]:
            await self.store.set_presence(chat_id, is_sleeping=False, sleep_until=None, is_offline=True)
        return "offline", offline

    async def is_online(self, chat_id, habits: Habits) -> bool:
        """Whether `state` would say online, without writing anything (safe outside the turn lock)."""
        now = self.now()
        row = await self.store.get_presence(chat_id)
        online = _deadline(row["online_until"])
        if online is None or online <= now:
            return False
        sleep = _deadline(row["sleep_until"])
        if sleep == online and not row["is_sleeping"]:
            return True
        sleeping = row["is_sleeping"] and sleep and sleep > now
        return not sleeping and self._sleep_end(now, habits) is None

    async def go_online(self, chat_id, habits: Habits, minutes: float | None = None) -> datetime:
        if minutes is None:
            lo, hi = habits.online_minutes
            minutes = self.rng.triangular(lo, hi, hi - 0.2 * (hi - lo))
        return await self._set_online(chat_id, self.now() + timedelta(minutes=minutes))

    async def _set_online(self, chat_id, until: datetime, override: bool = False) -> datetime:
        await self.store.set_presence(
            chat_id, is_sleeping=False, sleep_until=until.isoformat() if override else None,
            is_offline=False, offline_until=None, online_until=until.isoformat(),
        )
        return until

    async def go_offline(self, chat_id, habits: Habits) -> datetime:
        return await self._go_offline(chat_id, habits, self.now())

    async def _go_offline(self, chat_id, habits: Habits, since: datetime) -> datetime:
        lo, hi = habits.offline_minutes
        until = since + timedelta(minutes=self.rng.triangular(lo, hi, lo + 0.2 * (hi - lo)))
        await self.store.set_presence(
            chat_id, is_sleeping=False, sleep_until=None, is_offline=True,
            offline_until=until.isoformat(), online_until=None,
        )
        return until

    async def extend_online(self, chat_id, minutes: float) -> datetime:
        row = await self.store.get_presence(chat_id)
        online = _deadline(row["online_until"])
        now = self.now()
        if online is None or online <= now:
            raise ValueError("the chat is no longer online")
        until = online + timedelta(minutes=min(max(minutes, 0), 30))
        return await self._set_online(chat_id, until, override=_deadline(row["sleep_until"]) == online)

    async def wake(self, chat_id, habits: Habits, minutes: float = 30) -> datetime:
        return await self._set_online(chat_id, self.now() + timedelta(minutes=minutes), override=True)

    async def sleep(self, chat_id, habits: Habits) -> datetime:
        now = self.now().astimezone(self.tz)
        end = datetime.combine(now.date(), habits.sleep_end, self.tz)
        if end <= now:
            end += timedelta(days=1)
        await self.store.set_presence(
            chat_id, is_sleeping=True, sleep_until=end.isoformat(),
            is_offline=False, offline_until=None, online_until=None,
        )
        return end

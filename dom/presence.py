"""Per-chat presence and the habits that govern its transitions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from random import Random
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from dom.home import Home, PrivateAccessError
from dom.store import Store

State = Literal["asleep", "offline", "online"]
MAX_SESSION = timedelta(minutes=120)  # an online session, extensions included
MAX_SLEEP_COMMAND = timedelta(hours=8)


@dataclass(frozen=True)
class Habits:
    sleep_start: time
    sleep_end: time
    online_minutes: tuple[int, int]
    offline_minutes: tuple[int, int]
    wake_after_messages: int
    kapoh_wake: float


def _clamp(value, low, high):
    return min(max(value, low), high)


def _range(value: str, default: tuple[int, int], low: int, high: int) -> tuple[int, int]:
    """`lo-hi` with both ends clamped into [low, high]; anything malformed or out of order is `default`."""
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", value)
    if match:
        lo, hi = map(int, match.groups())
        if lo <= hi:
            return _clamp(lo, low, high), _clamp(hi, low, high)
    return default


def clamp_sleep_end(start_min: int, end_min: int) -> int:
    duration = (end_min - start_min) % (24 * 60)
    return (start_min + _clamp(duration, 4 * 60, 11 * 60)) % (24 * 60)


def read_habits(home: Home) -> Habits:
    try:
        text = home.scoped(allow_private=False).read("memory/HABITS.md")
    except (PrivateAccessError, ValueError):
        text = ""
    return parse_habits(text)


def parse_habits(text: str) -> Habits:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip().lstrip("-*").strip()] = value.split("#", 1)[0].strip().strip("\"'")

    start, end = time(23, 59), time(7, 30)
    sleep = re.fullmatch(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", values.get("sleep", ""))
    if sleep:
        try:
            start, end = map(time.fromisoformat, sleep.groups())
        except ValueError:
            pass
    start_min = start.hour * 60 + start.minute
    end_min = clamp_sleep_end(start_min, end.hour * 60 + end.minute)
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
        _range(values.get("online_minutes", ""), (5, 25), 1, 120),
        _range(values.get("offline_minutes", ""), (15, 90), 5, 600),
        _clamp(wake_after, 3, 50),
        _clamp(kapoh, 1.0, 3.0) if math.isfinite(kapoh) else 2.0,
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
        self._session_start: dict[int, datetime] = {}  # lost on restart; extensions then count from the next one
        self.on_session_end: Callable[[int], None] | None = None  # an online session ended (expired, slept, offline)

    def _session_ended(self, chat_id) -> None:
        if self.on_session_end is not None:
            self.on_session_end(int(chat_id))

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

    def _next_sleep_start(self, now: datetime, habits: Habits) -> datetime:
        local = now.astimezone(self.tz)
        start = datetime.combine(local.date(), habits.sleep_start, self.tz)
        return start if start > local else start + timedelta(days=1)

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
                    chat_id, observed_at=now, is_sleeping=True, sleep_until=sleep_end.isoformat(),
                    is_offline=False, offline_until=None, online_until=None,
                )
                if online:
                    self._session_ended(chat_id)
            return "asleep", sleep_end
        if online and online > now:
            return "online", online
        if online:
            offline = await self._go_offline(chat_id, habits, online)
        elif not row["is_offline"] or row["is_sleeping"]:
            await self.store.set_presence(chat_id, is_sleeping=False, sleep_until=None, is_offline=True)
        return "offline", offline

    def peek(self, row: dict, habits: Habits) -> tuple[State, datetime | None]:
        """What `state` would report for this presence row, without writing anything. The deadline is
        None when no future one is stored."""
        now = self.now()
        online, sleep, offline = _deadline(row["online_until"]), _deadline(row["sleep_until"]), _deadline(row["offline_until"])
        online = online if online and online > now else None
        if online and sleep == online and not row["is_sleeping"]:  # an owner wake
            return "online", online
        sleep_end = sleep if row["is_sleeping"] and sleep and sleep > now else self._sleep_end(now, habits)
        if sleep_end:
            return "asleep", sleep_end
        if online:
            return "online", online
        return "offline", offline if offline and offline > now else None

    async def is_online(self, chat_id, habits: Habits) -> bool:
        """Whether `state` would say online, without writing anything (safe outside the turn lock)."""
        return self.peek(await self.store.get_presence(chat_id), habits)[0] == "online"

    def session_minutes(self, habits: Habits) -> float:
        """Draw how long a new online session lasts, without starting one."""
        lo, hi = habits.online_minutes
        return self.rng.triangular(lo, hi, hi - 0.2 * (hi - lo))

    async def go_online(self, chat_id, habits: Habits, minutes: float | None = None) -> datetime:
        if minutes is None:
            minutes = self.session_minutes(habits)
        now = self.now()
        self._session_start[chat_id] = now
        return await self._set_online(chat_id, now + timedelta(minutes=minutes), start_session=True)

    async def _set_online(
        self, chat_id, until: datetime, override: bool = False, *, start_session: bool = False,
    ) -> datetime:
        await self.store.set_presence(
            chat_id, observed_at=self.now(), start_session=start_session,
            is_sleeping=False, sleep_until=until.isoformat() if override else None,
            is_offline=False, offline_until=None, online_until=until.isoformat(),
        )
        return until

    async def go_offline(self, chat_id, habits: Habits) -> datetime:
        return await self._go_offline(chat_id, habits, self.now())

    async def _go_offline(self, chat_id, habits: Habits, since: datetime) -> datetime:
        lo, hi = habits.offline_minutes
        until = since + timedelta(minutes=self.rng.triangular(lo, hi, lo + 0.2 * (hi - lo)))
        await self.store.set_presence(
            chat_id, observed_at=since, is_sleeping=False, sleep_until=None, is_offline=True,
            offline_until=until.isoformat(), online_until=None,
        )
        self._session_ended(chat_id)
        return until

    async def extend_online(self, chat_id, habits: Habits, minutes: float) -> datetime:
        """Extend by up to 30 minutes, but never past 2 hours after the session began, nor (unless the
        owner woke Dom) into the next sleep window. When nothing can be added the deadline is unchanged."""
        row = await self.store.get_presence(chat_id)
        online = _deadline(row["online_until"])
        now = self.now()
        if online is None or online <= now:
            raise ValueError("the chat is no longer online")
        owner_wake = _deadline(row["sleep_until"]) == online
        until = online + timedelta(minutes=min(max(minutes, 0), 30))
        until = min(until, self._session_start.get(chat_id, now) + MAX_SESSION)
        if not owner_wake:
            until = min(until, self._next_sleep_start(now, habits))
        return await self._set_online(chat_id, max(until, online), override=owner_wake)

    async def wake(self, chat_id, habits: Habits, minutes: float = 30) -> datetime:
        now = self.now()
        self._session_start[chat_id] = now
        return await self._set_online(chat_id, now + timedelta(minutes=minutes), override=True, start_session=True)

    async def sleep(self, chat_id, habits: Habits) -> datetime:
        now = self.now().astimezone(self.tz)
        end = datetime.combine(now.date(), habits.sleep_end, self.tz)
        if end <= now:
            end += timedelta(days=1)
        end = min(end, now + MAX_SLEEP_COMMAND)
        was_online = (await self.store.get_presence(chat_id))["online_until"] is not None
        await self.store.set_presence(
            chat_id, observed_at=now, is_sleeping=True, sleep_until=end.isoformat(),
            is_offline=False, offline_until=None, online_until=None,
        )
        if was_online:
            self._session_ended(chat_id)
        return end

"""Serial daily background jobs, scheduled in the instance's timezone."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dom.home import PrivateAccessError
from dom.presence import Habits, parse_habits

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 4  # first attempt plus three retries


class DailyJobs:
    def __init__(self, app: App, now: Callable[[], datetime]):
        self.app, self.now = app, now
        self._jobs: dict[str, tuple[Callable[[datetime, Habits], bool], Callable[[], Awaitable[None]]]] = {}
        self._lock = asyncio.Lock()

    def register(
        self, name: str, due: Callable[[datetime, Habits], bool], run: Callable[[], Awaitable[None]],
    ) -> None:
        self._jobs[name] = due, run

    async def tick(self) -> None:
        async with self._lock:
            store = self.app.store
            home = self.app.home.scoped(allow_private=False)
            for name, (due, run) in self._jobs.items():
                local = self.now().astimezone(ZoneInfo(self.app.config.timezone))
                day = local.date().isoformat()
                key = f"job:{name}"
                try:
                    if await store.kv_get(f"{key}:last") == day:
                        continue
                    try:
                        text = home.read("memory/HABITS.md")
                    except (PrivateAccessError, ValueError):
                        text = ""
                    if not due(local, parse_habits(text)):
                        continue
                    attempt_day, _, count = (await store.kv_get(f"{key}:attempts") or "").partition("/")
                    attempts = int(count) if attempt_day == day else 0
                    if attempts >= MAX_ATTEMPTS:
                        continue
                    await store.kv_set(f"{key}:attempts", f"{day}/{attempts + 1}")
                    await run()
                    await store.kv_set(f"{key}:last", day)
                except Exception:
                    log.exception("daily job %s failed", name)

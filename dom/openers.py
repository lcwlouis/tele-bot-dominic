"""Occasionally start a turn in the owner's DM after a quiet stretch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from random import Random
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dom import turn_log
from dom.presence import read_habits
from dom.stickers import resolve_sticker
from dom.telegram import executor
from dom.usage import ModelCallSkipped, check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
ATTEMPT_SPACING = timedelta(hours=3)
QUIET_PERIOD = timedelta(minutes=45)
MINUTE_CHANCE = 1 - 0.70 ** (1 / 60)  # a 30% chance in 60 eligible minute ticks
LAST_ATTEMPT = "opener:last_attempt"
LAST_MESSAGE = "opener:last_msg_id"
SENT_COUNT = "opener:sent"
NOTE = (
    "Nobody has written for a while. If you have something real to say to your friend right now — "
    "a morning text, a follow-up on something from your notes, something you looked up — say it. "
    "Otherwise answer no_response."
)


def _timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


class Openers:
    def __init__(self, app: App, rng: Random):
        self.app, self.rng = app, rng
        self._running = False

    def _day(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.app.config.timezone)).date().isoformat()

    async def _sent_today(self, day: str) -> int:
        sent_day, _, count = (await self.app.store.kv_get(SENT_COUNT) or "").partition("/")
        return int(count) if sent_day == day else 0

    async def maybe(self, now: datetime) -> None:
        app = self.app
        owner = app.owner_id
        if self._running or not app.config.openers.enabled or owner is None or owner <= 0:
            return
        self._running = True
        try:
            with app.dispatch._protect_send() as commit:
                async with turn_log.capture(app.store, now, owner, "owner_dm"):
                    await self._maybe(owner, now, commit)
        except ModelCallSkipped:
            return
        except Exception:
            log.exception("owner opener failed")
        finally:
            self._running = False

    async def _maybe(self, owner: int, now: datetime, commit: Callable[[], None]) -> None:
        app = self.app
        dispatch, store = app.dispatch, app.store
        allowed = partial(dispatch.turn_allowed, owner, "owner_dm")
        async with app.locks[owner]:
            if owner != app.owner_id or not await store.is_chat_approved(owner):
                return
            habits = read_habits(app.home)
            state, until = dispatch.presence.peek(await store.get_presence(owner), habits)
            if state == "asleep" or await self._sent_today(self._day(now)) >= app.config.openers.max_per_day:
                return
            previous = await store.kv_get(LAST_ATTEMPT)
            if previous is not None and now - _timestamp(previous) < ATTEMPT_SPACING:
                return
            latest = await store.latest_chat_message(owner)
            if latest and (
                now - _timestamp(latest[1]) < QUIET_PERIOD or str(latest[0]) == await store.kv_get(LAST_MESSAGE)
            ):
                return
            if (await store.get_unread(owner))[0] > 0:  # the owner wrote and Dom has not answered yet
                return
            if self.rng.random() >= MINUTE_CHANCE:
                return
            await store.kv_set(LAST_ATTEMPT, now.isoformat())
            window = await dispatch._window(owner)
            minutes = None  # a session is only started once Dom really texts
            if state != "online" or until is None:
                minutes = dispatch.presence.session_minutes(habits)
                until = dispatch.now() + timedelta(minutes=minutes)

        with model_request("chat", owner, allowed):
            await check_model_allowed()
            actions = await dispatch._compose_turn(
                owner, "owner_dm", None, None, window, until, 0, False, habits, extra_note=NOTE,
            )
        if actions.no_response or not (actions.bubbles or resolve_sticker(app.stickers.sendable(), actions.sticker) or (actions.gif and app.gifs)):
            return

        async with dispatch._dispatch_slot(owner, wait_media=False):
            if owner != app.owner_id or not await store.is_chat_approved(owner):
                return
            if dispatch.presence.peek(await store.get_presence(owner), read_habits(app.home))[0] == "asleep":
                return
            if await store.latest_chat_message(owner) != latest:
                return
            day = self._day(dispatch.now())
            sent = await self._sent_today(day)
            if not app.config.openers.enabled or sent >= app.config.openers.max_per_day:
                return
            if minutes is not None:
                await dispatch.presence.go_online(owner, habits, minutes)
            first = True

            async def on_sent(msg_id: int) -> None:
                nonlocal first
                values = {LAST_MESSAGE: str(msg_id)}
                if first:
                    delivered_day = self._day(dispatch.now())
                    delivered_count = sent if delivered_day == day else await self._sent_today(delivered_day)
                    values[SENT_COUNT] = f"{delivered_day}/{delivered_count + 1}"
                await store.kv_set_many(values)
                first = False

            sent_ids = await executor.perform(
                app.bot, store, owner, None, actions, sleep=app.sleep, now=dispatch.now, on_sent=on_sent,
                stickers=app.stickers.sendable(), gifs=app.gifs, before_send=commit,
            )
            if sent_ids:
                turn_log.outcome("Texted first")

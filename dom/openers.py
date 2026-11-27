"""Occasionally start a turn after a quiet stretch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from random import Random
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError

from dom import turn_log
from dom.goals import checkin_opener_note
from dom.models import Scope, StoredMessage
from dom.noticings import card_markup, format_card
from dom.presence import Habits, read_habits
from dom.stickers import resolve_sticker
from dom.telegram import executor
from dom.usage import ModelCallSkipped, check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
ATTEMPT_SPACING = timedelta(hours=3)
QUIET_PERIOD = timedelta(minutes=45)
GROUP_QUIET_PERIOD = timedelta(hours=3)
CHAT_SCAN_LIMIT = 16
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
        self._cursor = ""

    def _day(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.app.config.timezone)).date().isoformat()

    async def _sent_today(self, day: str, key: str) -> int:
        sent_day, _, count = (await self.app.store.kv_get(key) or "").partition("/")
        return int(count) if sent_day == day else 0

    def _enabled(self, chat_id: int, scope: Scope) -> bool:
        config = self.app.config.openers
        if scope == "owner_dm":
            return config.enabled and chat_id == self.app.owner_id
        if self.app.owner_id is None or chat_id == self.app.owner_id:
            return False
        return config.groups if scope == "group" else config.friends

    async def maybe(self, now: datetime) -> None:
        app = self.app
        owner = app.owner_id
        settings = app.config.openers
        if self._running or not (settings.enabled or settings.groups or settings.friends) or owner is None or owner <= 0:
            return
        self._running = True
        try:
            if settings.enabled:
                await self._check_chat(owner, "owner_dm", now)
            settings = app.config.openers
            if not (settings.groups or settings.friends) or settings.max_per_day == 0:
                return
            # Resume after the last chat checked, even if it attempted and passed.
            rows = await app.store.opener_candidates(
                owner, self._cursor, CHAT_SCAN_LIMIT, groups=settings.groups, friends=settings.friends,
            )
            for row in rows:
                self._cursor = row["chat_id"]
                chat_id = int(row["chat_id"])
                if app.dispatch.turn_running(chat_id) or app.locks[chat_id].locked():
                    continue
                scope = app.store.scope_for(chat_id, row["chat_type"])
                if await self._check_chat(chat_id, scope, now):
                    return
            if len(rows) < CHAT_SCAN_LIMIT:
                self._cursor = ""
        except Exception:
            log.exception("could not check opener candidates")
        finally:
            self._running = False

    async def _check_chat(self, chat_id: int, scope: Scope, now: datetime) -> bool:
        """Return whether this chat used the tick's attempt."""
        attempted = False

        def mark_attempt() -> None:
            nonlocal attempted
            attempted = True

        try:
            with self.app.dispatch.protect_send() as commit:
                async with turn_log.capture(self.app.store, now, chat_id, scope):
                    await self._maybe(chat_id, scope, now, commit, mark_attempt)
        except ModelCallSkipped:
            pass
        except Exception:
            log.exception("opener failed in chat %s", chat_id)
        return attempted

    async def _presence(self, chat_id: int, scope: Scope, habits: Habits) -> tuple[bool, datetime | None]:
        if not self._enabled(chat_id, scope) or not await self.app.store.is_chat_approved(chat_id):
            return False, None
        state, until = self.app.dispatch.presence.peek(await self.app.store.get_presence(chat_id), habits)
        if state == "online":
            return True, until
        # Offline is where an opener starts: Dom comes online by texting first, in any chat. Asleep never opens.
        return state == "offline", None

    async def _group_can_send(self, chat_id: int) -> bool:
        app = self.app
        try:
            member = await app.bot.get_chat_member(chat_id, app.bot_id, request_timeout=5)
            if member.status in ("administrator", "creator"):
                return True
            if member.status == "restricted":
                return member.is_member and member.can_send_messages
            if member.status != "member":
                return False
            chat = await app.bot.get_chat(chat_id, request_timeout=5)
            return chat.type in ("group", "supergroup") and (
                chat.permissions is None or chat.permissions.can_send_messages is True
            )
        except TelegramAPIError:
            return False

    async def _maybe(
        self, chat_id: int, scope: Scope, now: datetime, commit: Callable[[], None], mark_attempt: Callable[[], None],
    ) -> None:
        app = self.app
        dispatch, store = app.dispatch, app.store
        owner_dm = scope == "owner_dm"
        suffix = "" if owner_dm else f":{chat_id}"
        attempt_key, message_key, count_key = (key + suffix for key in (LAST_ATTEMPT, LAST_MESSAGE, SENT_COUNT))
        quiet = GROUP_QUIET_PERIOD if scope == "group" else QUIET_PERIOD
        async with app.locks[chat_id]:
            habits = read_habits(app.home)
            present, until = await self._presence(chat_id, scope, habits)
            if not present or await self._sent_today(self._day(now), count_key) >= app.config.openers.max_per_day:
                return
            previous = await store.kv_get(attempt_key)
            if previous is not None and now - _timestamp(previous) < ATTEMPT_SPACING:
                return
            latest = await store.latest_chat_message(chat_id)
            if latest is None and not owner_dm:
                return
            if latest and (
                now - _timestamp(latest[1]) < quiet or str(latest[0]) == await store.kv_get(message_key)
            ):
                return
            if (await store.get_unread(chat_id))[0] > 0:
                return
            if self.rng.random() >= MINUTE_CHANCE:
                return
            await store.kv_set(attempt_key, now.isoformat())
            mark_attempt()
            window = await dispatch._window(chat_id)
            minutes = None  # the owner's session starts only once Dom really texts
            if until is None:
                minutes = dispatch.presence.session_minutes(habits)
                until = dispatch.now() + timedelta(minutes=minutes)
            noticing = await app.noticings.opener_due(now) if owner_dm else None
            goal_checkin = await app.goals.due_checkin(now) if (owner_dm and noticing is None) else None

        if owner_dm and noticing is not None:
            async with dispatch._dispatch_slot(chat_id, wait_media=False):
                present, _ = await self._presence(chat_id, scope, read_habits(app.home))
                if not present or await store.latest_chat_message(chat_id) != latest:
                    return
                day = self._day(dispatch.now())
                sent = await self._sent_today(day, count_key)
                if not self._enabled(chat_id, scope) or sent >= app.config.openers.max_per_day:
                    return
                live_noticing = await app.noticings.opener_due(dispatch.now())
                if live_noticing is None or live_noticing["id"] != noticing["id"]:
                    return
                if minutes is not None:
                    await dispatch.presence.go_online(chat_id, habits, minutes)
                commit()
                card_text = format_card(live_noticing)
                delivered = await executor.send_text(
                    app.bot, chat_id, card_text,
                    reply_markup=card_markup(live_noticing["id"]),
                    sleep=app.sleep, literal=True,
                )
                if delivered is not None:
                    delivered_now = dispatch.now()
                    delivered_day = self._day(delivered_now)
                    delivered_count = sent if delivered_day == day else await self._sent_today(delivered_day, count_key)
                    await store.record_new_message(StoredMessage(
                        chat_id=str(chat_id), msg_id=delivered.message_id, role="assistant", is_bot=True,
                        visibility_scope=store.visibility_for(chat_id), text=card_text,
                        created_at=delivered_now.isoformat(),
                    ))
                    await store.kv_set_many({
                        message_key: str(delivered.message_id),
                        count_key: f"{delivered_day}/{delivered_count + 1}",
                    })
                    await app.noticings.mark_opener_sent(live_noticing["id"], delivered_now, delivered.message_id)
                    turn_log.outcome("Sent a noticing")
            return

        async def allowed() -> bool:
            if not owner_dm:
                present, _ = await self._presence(chat_id, scope, read_habits(app.home))
                if not present:
                    return False
            return await dispatch.turn_allowed(chat_id, scope)

        opener_note = checkin_opener_note(goal_checkin) if (owner_dm and goal_checkin is not None) else NOTE
        with model_request("chat", chat_id, allowed, unprompted=True):
            await check_model_allowed()
            if scope == "group" and not await self._group_can_send(chat_id):
                return
            if not owner_dm and await store.latest_chat_message(chat_id) != latest:
                return
            actions = await dispatch._compose_turn(
                chat_id, scope, None, None, window, until, 0, False, habits, extra_note=opener_note,
            )
        if actions.no_response or not (actions.bubbles or resolve_sticker(app.stickers.sendable(), actions.sticker) or (actions.gif and app.gifs)):
            return
        if scope == "group" and actions.private:
            return
        if not owner_dm and await dispatch._hold_for_leak_check(chat_id, scope, actions, ""):
            turn_log.outcome("Held back: possible leak")
            return

        async with dispatch._dispatch_slot(chat_id, wait_media=False):
            present, _ = await self._presence(chat_id, scope, read_habits(app.home))
            if not present or await store.latest_chat_message(chat_id) != latest:
                return
            day = self._day(dispatch.now())
            sent = await self._sent_today(day, count_key)
            if not self._enabled(chat_id, scope) or sent >= app.config.openers.max_per_day:
                return
            if minutes is not None:
                await dispatch.presence.go_online(chat_id, habits, minutes)
            first = True
            expected_id = latest[0] if latest else None

            async def authorize_message() -> None:
                # Membership was checked before composing; check it once more before the first message only, so a
                # slow Telegram can't hold this chat's dispatch slot for every bubble.
                if scope == "group" and first and not await self._group_can_send(chat_id):
                    raise ModelCallSkipped
                present, _ = await self._presence(chat_id, scope, read_habits(app.home))
                current = await store.latest_chat_message(chat_id)
                if not present or (current[0] if current else None) != expected_id:
                    raise ModelCallSkipped
                if first and await self._sent_today(self._day(dispatch.now()), count_key) >= app.config.openers.max_per_day:
                    raise ModelCallSkipped

            async def on_sent(msg_id: int) -> None:
                nonlocal first, expected_id
                values = {message_key: str(msg_id)}
                if first:
                    delivered_day = self._day(dispatch.now())
                    delivered_count = sent if delivered_day == day else await self._sent_today(delivered_day, count_key)
                    values[count_key] = f"{delivered_day}/{delivered_count + 1}"
                await store.kv_set_many(values)
                expected_id = msg_id
                first = False

            sent_ids = await executor.perform(
                app.bot, store, chat_id, None, actions, sleep=app.sleep, now=dispatch.now, on_sent=on_sent,
                stickers=app.stickers.sendable(), gifs=app.gifs, before_send=commit,
                authorize_message=None if owner_dm else authorize_message,
            )
            if sent_ids:
                if owner_dm and goal_checkin is not None:
                    await app.goals.record_checkin(goal_checkin["id"], dispatch.now())
                    turn_log.outcome("Checked in on a goal")
                else:
                    turn_log.outcome("Texted first")

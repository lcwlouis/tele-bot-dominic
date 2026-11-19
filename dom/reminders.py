"""Owner reminders, including delivery independent of the chat model."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from dom.agent import parse_actions
from dom.models import StoredMessage
from dom.prompt import build_messages
from dom.usage import check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
MODEL_TIMEOUT = 20
MAX_ACTIVE = 50
IMPORTANCE = ("critical", "normal", "casual")
REPEATS = ("", "daily", "weekdays", "weekly")


def _utc_local(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """Prefer the earlier occurrence of an ambiguous time. A clock gap has no occurrence."""
    utc = wall.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    return utc if utc.astimezone(tz).replace(tzinfo=None) == wall else None


def _parse_when(when: str, tz: ZoneInfo) -> datetime:
    if not isinstance(when, str) or when[10:11] not in ("T", " "):
        raise ValueError("when must be an ISO-8601 date and time")
    try:
        instant = datetime.fromisoformat(when)
        due = instant.astimezone(timezone.utc) if instant.tzinfo else _utc_local(instant, tz)
    except (OverflowError, ValueError):
        raise ValueError("when must be an ISO-8601 date and time") from None
    if due is None:
        raise ValueError("when is a local time that does not exist")
    return due


def _next_due(due: datetime, repeat: str, now: datetime, tz: ZoneInfo) -> str | None:
    if not repeat:
        return None
    wall = due.astimezone(tz).replace(tzinfo=None)
    step = timedelta(days=7 if repeat == "weekly" else 1)
    while True:
        wall += step
        if repeat == "weekdays" and wall.weekday() >= 5:
            continue
        candidate = _utc_local(wall, tz)
        if candidate is not None and candidate > now:
            return candidate.isoformat()


class Reminders:
    def __init__(self, app: App):
        self.app = app
        self._tick_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._started_at: datetime | None = None

    def _now(self) -> datetime:
        return self.app.dispatch.now().astimezone(timezone.utc)

    @property
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.app.config.timezone)

    async def create(self, when: str, text: str, importance: str = "normal", repeat: str = "") -> int:
        app = self.app
        if app.owner_id is None:
            raise ValueError("there is no owner")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be non-empty")
        if importance not in IMPORTANCE or repeat not in REPEATS:
            raise ValueError("invalid importance or repeat")
        due = _parse_when(when, self._tz)
        now = self._now()
        if due <= now:
            raise ValueError("when must be in the future")
        reminder_id = await app.store.create_reminder(
            app.owner_id, text.strip(), due.isoformat(), importance, repeat, now.isoformat(), MAX_ACTIVE,
        )
        if reminder_id is None:
            raise ValueError(f"at most {MAX_ACTIVE} active reminders")
        await app.store.record_owner_event(f"Scheduled {importance} reminder #{reminder_id} for {due.isoformat()}")
        return reminder_id

    async def active(self) -> list[dict]:
        owner = self.app.owner_id
        return await self.app.store.active_reminders(owner) if owner is not None else []

    async def cancel(self, reminder_id: int) -> bool:
        app = self.app
        async with self._delivery_lock:
            changed = await app.store.cancel_reminder(app.owner_id, reminder_id)
        if changed:
            await app.store.record_owner_event(f"Cancelled reminder #{reminder_id}")
        return changed

    async def _advance(self, row: dict, now: datetime) -> None:
        due = datetime.fromisoformat(row["due_at"])
        next_due = _next_due(due, row["repeat"], now, self._tz)
        await self.app.store.finish_reminder(row["id"], now.isoformat(), next_due)

    async def casual_note(self) -> str:
        now = self._now()
        lines = []
        for row in await self.active():
            if row["importance"] == "casual" and datetime.fromisoformat(row["due_at"]) <= now:
                async with self._delivery_lock:
                    if not await self.app.store.reminder_is_active(row["id"]):
                        continue
                    await self._advance(row, now)
                lines.append(f"reminder: {escape(row['text'])}")
        return "\n".join(lines)

    async def tick(self, *, startup: bool = False) -> None:
        app = self.app
        owner = app.owner_id
        if owner is None:
            return
        async with self._tick_lock:
            now = self._now()
            if startup and self._started_at is None:
                self._started_at = now
            asleep = app.dispatch.presence.peek(
                await app.store.get_presence(owner), app.dispatch._habits(),
            )[0] == "asleep"
            pending = []
            for row in await self.active():
                due = datetime.fromisoformat(row["due_at"])
                if due > now or row["importance"] == "casual":
                    continue
                missed = self._started_at is not None and due < self._started_at
                if row["importance"] == "normal" and asleep and not missed:
                    continue
                late = missed or now - due > timedelta(minutes=30)
                pending.append(self._deliver(owner, row, late))
            await asyncio.gather(*pending)

    async def _in_character(self, owner: int, text: str) -> str | None:
        app = self.app
        try:
            async with asyncio.timeout(MODEL_TIMEOUT):
                messages = build_messages(
                    app.home, "owner_dm", owner, [], "",
                    "Send one short in-character reminder using the reminder text below as content. "
                    "Include what the owner asked to remember.\n"
                    f"<reminder>{escape(text)}</reminder>",
                )
                with model_request("chat", owner, partial(app.dispatch.turn_allowed, owner, "owner_dm")):
                    await check_model_allowed()
                    reply = await app.client.complete(messages, [], once=True)
            actions = parse_actions(reply.get("content"))
            if actions and not actions.no_response and actions.bubbles:
                return actions.bubbles[0].text
        except Exception as error:  # the stored reminder still goes out if the model path fails
            log.warning("critical reminder model unavailable (%s)", type(error).__name__)
        return None

    async def _deliver(self, owner: int, row: dict, late: bool) -> None:
        app = self.app
        try:
            styled = await self._in_character(owner, row["text"]) if row["importance"] == "critical" else None
            text = styled or f"⏰ {row['text']}"
            if late:
                text += " (late)"
            chunks = [text] if len(text.encode("utf-16-le")) <= 8192 else [
                text[start:start + 2000] for start in range(0, len(text), 2000)
            ]
            async with self._delivery_lock:
                if owner != app.owner_id or not await app.store.reminder_is_active(row["id"]):
                    return
                sent = [await app.bot.send_message(owner, chunk) for chunk in chunks]
                now = self._now()
                await self._advance(row, now)
            for message, chunk in zip(sent, chunks):
                await app.store.record_new_message(StoredMessage(
                    chat_id=str(owner), msg_id=message.message_id, role="assistant", is_bot=True,
                    visibility_scope=app.store.visibility_for(owner), text=chunk, created_at=now.isoformat(),
                ))
        except Exception:
            log.exception("could not deliver reminder %s", row["id"])

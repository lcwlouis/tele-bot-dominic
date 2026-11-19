"""Owner reminders, including delivery independent of the chat model.

A reminder is advanced only after every chunk of it was sent, so a crash between send and advance can duplicate a
reminder, never lose one. A failed delivery is retried with a growing delay and cancelled after MAX_ATTEMPTS."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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
MAX_ATTEMPTS = 10
MAX_BACKOFF_MINUTES = 60


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


def _carry(styled: str | None, text: str) -> str:
    """The model's styled text when it still says what the owner asked to remember, else it followed by the stored text."""
    plain = f"⏰ {text}"
    if not styled:
        return plain
    if " ".join(text.split()).casefold() in " ".join(styled.split()).casefold():
        return styled
    return f"{styled}\n{plain}"


@dataclass
class _Delivery:
    """What one reminder has done so far in memory: its styled chunks (styling costs a model call, so once), the
    message ids of the chunks already sent (never resent), and the failed attempts."""

    chunks: list[str] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    failures: int = 0
    retry_at: datetime | None = None


class Reminders:
    def __init__(self, app: App):
        self.app = app
        self._deliveries: dict[int, _Delivery] = {}
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
            self._deliveries.pop(reminder_id, None)
        if changed:
            await app.store.record_owner_event(f"Cancelled reminder #{reminder_id}")
        return changed

    async def _advance(self, row: dict, now: datetime) -> None:
        due = datetime.fromisoformat(row["due_at"])
        next_due = _next_due(due, row["repeat"], now, self._tz)
        await self.app.store.finish_reminder(row["id"], now.isoformat(), next_due)

    async def casual_note(self) -> tuple[str, list[int]]:
        """The note for the casual reminders that are due, and their ids. They stay due until `consume` is called."""
        now = self._now()
        due = [
            row for row in await self.active()
            if row["importance"] == "casual" and datetime.fromisoformat(row["due_at"]) <= now
        ]
        return "\n".join(f"reminder: {escape(row['text'])}" for row in due), [row["id"] for row in due]

    async def consume(self, ids: list[int]) -> None:
        """Mark casual reminders as brought up: finish a one-off, move a repeating one to its next time."""
        async with self._delivery_lock:
            now = self._now()
            for row in await self.active():
                if row["id"] in ids and row["importance"] == "casual" and datetime.fromisoformat(row["due_at"]) <= now:
                    await self._advance(row, now)

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
                if (delivery := self._deliveries.get(row["id"])) and delivery.retry_at and delivery.retry_at > now:
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
                return "\n".join(bubble.text for bubble in actions.bubbles)
        except Exception as error:  # the stored reminder still goes out if the model path fails
            log.warning("critical reminder model unavailable (%s)", type(error).__name__)
        return None

    async def _deliver(self, owner: int, row: dict, late: bool) -> None:
        app = self.app
        delivery = self._deliveries.setdefault(row["id"], _Delivery())
        try:
            if not delivery.chunks:
                styled = await self._in_character(owner, row["text"]) if row["importance"] == "critical" else None
                text = _carry(styled, row["text"])
                if late:
                    text += " (late)"
                delivery.chunks = [text] if len(text.encode("utf-16-le")) <= 8192 else [
                    text[start:start + 2000] for start in range(0, len(text), 2000)
                ]
            async with self._delivery_lock:
                if owner != app.owner_id or not await app.store.reminder_is_active(row["id"]):
                    self._deliveries.pop(row["id"], None)
                    return
                for chunk in delivery.chunks[len(delivery.sent):]:
                    delivery.sent.append((await app.bot.send_message(owner, chunk)).message_id)
                now = self._now()
                await self._advance(row, now)
                self._deliveries.pop(row["id"], None)
        except Exception:
            log.exception("could not deliver reminder %s", row["id"])
            await self._failed(owner, row, delivery)
            return
        try:
            for message_id, chunk in zip(delivery.sent, delivery.chunks):
                await app.store.record_new_message(StoredMessage(
                    chat_id=str(owner), msg_id=message_id, role="assistant", is_bot=True,
                    visibility_scope=app.store.visibility_for(owner), text=chunk, created_at=now.isoformat(),
                ))
        except Exception:
            log.exception("could not record reminder %s in the chat", row["id"])

    async def _failed(self, owner: int, row: dict, delivery: _Delivery) -> None:
        """Back off 1, 2, 4... minutes (at most an hour) before the next attempt; give up after MAX_ATTEMPTS."""
        delivery.failures += 1
        if delivery.failures < MAX_ATTEMPTS:
            wait = min(2 ** (delivery.failures - 1), MAX_BACKOFF_MINUTES)
            delivery.retry_at = self._now() + timedelta(minutes=wait)
            return
        log.error("giving up on reminder %s after %s failed deliveries", row["id"], delivery.failures)
        self._deliveries.pop(row["id"], None)
        try:
            await self.app.store.cancel_reminder(owner, row["id"])
        except Exception:
            log.exception("could not cancel reminder %s", row["id"])

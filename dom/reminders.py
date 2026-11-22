"""Owner reminders, including delivery independent of the chat model.

A reminder is advanced only after every chunk of it was sent, so a crash between send and advance can duplicate a
reminder, never lose one. A failed delivery is retried with a growing delay: non-critical reminders are cancelled
after MAX_ATTEMPTS, while critical reminders persist retry state, keep retrying (notifying the owner once after
MAX_ATTEMPTS), and stay active marked undeliverable if the destination is gone, retried every UNDELIVERABLE_RETRY
until one delivery clears the mark."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from dom.agent import parse_actions
from dom.deletion import STALE_MESSAGE, prune_deletions, stage_deletion, stamp_matches
from dom.models import StoredMessage
from dom.prompt import build_messages
from dom.telegram.executor import FloodBudget, retry_telegram
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
UNDELIVERABLE_RETRY = timedelta(hours=6)  # a critical reminder is never given up; a gone destination is tried this slowly
RETRY_STATE_PREFIX = "reminder_retry:"
_UNDELIVERABLE = re.compile(
    r"chat not found|user not found|user is deactivated|bot was blocked|can't initiate conversation", re.IGNORECASE,
)


def _is_destination_gone(exc: BaseException) -> bool:
    return isinstance(exc, TelegramForbiddenError) or (
        isinstance(exc, TelegramBadRequest) and _UNDELIVERABLE.search(str(exc)) is not None
    )


def _utc_local(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """Prefer the earlier occurrence of an ambiguous time. A clock gap has no occurrence."""
    utc = wall.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    return utc if utc.astimezone(tz).replace(tzinfo=None) == wall else None


def _after_gap(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """The first valid time after a local time that does not exist, on the same day (None if the gap lasts past it)."""
    probe = wall.replace(second=0, microsecond=0)
    while probe.date() == wall.date():
        if (instant := _utc_local(probe, tz)) is not None:
            return instant
        probe += timedelta(minutes=1)
    return None


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
        candidate = _utc_local(wall, tz) or _after_gap(wall, tz)
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
    """What one reminder occurrence has done so far: its styled chunks (styling costs a model call, so once), the
    message ids of the chunks already sent (never resent), and the failed attempts / persisted critical retry state."""

    due_at: str = ""
    chunks: list[str] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    failures: int = 0
    retry_at: datetime | None = None
    owner_notified: bool = False
    undeliverable: bool = False


class Reminders:
    def __init__(self, app: App):
        self.app = app
        self._deliveries: dict[int, _Delivery] = {}
        self._in_flight: set[int] = set()
        self._tick_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._started_at: datetime | None = None

    @staticmethod
    def _retry_key(reminder_id: int) -> str:
        return f"{RETRY_STATE_PREFIX}{reminder_id}"

    async def _load_retry_state(self, reminder_id: int, due_at: str) -> _Delivery | None:
        raw = await self.app.store.kv_get(self._retry_key(reminder_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("due_at") != due_at:
            return None
        retry_at = None
        if isinstance(data.get("retry_at"), str) and data["retry_at"]:
            try:
                retry_at = datetime.fromisoformat(data["retry_at"])
            except ValueError:
                retry_at = None
        raw_chunks = data.get("chunks")
        chunks = [c for c in raw_chunks if isinstance(c, str)] if isinstance(raw_chunks, list) else []
        raw_sent = data.get("sent")
        sent = [m for m in raw_sent if isinstance(m, int)] if isinstance(raw_sent, list) else []
        if not sent or len(sent) > len(chunks):
            # Only restore saved chunks once at least one chunk was sent; otherwise a restart re-evaluates `(late)`.
            chunks, sent = [], []
        return _Delivery(
            due_at=due_at,
            chunks=chunks,
            sent=sent,
            failures=int(data.get("failures") or 0),
            retry_at=retry_at,
            owner_notified=bool(data.get("owner_notified")),
            undeliverable=bool(data.get("undeliverable")),
        )

    async def _save_retry_state(self, reminder_id: int, due_at: str, delivery: _Delivery) -> None:
        payload = {
            "due_at": due_at,
            "failures": delivery.failures,
            "retry_at": delivery.retry_at.isoformat() if delivery.retry_at else None,
            "owner_notified": delivery.owner_notified,
            "undeliverable": delivery.undeliverable,
            "chunks": list(delivery.chunks) if delivery.sent else [],
            "sent": list(delivery.sent),
        }

        async def persist() -> None:
            try:
                await self.app.store.kv_set(self._retry_key(reminder_id), json.dumps(payload))
            except Exception:
                log.exception("could not persist retry state for reminder %s", reminder_id)

        task = asyncio.create_task(persist())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _clear_retry_state(self, reminder_id: int) -> None:
        try:
            await self.app.store.kv_set(self._retry_key(reminder_id), "")
        except Exception:
            log.exception("could not clear retry state for reminder %s", reminder_id)

    def is_delivering(self, reminder_id: int) -> bool:
        return reminder_id in self._in_flight

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

    async def list_all(self, limit: int = 50) -> list[dict]:
        owner = self.app.owner_id
        return await self.app.store.list_reminders(owner, limit=limit) if owner is not None else []

    async def cancel(self, reminder_id: int) -> bool:
        app = self.app
        async with self._delivery_lock:
            changed = await app.store.cancel_reminder(app.owner_id, reminder_id)
            self._deliveries.pop(reminder_id, None)
            await self._clear_retry_state(reminder_id)
        if changed:
            await app.store.record_owner_event(f"Cancelled reminder #{reminder_id}")
        return changed

    async def delete(self, reminder_id: int, expected_created_at: str) -> tuple[bool, str]:
        app = self.app
        owner = app.owner_id
        if owner is None:
            return False, "There is no owner."
        if not expected_created_at:
            return False, STALE_MESSAGE
        row = await app.store.get_reminder(owner, reminder_id)
        if row is None:
            return False, f"Reminder #{reminder_id} not found."
        if not stamp_matches(row["created_at"], expected_created_at):
            return False, STALE_MESSAGE
        if self.is_delivering(reminder_id):
            return False, f"Cannot delete reminder #{reminder_id} while it is being delivered."
        event_at = datetime.now(timezone.utc).isoformat()
        event_text = f"Deleted reminder #{reminder_id}"
        retry_key = self._retry_key(reminder_id)
        retry_value = await app.store.kv_get(retry_key)
        rows: dict[str, object] = {"reminder": row}
        if retry_value:
            rows["kv"] = [{"key": retry_key, "value": retry_value}]
        try:
            archive = await asyncio.to_thread(
                stage_deletion,
                app.home.root,
                "reminder",
                reminder_id,
                event_text=event_text,
                event_at=event_at,
                rows=rows,
            )
        except (OSError, ValueError) as error:
            log.warning("could not archive reminder %s: %s", reminder_id, type(error).__name__)
            return False, "Could not archive reminder before deleting."
        async with self._delivery_lock:
            if self.is_delivering(reminder_id):
                await asyncio.to_thread(archive.rollback)
                return False, f"Cannot delete reminder #{reminder_id} while it is being delivered."
            latest_row = await app.store.get_reminder(owner, reminder_id)
            if latest_row is None:
                await asyncio.to_thread(archive.rollback)
                return False, f"Reminder #{reminder_id} not found."
            if not stamp_matches(latest_row["created_at"], expected_created_at):
                await asyncio.to_thread(archive.rollback)
                return False, STALE_MESSAGE
            latest_retry = await app.store.kv_get(retry_key)
            updated_rows: dict[str, object] = {"reminder": latest_row}
            if latest_retry:
                updated_rows["kv"] = [{"key": retry_key, "value": latest_retry}]
            if updated_rows != rows:
                try:
                    await asyncio.to_thread(archive.update_rows, updated_rows)
                except (OSError, ValueError) as error:
                    log.warning("could not archive reminder %s: %s", reminder_id, type(error).__name__)
                    await asyncio.to_thread(archive.rollback)
                    return False, "Could not archive reminder before deleting."
            try:
                result = await app.store.delete_reminder(
                    owner, reminder_id, expected_created_at, event_text, event_at, retry_key=retry_key,
                )
            except sqlite3.Error as error:
                log.warning("could not delete reminder %s: %s", reminder_id, type(error).__name__)
                await asyncio.to_thread(archive.rollback)
                return False, "Could not delete reminder."
            if result != "deleted":
                await asyncio.to_thread(archive.rollback)
                return False, STALE_MESSAGE if result == "stale" else f"Reminder #{reminder_id} not found."
            self._deliveries.pop(reminder_id, None)
            await asyncio.to_thread(archive.commit)
        await asyncio.to_thread(prune_deletions, app.home.root)
        return True, f"Deleted reminder #{reminder_id}."

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
                delivery = self._deliveries.get(row["id"])
                if delivery is not None and delivery.due_at != row["due_at"]:
                    self._deliveries.pop(row["id"], None)
                    delivery = None
                if delivery is None and row["importance"] == "critical":
                    if (saved := await self._load_retry_state(row["id"], row["due_at"])) is not None:
                        self._deliveries[row["id"]] = delivery = saved
                if delivery is not None and delivery.retry_at and delivery.retry_at > now:
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
                prompt_cache = getattr(app.dispatch, "prompt_cache", None)
                mem_text = prompt_cache.memory_text(app.home, app.dispatch.now())[0] if prompt_cache is not None else None
                messages = build_messages(
                    app.home, "owner_dm", owner, [], "",
                    "Send one short in-character reminder using the reminder text below as content. "
                    "Include what the owner asked to remember.\n"
                    f"<reminder>{escape(text)}</reminder>",
                    memory_text=mem_text,
                )
                with model_request("chat", owner, partial(app.dispatch.turn_allowed, owner, "owner_dm")):
                    await check_model_allowed()
                    reply = await app.client.complete(messages, [], once=True)
            actions = parse_actions(reply.get("content"))
            if actions and not actions.no_response and actions.bubbles:
                return "\n".join(bubble.text for bubble in actions.bubbles)
        except Exception as error:  # the stored reminder still goes out if the model path fails
            log.warning("reminder model unavailable (%s)", type(error).__name__)
        return None

    async def _deliver(self, owner: int, row: dict, late: bool) -> None:
        app = self.app
        reminder_id = row["id"]
        # Claim delivery under _delivery_lock, then style, send, and retry outside it so flood waits never block cancel/consume.
        async with self._delivery_lock:
            if (
                reminder_id in self._in_flight
                or owner != app.owner_id
                or not await app.store.reminder_is_active(reminder_id)
            ):
                return
            delivery = self._deliveries.get(reminder_id)
            if delivery is None or delivery.due_at != row["due_at"]:
                saved = (
                    await self._load_retry_state(reminder_id, row["due_at"])
                    if row["importance"] == "critical" else None
                )
                delivery = saved if saved is not None else _Delivery(due_at=row["due_at"])
                self._deliveries[reminder_id] = delivery
            self._in_flight.add(reminder_id)

        try:
            if not delivery.chunks:
                styled = await self._in_character(owner, row["text"])
                text = _carry(styled, row["text"])
                if late:
                    text += " (late)"
                delivery.chunks = [text] if len(text.encode("utf-16-le")) <= 8192 else [
                    text[start:start + 2000] for start in range(0, len(text), 2000)
                ]
            budget = FloodBudget()
            for chunk in delivery.chunks[len(delivery.sent):]:
                if owner != app.owner_id or not await app.store.reminder_is_active(reminder_id):
                    self._deliveries.pop(reminder_id, None)
                    return
                sent = await retry_telegram(
                    lambda c=chunk: app.bot.send_message(owner, c), sleep=app.sleep, budget=budget,
                )
                delivery.sent.append(sent.message_id)
                # A reminder cancelled or deleted while this chunk was in flight must not get its retry state back.
                if row["importance"] == "critical" and await app.store.reminder_is_active(reminder_id):
                    await self._save_retry_state(reminder_id, row["due_at"], delivery)
            async with self._delivery_lock:
                if owner != app.owner_id or not await app.store.reminder_is_active(reminder_id):
                    self._deliveries.pop(reminder_id, None)
                    return
                now = self._now()
                await self._advance(row, now)
                self._deliveries.pop(reminder_id, None)
                if row["importance"] == "critical":
                    await self._clear_retry_state(reminder_id)
        except Exception as exc:
            log.exception("could not deliver reminder %s", reminder_id)
            await self._failed(owner, row, delivery, exc)
            return
        finally:
            self._in_flight.discard(reminder_id)

        try:
            for message_id, chunk in zip(delivery.sent, delivery.chunks):
                await app.store.record_new_message(StoredMessage(
                    chat_id=str(owner), msg_id=message_id, role="assistant", is_bot=True,
                    visibility_scope=app.store.visibility_for(owner), text=chunk, created_at=now.isoformat(),
                ))
        except Exception:
            log.exception("could not record reminder %s in the chat", reminder_id)

    async def _failed(self, owner: int, row: dict, delivery: _Delivery, exc: Exception) -> None:
        """Back off 1, 2, 4... minutes (at most an hour) before the next attempt; non-critical reminders give up
        after MAX_ATTEMPTS while critical reminders persist retry state per occurrence, mark confirmed blocked/missing
        destinations undeliverable (while staying active), and notify the owner once per occurrence."""
        reminder_id = row["id"]
        if row["importance"] == "critical":
            if _is_destination_gone(exc):
                delivery.undeliverable = True
                delivery.retry_at = self._now() + UNDELIVERABLE_RETRY
                should_notify = not delivery.owner_notified
                delivery.owner_notified = True
                try:
                    await self.app.store.set_reminder_undeliverable(reminder_id, True)
                except Exception:
                    log.exception("could not mark critical reminder %s undeliverable", reminder_id)
                await self._save_retry_state(reminder_id, row["due_at"], delivery)
                if should_notify:
                    log.error("critical reminder %s destination is undeliverable (%s); keeping active", reminder_id, type(exc).__name__)
                    notice = (
                        f"Critical reminder #{reminder_id} is undeliverable ({type(exc).__name__}: {str(exc)[:120]}); "
                        f"kept active: {row['text'][:200]}"
                    )
                    try:
                        await self.app.store.record_owner_event(notice)
                    except Exception:
                        log.exception("could not record owner event for undeliverable critical reminder %s", reminder_id)
                return

            delivery.failures += 1
            wait = min(2 ** (delivery.failures - 1), MAX_BACKOFF_MINUTES)
            delivery.retry_at = self._now() + timedelta(minutes=wait)
            should_notify = delivery.failures >= MAX_ATTEMPTS and not delivery.owner_notified
            if should_notify:
                delivery.owner_notified = True
            await self._save_retry_state(reminder_id, row["due_at"], delivery)
            if should_notify:
                log.error("critical reminder %s failed %s deliveries; will keep retrying", reminder_id, delivery.failures)
                notice = (
                    f"Critical reminder #{reminder_id} could not be delivered after {MAX_ATTEMPTS} attempts; "
                    f"still retrying: {row['text'][:200]}"
                )
                try:
                    await self.app.store.record_owner_event(notice)
                except Exception:
                    log.exception("could not record owner event for critical reminder %s", reminder_id)
                try:
                    await retry_telegram(
                        lambda: self.app.bot.send_message(owner, notice),
                        sleep=self.app.sleep,
                        budget=FloodBudget(),
                    )
                except Exception:
                    log.exception("could not DM owner about critical reminder %s", reminder_id)
            return

        delivery.failures += 1
        if delivery.failures < MAX_ATTEMPTS:
            wait = min(2 ** (delivery.failures - 1), MAX_BACKOFF_MINUTES)
            delivery.retry_at = self._now() + timedelta(minutes=wait)
            return
        log.error("giving up on reminder %s after %s failed deliveries", reminder_id, delivery.failures)
        async with self._delivery_lock:
            self._deliveries.pop(reminder_id, None)
            try:
                await self.app.store.cancel_reminder(owner, reminder_id)
            except Exception:
                log.exception("could not cancel reminder %s", reminder_id)

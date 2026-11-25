"""Owner-only goals (R6): things the owner wants to get done or keep up.

Goals are strictly private to the owner DM and the owner dashboard. Code filters every prompt,
tool, opener and API path so goals never reach groups or friends' DMs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from dom import notes
from dom.deletion import STALE_MESSAGE, prune_deletions, stage_deletion, stamp_matches
from dom.models import Scope

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

CATEGORIES = ("", "health", "relationships", "money", "career", "interests", "productivity", "other")
RHYTHMS = ("", "daily", "every_few_days", "weekly", "biweekly", "monthly")
RHYTHM_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "every_few_days": timedelta(days=3),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}
STATUSES = ("active", "paused", "done", "dropped")
MAX_ACTIVE = 50
MAX_TEXT = 400
MAX_NOTE = 300
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def clean_text(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(notes.strip_tags(value).split())
    return cleaned[:limit]


def validate_category(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("category must be text")
    cat = value.strip().lower()
    if cat not in CATEGORIES:
        allowed = ", ".join(c for c in CATEGORIES if c)
        raise ValueError(f"category must be one of: {allowed}")
    return cat


def validate_rhythm(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("check_in_rhythm must be text")
    rhythm = value.strip().lower().replace("-", "_").replace(" ", "_")
    if rhythm not in RHYTHMS:
        allowed = ", ".join(r for r in RHYTHMS if r)
        raise ValueError(f"check_in_rhythm must be one of: {allowed}")
    return rhythm


def validate_target_date(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("target_date must be YYYY-MM-DD")
    raw = value.strip()
    if not _DATE_RE.fullmatch(raw):
        raise ValueError("target_date must be YYYY-MM-DD")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise ValueError("target_date must be a valid YYYY-MM-DD date") from None
    return raw


def validate_status(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("status must be text")
    status = value.strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    return status


def due_for_checkin(goals: list[dict], now: datetime) -> dict | None:
    """Return at most one active goal whose check-in rhythm has elapsed, preferring the most overdue."""
    utc_now = now.astimezone(timezone.utc)
    eligible: list[tuple[float, int, dict]] = []
    for goal in goals:
        if goal.get("status") != "active":
            continue
        rhythm = goal.get("check_in_rhythm") or ""
        interval = RHYTHM_INTERVALS.get(rhythm)
        if interval is None:
            continue
        ref_raw = goal.get("last_checkin_at") or goal.get("created_at")
        if not ref_raw:
            continue
        try:
            ref = _timestamp(ref_raw)
        except ValueError:
            continue
        elapsed = utc_now - ref
        if elapsed >= interval:
            overdue_ratio = elapsed.total_seconds() / interval.total_seconds()
            eligible.append((overdue_ratio, -int(goal["id"]), goal))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return eligible[0][2]


def checkin_opener_note(goal: dict) -> str:
    """Turn note for one gentle friend-like goal check-in opener (owner DM only)."""
    bits = [f"goal #{goal['id']}: {escape(goal['text'])}"]
    if goal.get("category"):
        bits.append(f"category: {escape(goal['category'])}")
    if goal.get("target_date"):
        bits.append(f"target date: {escape(goal['target_date'])}")
    if goal.get("progress_note"):
        bits.append(f"latest progress: {escape(goal['progress_note'])}")
    summary = " | ".join(bits)
    return (
        "Nobody has written for a while, and one of your friend's goals is due for a gentle check-in:\n"
        f"<goal_checkin>{summary}</goal_checkin>\n"
        "Check in warmly and briefly in your own voice like a friend who remembers — never like a task inbox "
        "or a nag. If it does not feel right to bring up now, answer no_response."
    )


def prompt_block(goals: list[dict], scope: Scope, chat_id: int, owner_id: int | None) -> str:
    """Compact owner-DM-only goal context for the turn note. Always empty outside the owner DM."""
    if scope != "owner_dm" or owner_id is None or chat_id != owner_id or not goals:
        return ""
    lines = []
    for goal in goals:
        if goal.get("status") != "active":
            continue
        parts = [f"#{goal['id']} {escape(goal['text'])}"]
        meta = []
        if goal.get("category"):
            meta.append(goal["category"])
        if goal.get("target_date"):
            meta.append(f"target {goal['target_date']}")
        if goal.get("check_in_rhythm"):
            meta.append(f"check-in {goal['check_in_rhythm'].replace('_', ' ')}")
        if meta:
            parts.append(f"({', '.join(meta)})")
        if goal.get("progress_note"):
            parts.append(f"— {escape(goal['progress_note'])}")
        active_rems = [r for r in goal.get("reminders", []) if r.get("status") == "active"]
        if active_rems:
            parts.append(f"[{len(active_rems)} active reminder{'s' if len(active_rems) != 1 else ''}]")
        lines.append("- " + " ".join(parts))
    if not lines:
        return ""
    return "<owner_goals>\n" + "\n".join(lines) + "\n</owner_goals>"


class Goals:
    """Owner-only goals service backed by SQLite."""

    def __init__(self, app: App):
        self.app = app

    def _now(self) -> datetime:
        return self.app.dispatch.now().astimezone(timezone.utc)

    def _require_owner(self) -> int:
        owner_id = self.app.owner_id
        if owner_id is None:
            raise ValueError("there is no owner")
        return owner_id

    async def create(
        self,
        text: object,
        *,
        category: object = "",
        target_date: object = None,
        check_in_rhythm: object = "",
        source: str = "owner",
    ) -> dict:
        self._require_owner()
        cleaned = clean_text(text, MAX_TEXT)
        if not cleaned:
            raise ValueError("goal text must be non-empty")
        cat = validate_category(category)
        due = validate_target_date(target_date)
        rhythm = validate_rhythm(check_in_rhythm)
        now = self._now().isoformat()
        row = await self.app.store.create_goal(
            cleaned, cat, due, rhythm, now, MAX_ACTIVE, source=source,
        )
        if row is None:
            raise ValueError(f"at most {MAX_ACTIVE} active goals")
        await self.app.store.record_owner_event(f"Added goal #{row['id']}: {cleaned[:120]}")
        return row

    async def get(self, goal_id: int) -> dict | None:
        if self.app.owner_id is None:
            return None
        return await self.app.store.get_goal(goal_id)

    async def list_all(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        if self.app.owner_id is None:
            return []
        if status is not None and status != "all":
            status = validate_status(status)
        else:
            status = None
        return await self.app.store.list_goals(status=status, limit=limit)

    async def active(self) -> list[dict]:
        if self.app.owner_id is None:
            return []
        return await self.app.store.active_goals(limit=MAX_ACTIVE)

    async def update(
        self,
        goal_id: int,
        *,
        text: object = None,
        category: object = None,
        target_date: object = ...,
        check_in_rhythm: object = None,
        source: str = "owner",
    ) -> dict:
        self._require_owner()
        existing = await self.app.store.get_goal(goal_id)
        if existing is None:
            raise ValueError(f"Goal #{goal_id} not found.")
        new_text = existing["text"] if text is None else clean_text(text, MAX_TEXT)
        if not new_text:
            raise ValueError("goal text must be non-empty")
        new_cat = existing["category"] if category is None else validate_category(category)
        new_due = existing["target_date"] if target_date is ... else validate_target_date(target_date)
        new_rhythm = existing["check_in_rhythm"] if check_in_rhythm is None else validate_rhythm(check_in_rhythm)
        row = await self.app.store.update_goal(
            goal_id,
            text=new_text,
            category=new_cat,
            target_date=new_due,
            check_in_rhythm=new_rhythm,
            at=self._now().isoformat(),
            source=source,
        )
        if row is None:
            raise ValueError(f"Goal #{goal_id} not found.")
        await self.app.store.record_owner_event(f"Updated goal #{goal_id}: {new_text[:120]}")
        return row

    async def set_status(self, goal_id: int, status: object, *, note: object = "", source: str = "owner") -> dict:
        self._require_owner()
        new_status = validate_status(status)
        cleaned_note = clean_text(note, MAX_NOTE) if note else ""
        row = await self.app.store.set_goal_status(
            goal_id, new_status, cleaned_note, self._now().isoformat(), MAX_ACTIVE, source=source,
        )
        if row == "not_found":
            raise ValueError(f"Goal #{goal_id} not found.")
        if row == "limit":
            raise ValueError(f"at most {MAX_ACTIVE} active goals")
        await self.app.store.record_owner_event(f"Marked goal #{goal_id} as {new_status}")
        return row

    async def add_progress(self, goal_id: int, note: object, *, source: str = "owner") -> dict:
        self._require_owner()
        cleaned_note = clean_text(note, MAX_NOTE)
        if not cleaned_note:
            raise ValueError("progress note must be non-empty")
        row = await self.app.store.add_goal_progress(goal_id, cleaned_note, self._now().isoformat(), source=source)
        if row is None:
            raise ValueError(f"Goal #{goal_id} not found.")
        if source != "reflection":
            await self.app.store.record_owner_event(f"Noted progress on goal #{goal_id}: {cleaned_note[:120]}")
        return row

    async def link_reminder(self, goal_id: int, reminder_id: int, *, source: str = "owner") -> dict:
        owner_id = self._require_owner()
        linked = await self.app.store.link_goal_reminder(
            owner_id, goal_id, reminder_id, self._now().isoformat(), source=source,
        )
        if linked == "goal_not_found":
            raise ValueError(f"Goal #{goal_id} not found.")
        if linked == "reminder_not_found":
            raise ValueError(f"Reminder #{reminder_id} not found.")
        row = await self.app.store.get_goal(goal_id)
        if row is None:
            raise ValueError(f"Goal #{goal_id} not found.")
        await self.app.store.record_owner_event(f"Linked reminder #{reminder_id} to goal #{goal_id}")
        return row

    async def unlink_reminder(self, goal_id: int, reminder_id: int, *, source: str = "owner") -> dict:
        self._require_owner()
        changed = await self.app.store.unlink_goal_reminder(
            goal_id, reminder_id, self._now().isoformat(), source=source,
        )
        row = await self.app.store.get_goal(goal_id)
        if row is None:
            raise ValueError(f"Goal #{goal_id} not found.")
        if changed:
            await self.app.store.record_owner_event(f"Unlinked reminder #{reminder_id} from goal #{goal_id}")
        return row

    async def delete(self, goal_id: int, expected_created_at: str) -> tuple[bool, str]:
        app = self.app
        if app.owner_id is None:
            return False, "There is no owner."
        if not expected_created_at:
            return False, STALE_MESSAGE
        row = await app.store.get_goal(goal_id)
        if row is None:
            return False, f"Goal #{goal_id} not found."
        if not stamp_matches(row["created_at"], expected_created_at):
            return False, STALE_MESSAGE
        event_at = datetime.now(timezone.utc).isoformat()
        event_text = f"Deleted goal #{goal_id}"
        try:
            archive = await asyncio.to_thread(
                stage_deletion,
                app.home.root,
                "goal",
                goal_id,
                event_text=event_text,
                event_at=event_at,
                rows={"goal": row},
            )
        except (OSError, ValueError) as error:
            log.warning("could not archive goal %s: %s", goal_id, type(error).__name__)
            return False, "Could not archive goal before deleting."
        try:
            result = await app.store.delete_goal(goal_id, expected_created_at, event_text, event_at)
        except sqlite3.Error as error:
            log.warning("could not delete goal %s: %s", goal_id, type(error).__name__)
            await asyncio.to_thread(archive.rollback)
            return False, "Could not delete goal."
        if result != "deleted":
            await asyncio.to_thread(archive.rollback)
            return False, STALE_MESSAGE if result == "stale" else f"Goal #{goal_id} not found."
        await asyncio.to_thread(archive.commit)
        await asyncio.to_thread(prune_deletions, app.home.root)
        return True, f"Deleted goal #{goal_id}."

    async def due_checkin(self, now: datetime) -> dict | None:
        if self.app.owner_id is None:
            return None
        return due_for_checkin(await self.active(), now)

    async def record_checkin(self, goal_id: int, now: datetime) -> None:
        await self.app.store.mark_goal_checkin(goal_id, now.astimezone(timezone.utc).isoformat())

    async def turn_note(self, scope: Scope, chat_id: int) -> str:
        """Code-filtered goal context: empty outside the owner DM."""
        owner_id = self.app.owner_id
        if scope != "owner_dm" or owner_id is None or chat_id != owner_id:
            return ""
        return prompt_block(await self.active(), scope, chat_id, owner_id)

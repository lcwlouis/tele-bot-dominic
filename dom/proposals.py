"""Owner approval for changes to Dom's soul and memory."""

from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.home import MAX_SOUL, MAX_SOUL_EDIT, Home

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
MEMORY_FILES = {"USER.md": "private/USER.md", "NOTES.md": "private/NOTES.md", "MEMORY.md": "memory/MEMORY.md"}
REFLECTION_SOURCE = "reflection"
REFLECTION_INTERVAL = timedelta(days=7)


class Refused(ValueError):
    """The proposal remains pending because its change could not be applied."""


def soul_edit_problem(home: Home, old: str, new: str) -> str | None:
    """Why replacing `old` by `new` in SOUL.md cannot be proposed or applied now, or None."""
    if len(old) > MAX_SOUL_EDIT or len(new) > MAX_SOUL_EDIT:
        return f"a soul edit's old and new text can be at most {MAX_SOUL_EDIT} characters each"
    edited = home.edited_soul(old, new)
    if edited is None:
        return "SOUL.md must contain the old text exactly once"
    if len(edited) > MAX_SOUL:
        return f"that edit would make SOUL.md longer than {MAX_SOUL} characters"
    return None


def buttons(proposal_id: int, *, numbered: bool = False) -> list[InlineKeyboardButton]:
    suffix = f" #{proposal_id}" if numbered else ""
    return [
        InlineKeyboardButton(text=f"Approve{suffix}", callback_data=f"prop:yes:{proposal_id}"),
        InlineKeyboardButton(text=f"Reject{suffix}", callback_data=f"prop:no:{proposal_id}"),
    ]


def summary(row: dict) -> str:
    body = json.loads(row["body_json"])
    heading = f"#{row['id']} {row['title']}"
    if row["kind"] == "soul_edit":
        return f"{heading}\n\nReplace:\n{body['old']}\n\nWith:\n{body['new']}"
    if row["kind"] == "memory_fact":
        if row["title"] == body["text"]:
            return f"#{row['id']} {body['file']}: {body['text']}"
        return f"{heading}\n{body['file']}: {body['text']}"
    return heading


class Proposals:
    def __init__(self, app: App):
        self.app = app

    async def create(self, kind: str, title: str, body: dict, source: str, *, notify: bool = True) -> int:
        if kind in ("skill", "plugin"):
            raise NotImplementedError(kind)
        if kind == "soul_edit":
            if not (isinstance(body.get("old"), str) and body["old"] and isinstance(body.get("new"), str)):
                raise ValueError("a soul edit needs old and new text")
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
                raise ValueError(problem)
        elif kind == "memory_fact":
            text, file = body.get("text"), body.get("file")
            if not isinstance(text, str) or not text.strip() or not isinstance(file, str) or file not in MEMORY_FILES:
                raise ValueError("a memory fact needs a memory filename and text")
        else:
            raise ValueError("unknown proposal kind")
        at = self.app.dispatch.now().astimezone(timezone.utc).isoformat()
        proposal_id = await self.app.store.create_proposal(kind, title, body, source, at)
        assert proposal_id is not None
        if notify:
            await self._notify(proposal_id, kind, title, body)
        return proposal_id

    async def _notify(self, proposal_id: int, kind: str, title: str, body: dict, event: str | None = None) -> None:
        app = self.app
        if app.owner_id is None:
            return
        await app.store.record_owner_event(event or f"Proposed #{proposal_id}: {title[:250]}")
        try:
            await self.send({"id": proposal_id, "kind": kind, "title": title, "body_json": json.dumps(body)})
        except Exception:
            log.exception("could not notify the owner about proposal %s", proposal_id)

    async def send(self, row: dict) -> None:
        app = self.app
        text = summary(row)
        for start in range(0, len(text), 2000):
            markup = None
            if start + 2000 >= len(text):
                markup = InlineKeyboardMarkup(inline_keyboard=[buttons(row["id"])])
            await app.bot.send_message(
                app.owner_id, text[start:start + 2000], disable_notification=True, reply_markup=markup,
            )

    async def pending(self) -> list[dict]:
        return await self.app.store.pending_proposals()

    async def reflection_due(self) -> bool:
        since = self.app.dispatch.now().astimezone(timezone.utc) - REFLECTION_INTERVAL
        return not await self.app.store.has_recent_proposal("soul_edit", REFLECTION_SOURCE, since.isoformat())

    async def from_reflection(self, edit: object) -> None:
        if not isinstance(edit, dict):
            return
        old, new, why = (edit.get(key) for key in ("old", "new", "why"))
        if not all(isinstance(value, str) for value in (old, new, why)) or not old or old == new:
            return
        if problem := soul_edit_problem(self.app.home, old, new):
            log.info("dropped a reflection soul edit: %s", problem)
            return
        title = why.strip()[:250] or "Soul edit"
        body = {"old": old, "new": new}
        now = self.app.dispatch.now().astimezone(timezone.utc)
        proposal_id = await self.app.store.create_proposal(
            "soul_edit", title, body, REFLECTION_SOURCE, now.isoformat(),
            since=(now - REFLECTION_INTERVAL).isoformat(),
        )
        if proposal_id is not None:
            # The reason is model-written from chat content; the event row goes back into Dom's own prompt.
            await self._notify(proposal_id, "soul_edit", title, body, event=f"Dom proposed a soul edit (#{proposal_id})")

    async def decide(self, proposal_id: int, approve: bool) -> str:
        app = self.app
        status = "approved" if approve else "rejected"
        at = app.dispatch.now().astimezone(timezone.utc).isoformat()
        try:
            changed = await app.store.decide_proposal(proposal_id, status, at, self._apply)
        except (OSError, ValueError) as error:
            await app.store.record_owner_event(f"Could not approve proposal #{proposal_id}")
            return str(error) if isinstance(error, Refused) else "Could not write the proposal. It is still pending."
        if not changed:
            return "already handled"
        await app.store.record_owner_event(f"{status.capitalize()} proposal #{proposal_id}")
        return status.capitalize()

    def _apply(self, kind: str, body: dict) -> None:
        if kind == "soul_edit":
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
                raise Refused(problem)
            self.app.home.replace_soul(body["old"], body["new"])
            return
        if kind == "memory_fact":
            rel = MEMORY_FILES[body["file"]]
            home = self.app.home.scoped(allow_private=rel.startswith("private/"))
            result = home.memory_write(rel, "add", body["text"])
            if not result.ok:
                raise Refused(f"{body['file']}: {result.error}")
            return
        raise Refused("This proposal kind is unavailable")

    async def expire(self) -> None:
        now = self.app.dispatch.now().astimezone(timezone.utc)
        await self.app.store.expire_proposals((now - timedelta(days=14)).isoformat(), now.isoformat())

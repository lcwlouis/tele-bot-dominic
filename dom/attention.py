"""Schedule the compose phase of dispatches on one event-loop-owned token."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from dom.models import Scope

Tier = Literal["next", "later"]
AGE_PENALTY = timedelta(seconds=60)
CONSECUTIVE_CAP = 2


@dataclass
class Entry:
    chat_id: int
    scope: Scope
    tier: Tier
    since: datetime
    resume: bool
    ready: asyncio.Future[None]
    behind: int | None
    reason: str = ""
    depth: int = 0
    capped: int | None = None


class Attention:
    def __init__(self):
        self.active: Entry | None = None
        self.waiting: dict[int, Entry] = {}
        self._last_chat: int | None = None
        self._consecutive = 0
        self._prompt_tokens = 0
        self._last_call: dict[int, tuple[datetime, int]] = {}

    def can_type(self, chat_id: int) -> bool:
        return self.active is None or self.active.chat_id == chat_id

    def _pick(self) -> None:
        if self.active is not None or not self.waiting:
            return
        entries = list(self.waiting.values())
        others = sum(entry.scope != "owner_dm" for entry in entries)
        capped = self.waiting.get(self._last_chat)
        if others > 1 and capped and capped.scope != "owner_dm" and self._consecutive >= CONSECUTIVE_CAP:
            entries.remove(capped)
        else:
            capped = None

        def priority(entry: Entry) -> tuple[bool, bool, datetime]:
            effective_since = entry.since + (AGE_PENALTY if entry.tier == "later" else timedelta())
            return not entry.resume, entry.scope != "owner_dm", effective_since

        picked = min(entries, key=priority)
        picked.depth = len(self.waiting)
        picked.capped = capped.chat_id if capped else None
        if picked.resume:
            picked.reason = "resume"
        elif picked.scope == "owner_dm":
            picked.reason = "owner"
        elif picked.tier == "later" and any(entry.tier == "next" for entry in entries):
            picked.reason = "aged"
        else:
            picked.reason = picked.tier
        del self.waiting[picked.chat_id]
        self.active = picked
        picked.ready.set_result(None)

    @asynccontextmanager
    async def visit(self, chat_id: int, scope: Scope, tier: Tier, since: datetime, *, resume: bool = False):
        entry = Entry(
            chat_id, scope, tier, since, resume, asyncio.get_running_loop().create_future(),
            self.active.chat_id if self.active is not None else None,
        )
        self.waiting[chat_id] = entry
        self._pick()
        try:
            await asyncio.shield(entry.ready)
            if chat_id == self._last_chat:
                self._consecutive += 1
            else:
                self._last_chat, self._consecutive = chat_id, 1
            yield entry
        finally:
            if self.waiting.get(chat_id) is entry:
                del self.waiting[chat_id]
            if self.active is entry:
                self.active = None
            self._pick()

    def previous_call(self, chat_id: int) -> tuple[datetime, int] | None:
        if (previous := self._last_call.get(chat_id)) is not None:
            at, tokens = previous
            return at, self._prompt_tokens - tokens
        return None

    def record_call(self, chat_id: int, at: datetime, tokens: int) -> None:
        self._prompt_tokens += tokens
        self._last_call[chat_id] = at, self._prompt_tokens

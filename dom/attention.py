"""Schedule the compose phase of dispatches on one event-loop-owned token."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from xml.sax.saxutils import escape

from dom.models import Scope

Tier = Literal["next", "later"]
AGE_PENALTY = timedelta(seconds=60)
CONSECUTIVE_CAP = 2
FACT_LIFETIME = timedelta(minutes=30)
RECENT_CONVERSATION = timedelta(minutes=10)
FACT_INSTRUCTION = (
    'Also add "elsewhere": one short line of plain facts about what you did here, for your own memory. '
    'No quotes, no names of people outside this chat.'
)
_QUOTED = re.compile(r"""["“]([^"”]+)["”]|(?<!\w)'([^']+)'(?!\w)|‘([^’]+)’|«([^»]+)»|`([^`]+)`""")
_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_QUOTES = {'"': "&quot;"}


def _words(text: str) -> list[str]:
    return [word.replace("'", "").replace("’", "") for word in _WORDS.findall(text.casefold())]


def _runs(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[index:index + 6]) for index in range(len(words) - 5)}


def filter_fact(line: str, messages: list[str]) -> str:
    line = " ".join(line.split())
    if any(len(_words(part)) >= 4 for match in _QUOTED.finditer(line) for part in match.groups() if part):
        return ""
    runs = _runs(_words(line))
    if runs and any(runs & _runs(_words(text)) for text in messages):
        return ""
    return line[:120]


@dataclass(frozen=True)
class Fact:
    chat_id: int
    scope: Scope
    title: str
    text: str
    at: datetime


LaneName = Literal["main", "fast"]


@dataclass
class Entry:
    chat_id: int
    scope: Scope
    tier: Tier
    since: datetime
    resume: bool
    ready: asyncio.Future[None]
    behind: int | None
    lane: LaneName = "main"
    reason: str = ""
    depth: int = 0
    capped: int | None = None


class Lane:
    """One compose token and the chats waiting for it. Turns of one lane compose one at a time."""

    def __init__(self, name: LaneName):
        self.name = name
        self.active: Entry | None = None
        self.waiting: dict[int, Entry] = {}
        self._last_chat: int | None = None
        self._consecutive = 0

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
            self.active.chat_id if self.active is not None else None, self.name,
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


class Attention:
    """The lanes plus what they share: the call history behind the cache metrics and the facts about other chats.

    The fast lane (`fast_lane`) is the owner DM's own token, so its turns never wait for a turn in another chat. Nothing
    but the owner DM ever uses it, so one chat's turns still run one at a time, in order, through the turn slots.
    """

    def __init__(self, *, fast_lane: bool = False):
        self.main, self.fast = Lane("main"), Lane("fast")
        self.fast_lane = fast_lane
        self._prompt_tokens = 0
        self._last_call: dict[int, tuple[datetime, int]] = {}
        self._last_conversation: tuple[int, datetime] | None = None
        self._facts: deque[Fact] = deque(maxlen=3)

    def lane_for(self, scope: Scope) -> Lane:
        return self.fast if self.fast_lane and scope == "owner_dm" else self.main

    def can_type(self, chat_id: int, scope: Scope) -> bool:
        return self.lane_for(scope).can_type(chat_id)

    def visit(self, chat_id: int, scope: Scope, tier: Tier, since: datetime, *, resume: bool = False):
        return self.lane_for(scope).visit(chat_id, scope, tier, since, resume=resume)

    def previous_call(self, chat_id: int) -> tuple[datetime, int] | None:
        if (previous := self._last_call.get(chat_id)) is not None:
            at, tokens = previous
            return at, self._prompt_tokens - tokens
        return None

    def record_call(self, chat_id: int, at: datetime, tokens: int) -> None:
        self._prompt_tokens += tokens
        self._last_call[chat_id] = at, self._prompt_tokens

    def finish_conversation(
        self, chat_id: int, scope: Scope, at: datetime, *, title: str = "", fact: str = "",
    ) -> None:
        self._last_conversation = chat_id, at
        if scope != "owner_dm" and fact:
            self._facts.append(Fact(chat_id, scope, " ".join(title.split())[:80] or scope, fact, at))

    def elsewhere(self, chat_id: int, scope: Scope, now: datetime) -> str:
        if scope != "owner_dm":
            if self._last_conversation is not None:
                previous, at = self._last_conversation
                if previous != chat_id and now - at < RECENT_CONVERSATION:
                    return "you were just in another conversation"
            return ""
        return "\n".join(
            f'- {int((now - fact.at).total_seconds() // 60)} min ago in "{escape(fact.title, _QUOTES)}" '
            f'({fact.scope}): {escape(fact.text)}'
            for fact in reversed(self._facts) if fact.chat_id != chat_id and now - fact.at < FACT_LIFETIME
        )

"""Tools the model may call during a turn, each limited to the scopes that may use it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from dom.home import Home
from dom.models import Scope
from dom.store import Store

ALL_SCOPES: set[Scope] = {"owner_dm", "friend_dm", "group"}


@dataclass(frozen=True)
class TurnCtx:
    scope: Scope
    chat_id: int
    sender_id: int
    home: Home
    store: Store
    owner_id: int | None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict  # JSON schema of the arguments
    handler: Callable[[dict, TurnCtx], Awaitable[str]]
    scopes: set[Scope]

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.schema},
        }


# ---- memory ------------------------------------------------------------------

_MEMORY_FILE = re.compile(r"^(?:(MEMORY|HABITS|USER|NOTES)\.md|people/(\d+)\.md|chats/(-?\d+)\.md)$")


def _memory_target(file: str, ctx: TurnCtx) -> tuple[str | None, str | None]:
    """Map a tool-facing file name to (home-relative path, error)."""
    match = _MEMORY_FILE.match(file or "")
    if not match:
        return None, f"unknown memory file {file!r}"
    named, person, chat = match.groups()
    if named in ("USER", "NOTES"):
        if ctx.scope != "owner_dm":
            return None, f"{file} is not available here"
        return f"private/{named}.md", None
    if named:
        return f"memory/{named}.md", None
    if person:
        return f"memory/people/{person}.md", None
    if ctx.scope != "owner_dm" and int(chat) != ctx.chat_id:
        return None, "you may only edit this chat's own notes here"
    return f"memory/chats/{chat}.md", None


async def _memory(args: dict, ctx: TurnCtx) -> str:
    rel, error = _memory_target(args.get("file", ""), ctx)
    if rel is None:
        return json.dumps({"ok": False, "error": error})
    home = ctx.home.scoped(allow_private=ctx.scope == "owner_dm")
    result = home.memory_write(rel, args.get("op", ""), args.get("text", ""), args.get("old"))
    return json.dumps({"ok": result.ok, "content": result.content, "error": result.error})


MEMORY_TOOL = Tool(
    name="memory",
    description=(
        "Edit your own notes. Each file has a small character limit; when a write is refused you get the "
        "current content back, so merge or remove entries first. Files: MEMORY.md (world and friend-circle "
        "knowledge), HABITS.md (your rhythm: sleep, busy hours), people/<user_id>.md (one friend), "
        "chats/<chat_id>.md (this chat's running jokes and vibe), and in the owner's DM only USER.md "
        "(the owner) and NOTES.md (assistant notes). op=add appends a bullet; op=replace swaps the unique "
        "`old` text for `text`; op=remove deletes the line containing the unique `old` text."
    ),
    schema={
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "op": {"type": "string", "enum": ["add", "replace", "remove"]},
            "text": {"type": "string"},
            "old": {"type": "string"},
        },
        "required": ["file", "op"],
    },
    handler=_memory,
    scopes=set(ALL_SCOPES),
)


# ---- search_history ------------------------------------------------------------

async def _search_history(args: dict, ctx: TurnCtx) -> str:
    limit = args.get("limit", 10)
    hits = await ctx.store.search_chat_history(
        str(args.get("query", "")), str(ctx.chat_id), limit if isinstance(limit, int) else 10
    )
    return json.dumps(
        [
            {k: h[k] for k in ("chat_id", "msg_id", "sender_name", "text", "created_at")}
            for h in hits
        ],
        ensure_ascii=False,
    )


SEARCH_HISTORY_TOOL = Tool(
    name="search_history",
    description=(
        "Full-text search of older messages beyond the chat window, to recall what was said earlier. "
        "Searches this chat only; in the owner's DM it searches every chat."
    ),
    schema={
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    },
    handler=_search_history,
    scopes=set(ALL_SCOPES),
)


def core_tools() -> list[Tool]:
    return [MEMORY_TOOL, SEARCH_HISTORY_TOOL]

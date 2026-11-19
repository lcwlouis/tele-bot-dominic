"""Tools the model may call while replying, each limited to the scopes that may use it. All read-only:
notes are written by the background memory pass (dom/memory_pass.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from dom.home import Home
from dom.models import Scope
from dom.presence import Habits, Presence
from dom.store import Store

ALL_SCOPES: set[Scope] = {"owner_dm", "friend_dm", "group"}


@dataclass(frozen=True)
class TurnCtx:
    scope: Scope
    chat_id: int
    sender_id: int | None
    home: Home
    store: Store
    owner_id: int | None
    presence: Presence | None = None
    habits: Habits | None = None


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


async def _extend_online(args: dict, ctx: TurnCtx) -> str:
    minutes = args.get("minutes")
    if type(minutes) not in (int, float) or not 0 < minutes <= 30:
        return "error: minutes must be greater than 0 and at most 30"
    if ctx.presence is None or ctx.habits is None:
        return "error: presence is unavailable"
    until = await ctx.presence.extend_online(ctx.chat_id, ctx.habits, minutes)
    return json.dumps({"online_until": until.isoformat()})


EXTEND_ONLINE_TOOL = Tool(
    name="extend_online",
    description=(
        "Add up to 30 minutes to this chat's online session when you want to stay longer. A session lasts "
        "at most 2 hours in total and does not run into your sleep hours."
    ),
    schema={
        "type": "object",
        "properties": {"minutes": {"type": "number", "exclusiveMinimum": 0, "maximum": 30}},
        "required": ["minutes"],
    },
    handler=_extend_online,
    scopes=set(ALL_SCOPES),
)


def core_tools() -> list[Tool]:
    return [SEARCH_HISTORY_TOOL, EXTEND_ONLINE_TOOL]

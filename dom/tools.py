"""Tools the model may call while replying, each limited to the scopes that may use it.
notes are written by the background memory pass (dom/memory_pass.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from dom.home import Home
from dom.models import Scope
from dom.presence import Habits, Presence
from dom.search import SearchClient
from dom.store import Store
from dom.usage import ModelCallSkipped, model_request

if TYPE_CHECKING:
    from dom.desktop.worker import DesktopWorker
    from dom.reminders import Reminders

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
    model_allowed: Callable[[], Awaitable[bool]] | None = None
    reminders: Reminders | None = None
    desktop: DesktopWorker | None = None


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


async def _remind(args: dict, ctx: TurnCtx) -> str:
    if ctx.scope != "owner_dm" or ctx.chat_id != ctx.owner_id or ctx.reminders is None:
        return "error: reminders are available only in the owner's DM"
    reminder_id = await ctx.reminders.create(
        args.get("when"), args.get("text"), args.get("importance", "normal"), args.get("repeat", ""),
    )
    return json.dumps({"id": reminder_id})


REMIND_TOOL = Tool(
    name="remind",
    description="Schedule a reminder for the owner. Give an ISO-8601 date and time in the configured timezone or with an offset.",
    schema={
        "type": "object",
        "properties": {
            "when": {"type": "string"},
            "text": {"type": "string"},
            "importance": {"type": "string", "enum": ["critical", "normal", "casual"], "default": "normal"},
            "repeat": {"type": "string", "enum": ["", "daily", "weekdays", "weekly"], "default": ""},
        },
        "required": ["when", "text"],
    },
    handler=_remind,
    scopes={"owner_dm"},
)


def _web_search_tool(client: SearchClient) -> Tool:
    calls = 0

    async def web_search(args: dict, ctx: TurnCtx) -> str:
        nonlocal calls
        if calls >= 2:
            return "error: web_search is limited to two calls per turn"
        calls += 1
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "search failed: query must be non-empty text"
        if ctx.model_allowed is None:
            return "search unavailable right now"
        try:
            with model_request("search", ctx.chat_id, ctx.model_allowed):
                return await client.search(query.strip())
        except ModelCallSkipped:
            return "search unavailable right now"
        except (TimeoutError, httpx.TimeoutException):
            return "search failed: request timed out"
        except httpx.HTTPStatusError as exc:
            return f"search failed: HTTP {exc.response.status_code}"
        except Exception as exc:
            return f"search failed: {type(exc).__name__}"

    return Tool(
        name="web_search",
        description="Find current facts and sources on the web. Use at most twice per turn.",
        schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        handler=web_search,
        scopes=set(ALL_SCOPES),
    )


async def _start_desktop_task(args: dict, ctx: TurnCtx) -> str:
    if ctx.scope != "owner_dm" or ctx.chat_id != ctx.owner_id or ctx.desktop is None:
        return "error: desktop tasks are available only in the owner's DM"
    try:
        task_id = await ctx.desktop.enqueue(ctx.chat_id, args.get("brief"))
    except ValueError as exc:
        return str(exc)
    return json.dumps({"id": task_id})


START_DESKTOP_TASK = Tool(
    name="start_desktop_task",
    description=(
        "Start work on the desktop in the background. Reply in character right away (e.g. 'give me a few min'); "
        "do not wait. Write a self-contained brief using only what the owner explicitly provided for this task. "
        "Never include secrets, private notes, conversation history, owner events, or memory files."
    ),
    schema={"type": "object", "properties": {"brief": {"type": "string", "maxLength": 2000}}, "required": ["brief"]},
    handler=_start_desktop_task, scopes={"owner_dm"},
)


def core_tools(search: SearchClient | None = None, desktop: DesktopWorker | None = None) -> list[Tool]:
    tools = [SEARCH_HISTORY_TOOL, EXTEND_ONLINE_TOOL, REMIND_TOOL]
    if search is not None:
        tools.append(_web_search_tool(search))
    if desktop is not None and desktop.available():
        tools.append(START_DESKTOP_TASK)
    return tools

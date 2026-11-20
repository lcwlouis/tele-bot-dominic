"""Tools the model may call while replying, each limited to the scopes that may use it.
notes are written by the background memory pass (dom/memory_pass.py)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from dom import own_code
from dom._version import VERSION
from dom.briefs import LIMITS as BRIEF_LIMITS, write_brief
from dom.guard import check_local_private_text

from dom.home import Home
from dom.models import Scope
from dom.presence import Habits, Presence
from dom.search import SearchClient
from dom.skills import BODY_LIMIT, DESCRIPTION_LIMIT, NAME_LIMIT, skills_list, view_text
from dom.store import Store
from dom.usage import ModelCallSkipped, model_request

if TYPE_CHECKING:
    from dom.desktop.worker import DesktopWorker
    from dom.reminders import Reminders
    from dom.telegram.handlers import App

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
    app: App | None = None
    t3_notes: list[str] = field(default_factory=list)

    def is_owner_dm(self) -> bool:
        return (
            self.app is not None and self.owner_id is not None and self.scope == "owner_dm"
            and self.sender_id == self.chat_id == self.owner_id == self.app.owner_id
        )


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


# ---- skills: public procedures, shared with the desktop worker through dom/skills.py ------------

async def _skills_list(args: dict, ctx: TurnCtx) -> str:
    return json.dumps(skills_list(ctx.home), ensure_ascii=False)


SKILLS_LIST_TOOL = Tool(
    name="skills_list",
    description="List approved skills. Each has a name and description; read one with skill_view.",
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_skills_list,
    scopes=set(ALL_SCOPES),
)


async def _skill_view(args: dict, ctx: TurnCtx) -> str:
    return view_text(ctx.home, args.get("name"))


SKILL_VIEW_TOOL = Tool(
    name="skill_view",
    description="Read an approved skill. Use it when a skill's description fits what you are doing.",
    schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    handler=_skill_view,
    scopes=set(ALL_SCOPES),
)


SKILL_PENDING_LIMIT = 3


async def _skill_manage(args: dict, ctx: TurnCtx) -> str:
    if ctx.app is None:
        return "error: skills can only be proposed during a chat turn"
    skill = {key: args.get(key) for key in ("name", "description", "body")}
    waiting = sum(row["kind"] == "skill" for row in await ctx.app.proposals.pending())
    if waiting >= SKILL_PENDING_LIMIT:  # any chat can prompt a proposal; don't let one flood the owner's DM
        return f"error: {waiting} skill proposals already wait for the owner; try again after they decide"
    try:
        proposal_id = await ctx.app.proposals.create("skill", f"Skill: {skill['name']}", skill, "chat")
    except (OSError, ValueError) as error:
        return f"error: {error}"
    return json.dumps({"id": proposal_id, "status": "waiting for the owner's approval"})


SKILL_MANAGE_TOOL = Tool(
    name="skill_manage",
    description=(
        "Propose a skill for the owner to approve. Reuse an existing name to propose a change to it. "
        "Nothing changes until they approve. Skills are public: write a reusable procedure using only public "
        "information, never private notes, chat history, memory or credentials."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": NAME_LIMIT},
            "description": {"type": "string", "maxLength": DESCRIPTION_LIMIT},
            "body": {"type": "string", "maxLength": BODY_LIMIT},
        },
        "required": ["name", "description", "body"],
    },
    handler=_skill_manage,
    scopes=set(ALL_SCOPES),
)


def core_tools(search: SearchClient | None = None, desktop: DesktopWorker | None = None) -> list[Tool]:
    tools = [SEARCH_HISTORY_TOOL, EXTEND_ONLINE_TOOL, REMIND_TOOL, SKILLS_LIST_TOOL, SKILL_VIEW_TOOL, SKILL_MANAGE_TOOL]
    if search is not None:
        tools.append(_web_search_tool(search))
    if desktop is not None and desktop.available():
        tools.append(START_DESKTOP_TASK)
    return tools


async def _write_brief(args: dict, ctx: TurnCtx) -> str:
    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: briefs can only be written in the owner's DM"
    fields = {field: args.get(field) for field in BRIEF_LIMITS}
    try:
        check_local_private_text(app.config, app.home, [text for text in fields.values() if isinstance(text, str)])
        day = app.dispatch.now().astimezone(app.dispatch.presence.tz).date()
        brief = write_brief(app.home, day, **fields)
    except ValueError as error:
        return f"error: {error}"
    except OSError:
        return "error: could not save the brief"
    await app.store.record_owner_event(f"Dom wrote a core-change brief ({brief.filename})")
    document = BufferedInputFile(brief.markdown.encode("utf-8"), filename=brief.filename)
    try:
        await app.bot.send_document(ctx.chat_id, document, caption=f"Core-change brief: {brief.title}")
    except TelegramAPIError:
        return json.dumps({"filename": brief.filename, "error": "Saved the brief, but could not send the document."})
    return json.dumps({"filename": brief.filename})


WRITE_BRIEF_TOOL = Tool(
    name="write_brief",
    description=(
        "Write and send a brief for a coding agent working on Dom's public code. "
        "Describe the problem, proposed change and acceptance criteria using only public information. "
        "Exclude private notes, chat history, memory and credentials."
    ),
    schema={
        "type": "object",
        "properties": {
            field: {"type": "string", "maxLength": limit} for field, limit in BRIEF_LIMITS.items()
        },
        "required": list(BRIEF_LIMITS),
    },
    handler=_write_brief,
    scopes={"owner_dm"},
)


async def _self_inspect(args: dict, ctx: TurnCtx) -> str:
    from dom.capabilities import desktop_status
    from dom.plugins.manifest import SCOPES
    from dom.traits import global_preset_name

    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: self_inspect is available only in the owner's DM"
    enabled = {scope: [tool.name for tool in await enabled_tools(app, scope)] for scope in SCOPES}
    return json.dumps({
        "version": VERSION,
        "changelog_latest": own_code.latest_release(own_code.installed_base()),
        "tools": enabled,
        "approved_plugins": [
            {"name": plugin.manifest.name, "hash": plugin.hash} for plugin in await app.plugins.approved()
        ],
        "plugin_sandbox_failure": app.plugin_runner.last_failure,
        "desktop": desktop_status(app, "owner_dm"),
        "gifs": "on" if app.gifs is not None else "off (set KLIPY_API_KEY in .env to turn them on)",
        "models": {kind: endpoint.model for kind, endpoint in app.config.models.items()},
        "last_errors": await app.store.recent_turn_errors(),
        "preset": {
            "everywhere": await global_preset_name(app.store),
            "chats_with_own": [
                {"title": row["title"] or row["chat_id"], "preset": row["preset"]}
                for row in await app.store.list_chats("approved") if row["preset"]
            ],
        },
    })


SELF_INSPECT_TOOL = Tool(
    name="self_inspect",
    description=(
        "Read Dom's version, newest changelog entry, available tools by chat scope, approved plugins, desktop status, whether GIFs are on, "
        "models, the preset used everywhere with the chats that override it, and recent errors. "
        "When someone asks about you, answer in your own words like a person would; "
        "give exact numbers or internal names only when asked for them."
    ),
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_self_inspect,
    scopes={"owner_dm"},
)


async def _own_code(args: dict, ctx: TurnCtx) -> str:
    if not ctx.is_owner_dm():
        return "error: own_code is available only in the owner's DM"
    base, action = own_code.installed_base(), args.get("action")
    path, query = args.get("path"), args.get("query")
    if action == "changelog":
        run = (own_code.changelog, base)
    elif action == "list":
        run = (own_code.list_files, base, path if isinstance(path, str) else "")
    elif action == "read":
        run = (own_code.read_file, base, path)
    elif action == "search":
        run = (own_code.search, base, query, path if isinstance(path, str) else "")
    else:
        return "error: action must be changelog, list, read or search"
    return await asyncio.to_thread(*run)


OWN_CODE_TOOL = Tool(
    name="own_code",
    description=(
        "Read your own source code, docs and changelog (read-only). Actions: changelog (latest releases); "
        "list (files with sizes; optional path narrows to a folder); read (one file by relative path such as "
        "dom/tools.py or docs/privacy.md); search (case-insensitive text search, optional path folder)."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["changelog", "list", "read", "search"]},
            "path": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["action"],
    },
    handler=_own_code,
    scopes={"owner_dm"},
)


async def enabled_tools(app: App, scope: Scope) -> list[Tool]:
    from dom.devices import FIND_DEVICES_TOOL
    from dom.plugins.tools import DRAFT_PLUGIN_TOOL, offered

    tools = core_tools(app.search_client, app.desktop_worker)
    if scope == "owner_dm" and app.t3 is not None and app.t3.available():
        from dom.t3.tools import TOOLS

        tools.extend(TOOLS)
    tools.extend((DRAFT_PLUGIN_TOOL, WRITE_BRIEF_TOOL, SELF_INSPECT_TOOL, OWN_CODE_TOOL, FIND_DEVICES_TOOL))
    return [tool for tool in tools if scope in tool.scopes] + await offered(app, scope)

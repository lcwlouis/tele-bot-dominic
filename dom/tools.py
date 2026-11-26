"""Tools the model may call while replying, each limited to the scopes that may use it.
notes are written by the background memory pass (dom/memory_pass.py)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from dom import global_search, own_code
from dom._version import VERSION
from dom.briefs import LIMITS as BRIEF_LIMITS, write_brief
from dom.cross_chat import MAX_REASON, MAX_TEXT
from dom.guard import check_local_private_text

from dom.home import Home
from dom.models import Scope
from dom.presence import Habits, Presence
from dom.search import SearchClient, SearchRouter
from dom.skills import BODY_LIMIT, NAME_LIMIT, skills_list, view_text
from dom.store import Store
from dom.telegram.executor import FloodBudget, FloodBudgetExhausted, retry_telegram
from dom.telegram.format import format_text
from dom.traits import AUTO, TASK_QUIET, clean_reason, global_preset_name
from dom.usage import ModelCallSkipped, model_request

if TYPE_CHECKING:
    from dom.desktop.fleet import DesktopFleet
    from dom.reminders import Reminders
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
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
    desktop: DesktopFleet | None = None
    app: App | None = None
    t3_notes: list[str] = field(default_factory=list)
    participant_ids: tuple[int, ...] = ()

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


# ---- search ----------------------------------------------------------------------

async def _search(args: dict, ctx: TurnCtx) -> str:
    """The owner's DM searches everything Dom holds (dom/global_search.py). Anywhere else the scope is fixed in code to
    this chat's own history: memory files, other chats and private notes cannot surface, whatever the model asks."""
    query = str(args.get("query", ""))
    mode = args.get("mode", "keyword")
    mode = mode if mode in ("keyword", "meaning", "entity") else "keyword"
    if ctx.scope == "owner_dm" and ctx.chat_id == ctx.owner_id and ctx.sender_id in (None, ctx.owner_id):
        found = await global_search.search_all(
            ctx.store, ctx.home, query, mode=mode, knowledge=ctx.app.knowledge if ctx.app else None,
        )
        return json.dumps(
            [{"kind": h.kind, "title": h.title, "text": h.text, "at": h.at} for hits in found.values() for h in hits],
            ensure_ascii=False,
        )
    limit = args.get("limit", 10)
    limit = limit if isinstance(limit, int) else 10
    people = [*ctx.participant_ids, *([ctx.sender_id] if ctx.sender_id is not None else [])]
    indexed = []
    if ctx.app is not None:
        indexed = await ctx.app.knowledge.search(
            query, requester_chat_id=ctx.chat_id, owner=False, mode=mode, limit=limit, participants=people,
        )
    recent: list[dict] = []
    if mode != "entity":
        # The index lags the newest messages, and the history search keeps what the index does not: sender names,
        # the pinned-day recency rule and chat-migration aliases. Both answer; the same message appears once.
        recent = await ctx.store.search_chat_history(
            query, str(ctx.chat_id), limit,
            keep_days=await asyncio.to_thread(ctx.home.scoped(allow_private=False).core_days),
        )
    found = [{k: h[k] for k in ("chat_id", "msg_id", "sender_name", "text", "created_at")} for h in recent]
    seen = {row["id"] for row in recent}  # the same message once; two messages with equal text both stay
    for hit in indexed:
        if hit.message_id is not None and hit.message_id in seen:
            continue
        found.append({"text": hit.text, "file": hit.file, "at": hit.at, "entities": hit.entities})
    return json.dumps(found, ensure_ascii=False)


SEARCH_TOOL = Tool(
    name="search",
    description=(
        "Full-text search of older messages beyond the chat window, to recall what was said earlier. "
        "Searches this chat only; in the owner's DM it searches everything you hold: every chat, memory files, "
        "briefs, reminders, desktop tasks, skills and plugins. Keyword is the fast default; use meaning when exact "
        "words miss, or entity to retrieve facts and linked people, places, projects and goals."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["keyword", "meaning", "entity"], "default": "keyword"},
        },
        "required": ["query"],
    },
    handler=_search,
    scopes=set(ALL_SCOPES),
)


async def _remind(args: dict, ctx: TurnCtx) -> str:
    if ctx.scope != "owner_dm" or ctx.chat_id != ctx.owner_id or ctx.reminders is None:
        return "error: reminders are available only in the owner's DM"
    goal_id = args.get("goal_id")
    if goal_id is not None:
        if type(goal_id) is not int or goal_id <= 0:
            return "error: goal_id must be a positive integer"
        if ctx.app is None or await ctx.app.goals.get(goal_id) is None:
            return f"error: Goal #{goal_id} not found."
    reminder_id = await ctx.reminders.create(
        args.get("when"), args.get("text"), args.get("importance", "normal"), args.get("repeat", ""),
    )
    if goal_id is not None and ctx.app is not None:
        await ctx.app.goals.link_reminder(goal_id, reminder_id, source="dom")
        return json.dumps({"id": reminder_id, "goal_id": goal_id})
    return json.dumps({"id": reminder_id})


REMIND_TOOL = Tool(
    name="remind",
    description=(
        "Schedule a reminder for the owner, only when they ask for one or agree to your offer of one; never as a "
        "placeholder, a test or a note to yourself. Give an ISO-8601 date and time in the configured timezone or "
        "with an offset. Pass goal_id to link the reminder to one of the owner's goals."
    ),
    schema={
        "type": "object",
        "properties": {
            "when": {"type": "string"},
            "text": {"type": "string"},
            "importance": {"type": "string", "enum": ["critical", "normal", "casual"], "default": "normal"},
            "repeat": {"type": "string", "enum": ["", "daily", "weekdays", "weekly"], "default": ""},
            "goal_id": {"type": "integer"},
        },
        "required": ["when", "text"],
    },
    handler=_remind,
    scopes={"owner_dm"},
)


async def _goals(args: dict, ctx: TurnCtx) -> str:
    app = ctx.app
    if (
        app is None or ctx.owner_id is None or ctx.scope != "owner_dm"
        or ctx.chat_id != ctx.owner_id or app.owner_id != ctx.owner_id
        or (ctx.sender_id is not None and ctx.sender_id != ctx.owner_id)
    ):
        return "error: goals are available only in the owner's DM"
    action = args.get("action")
    try:
        if action == "list":
            status = args.get("status")
            rows = await app.goals.list_all(status=status if isinstance(status, str) and status else None)
            return json.dumps([
                {
                    "id": g["id"], "text": g["text"], "category": g["category"],
                    "target_date": g["target_date"], "check_in_rhythm": g["check_in_rhythm"],
                    "status": g["status"], "progress_note": g["progress_note"],
                    "reminders": [{"id": r["id"], "text": r["text"], "due_at": r["due_at"], "status": r["status"]} for r in g.get("reminders", [])],
                }
                for g in rows
            ], ensure_ascii=False)
        if action == "create":
            goal = await app.goals.create(
                args.get("text"),
                category=args.get("category", ""),
                target_date=args.get("target_date"),
                check_in_rhythm=args.get("check_in_rhythm", ""),
                source="owner",
            )
            return json.dumps({"id": goal["id"], "status": goal["status"]}, ensure_ascii=False)
        gid = args.get("goal_id")
        if type(gid) is not int or gid <= 0:
            return "error: goal_id must be a positive integer"
        if action == "update":
            target = args.get("target_date") if "target_date" in args else ...
            goal = await app.goals.update(
                gid,
                text=args.get("text") if "text" in args else None,
                category=args.get("category") if "category" in args else None,
                target_date=target,
                check_in_rhythm=args.get("check_in_rhythm") if "check_in_rhythm" in args else None,
                source="owner",
            )
            return json.dumps({"id": goal["id"], "text": goal["text"], "status": goal["status"]}, ensure_ascii=False)
        if action == "set_status":
            goal = await app.goals.set_status(gid, args.get("status"), note=args.get("note", ""), source="owner")
            return json.dumps({"id": goal["id"], "status": goal["status"]}, ensure_ascii=False)
        if action == "note":
            goal = await app.goals.add_progress(gid, args.get("note"), source="owner")
            return json.dumps({"id": goal["id"], "progress_note": goal["progress_note"]}, ensure_ascii=False)
        if action in ("link_reminder", "unlink_reminder"):
            rid = args.get("reminder_id")
            if type(rid) is not int or rid <= 0:
                return "error: reminder_id must be a positive integer"
            if action == "link_reminder":
                await app.goals.link_reminder(gid, rid, source="owner")
                return json.dumps({"goal_id": gid, "reminder_id": rid, "linked": True})
            await app.goals.unlink_reminder(gid, rid, source="owner")
            return json.dumps({"goal_id": gid, "reminder_id": rid, "linked": False})
        return "error: action must be list, create, update, set_status, note, link_reminder or unlink_reminder"
    except ValueError as exc:
        return f"error: {exc}"


GOALS_TOOL = Tool(
    name="goals",
    description=(
        "Owner's DM only. Manage the owner's personal goals when they tell you about one or ask to update, "
        "pause, finish, drop, note progress on, or link a reminder to a goal. Never mention goals in other chats. "
        "Categories: health, relationships, money, career, interests, productivity, other. "
        "Check-in rhythms: daily, every_few_days, weekly, biweekly, monthly (or empty for no automatic check-ins)."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "set_status", "note", "link_reminder", "unlink_reminder"],
            },
            "goal_id": {"type": "integer"},
            "text": {"type": "string", "maxLength": 400},
            "category": {
                "type": "string",
                "enum": ["", "health", "relationships", "money", "career", "interests", "productivity", "other"],
            },
            "target_date": {"type": "string", "description": "Optional YYYY-MM-DD target date (empty string to clear)"},
            "check_in_rhythm": {
                "type": "string",
                "enum": ["", "daily", "every_few_days", "weekly", "biweekly", "monthly"],
            },
            "status": {"type": "string", "enum": ["active", "paused", "done", "dropped", "all"]},
            "note": {"type": "string", "maxLength": 300},
            "reminder_id": {"type": "integer"},
        },
        "required": ["action"],
    },
    handler=_goals,
    scopes={"owner_dm"},
)


def _web_search_tool(client: SearchClient | SearchRouter) -> Tool:
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
        task_id = await ctx.desktop.enqueue(ctx.chat_id, args.get("brief"), args.get("container"))
    except ValueError as exc:  # nothing waits for a slot, so say so: the model once told the owner it was "on the waiting list"
        return f"Not started and not queued: {exc}. Tell the owner it did not start; they can ask again later."
    return json.dumps({"id": task_id})


START_DESKTOP_TASK = Tool(
    name="start_desktop_task",
    description=(
        "Start work on the desktop in the background. Reply in character right away (e.g. 'give me a few min'); "
        "do not wait. Write a self-contained brief using only what the owner explicitly provided for this task. "
        "Never include secrets, private notes, conversation history, owner events, or memory files. "
        "When the task needs the owner's login, write in the brief that the worker should call handoff at the login "
        "page: the owner then gets a card with a live view, logs in there and taps Done, and the task carries on. "
        "Never ask the owner for a password, code or other secret in chat, and never offer to type one in. "
        "Give a container name (lowercase letters, digits, hyphens) to keep a project's work and logins on its own "
        "desktop; leave it out for the main one. Each container runs one task at a time, and a host refuses "
        "(with a reason) when it is full or short of memory."
    ),
    schema={"type": "object", "properties": {
        "brief": {"type": "string", "maxLength": 2000},
        "container": {"type": "string", "maxLength": 24},
    }, "required": ["brief"]},
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


def _key(name: object) -> str:
    return name.strip().lower() if isinstance(name, str) else ""


def _skill_name(row: dict) -> str:
    try:
        return _key(json.loads(row["body_json"]).get("name"))
    except (ValueError, AttributeError):
        return ""


async def _skill_manage(args: dict, ctx: TurnCtx) -> str:
    if ctx.app is None:
        return "error: skills can only be proposed during a chat turn"
    skill = {key: args[key] for key in ("name", "brief", "description", "body") if key in args}
    name = skill.get("name")
    pending = [row for row in await ctx.app.proposals.pending() if row["kind"] == "skill"]
    same = next((row for row in pending if _skill_name(row) == _key(name)), None)
    if same is not None:  # one card per skill in the owner's DM; a revision waits for their decision
        return (
            f"error: proposal #{same['id']} for this skill already waits for the owner; tell them what you would "
            "change, and propose the revision after they decide"
        )
    waiting = len(pending)
    if waiting >= SKILL_PENDING_LIMIT:  # any chat can prompt a proposal; don't let one flood the owner's DM
        return f"error: {waiting} skill proposals already wait for the owner; try again after they decide"
    try:
        proposal_id = await ctx.app.proposals.create("skill", f"Skill: {name}", skill, "chat")
    except (OSError, ValueError) as error:
        return f"error: {error}"
    return json.dumps({"id": proposal_id, "status": "waiting for the owner's approval"})


SKILL_MANAGE_TOOL = Tool(
    name="skill_manage",
    description=(
        "Propose a skill for the owner to approve. Reuse an existing name to propose a change to it. "
        "Nothing changes until they approve. Give a brief for a reusable procedure anyone could follow. Skills "
        "are public and used in every chat. Leave out personal details, private notes, chat history, memory and credentials."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": NAME_LIMIT},
            "brief": {"type": "string", "maxLength": BODY_LIMIT},
        },
        "required": ["name", "brief"],
    },
    handler=_skill_manage,
    scopes=set(ALL_SCOPES),
)


async def _read_notes(args: dict, ctx: TurnCtx) -> str:
    from dom.prompt import read_fresh_notes

    allow_private = (
        ctx.scope == "owner_dm"
        and ctx.owner_id is not None
        and ctx.chat_id == ctx.owner_id
        and (ctx.sender_id is None or ctx.sender_id == ctx.owner_id)
        and (ctx.app is None or ctx.app.owner_id == ctx.owner_id)
    )
    participants = list(ctx.participant_ids)
    if ctx.sender_id is not None and ctx.sender_id not in participants:
        participants.append(ctx.sender_id)
    return read_fresh_notes(ctx.home, ctx.scope, ctx.chat_id, participants, allow_private=allow_private)


READ_NOTES_TOOL = Tool(
    name="read_notes",
    description=(
        "Read the latest saved notes for this chat, its participants, and shared memory from disk. "
        "Use when the turn note says notes were updated since your prompt was built and you need fresh details."
    ),
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_read_notes,
    scopes=set(ALL_SCOPES),
)


_OWNER_ONLY_SCOPE = frozenset({"owner_dm"})


def _is_owner_only(tool: Tool) -> bool:
    return tool.scopes == _OWNER_ONLY_SCOPE


def _shared_core_tools(search: SearchClient | SearchRouter | None = None) -> list[Tool]:
    tools = [
        SEARCH_TOOL,
        SKILLS_LIST_TOOL,
        SKILL_VIEW_TOOL,
        SKILL_MANAGE_TOOL,
        READ_NOTES_TOOL,
    ]
    if search is not None:
        tools.append(_web_search_tool(search))
    return tools


def core_tools(search: SearchClient | SearchRouter | None = None, desktop: DesktopFleet | None = None) -> list[Tool]:
    tools = _shared_core_tools(search)
    tools.append(REMIND_TOOL)
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
    data = brief.markdown.encode("utf-8")
    caption, _ = format_text(f"Core-change brief: {brief.title}", limit=1024, literal=True)[0]
    try:
        await retry_telegram(
            lambda: app.bot.send_document(
                ctx.chat_id, BufferedInputFile(data, filename=brief.filename),
                caption=caption,
            ),
            sleep=app.sleep,
            budget=FloodBudget(),
        )
    except (TelegramAPIError, FloodBudgetExhausted):
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


async def _switch_vibe(args: dict, ctx: TurnCtx) -> str:
    from dom.traits import SwitchRefused, switch_vibe

    app = ctx.app
    if app is None:
        return "error: switching your vibe is unavailable"
    try:
        chosen = await switch_vibe(
            app.home, app.store, ctx.chat_id, args.get("preset"), args.get("reason"), app.dispatch.now(),
        )
    except SwitchRefused as error:
        return f"error: {error}"
    reason = clean_reason(args.get("reason"))
    log.info("Dom switched his vibe to %s in chat %s: %s", chosen.name, ctx.chat_id, reason)
    await app.store.record_owner_event(f"Dom switched his vibe to {chosen.title}: {reason}")
    return json.dumps({"preset": chosen.name, "applies": "from your next reply"})


SWITCH_VIBE_TOOL = Tool(
    name="switch_vibe",
    description=(
        "Change your own vibe (preset) for the rest of today. Use it at most once a day, only when this conversation "
        "clearly calls for a different vibe than the one you woke up with, and give a short reason. "
        "It does not work in a chat that has its own preset."
    ),
    schema={
        "type": "object",
        "properties": {"preset": {"type": "string"}, "reason": {"type": "string", "maxLength": 160}},
        "required": ["preset", "reason"],
    },
    handler=_switch_vibe,
    scopes=set(ALL_SCOPES),
)


TASK_QUIET_MINUTES = int(TASK_QUIET.total_seconds() // 60)


async def _task_mode(args: dict, ctx: TurnCtx) -> str:
    from dom.traits import TaskRefused, end_task_mode, start_task_mode, task_mode

    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: task mode is available only in the owner's DM"
    now = app.dispatch.now()
    action = args.get("action")
    if action == "end":
        if not await end_task_mode(app.store, ctx.chat_id, now):
            return "error: task mode is not on; to start it for a task, call task_mode with action=start and a reason"
        await app.store.record_owner_event("Dom left assistant mode: the task is done")
        return json.dumps({"task_mode": "off", "applies": "from your next reply"})
    if action != "start":
        return "error: action must be start or end"
    was_on = await task_mode(app.store, ctx.chat_id, now) is not None
    try:
        task = await start_task_mode(app.home, app.store, ctx.chat_id, args.get("reason"), now)
    except TaskRefused as error:
        return f"error: {error}"
    if not was_on:
        await app.store.record_owner_event(f"Dom went into assistant mode for a task: {task.reason}")
    log.info("Dom is in task mode: %s", task.reason)
    return json.dumps({"task_mode": "on", "applies": "from your next reply", "ends_after_quiet_minutes": TASK_QUIET_MINUTES})


TASK_MODE_TOOL = Tool(
    name="task_mode",
    # "start"/"end" rather than a boolean: with active=true/false the homelab model sent active=false for a planning
    # request three times in four, which is the "end" call and was refused.
    description=(
        "Owner's DM only. Switch into work mode for an important task your friend has asked you to do (planning, "
        "research, admin, or anything that needs reminders, web search or desktop tasks at length): action=start with a "
        "short reason. From your next reply you use the assistant preset, so tell your friend in character that you are "
        "switching to work mode. Never for small talk, a greeting, or a one-word reply like \"sure\" or \"ok\". "
        f"action=end only once the task is finished and task mode is on. It also ends after {TASK_QUIET_MINUTES} quiet "
        "minutes. It does not use up switch_vibe and does nothing in a chat that has its own preset."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "end"]},
            "reason": {"type": "string", "maxLength": 160},
        },
        "required": ["action"],
    },
    handler=_task_mode,
    scopes={"owner_dm"},
)


def _turn_timing_summary(row: dict) -> dict:
    try:
        trace = json.loads(row.get("trace") or "[]")
    except ValueError:
        trace = []
    stages: dict[str, int] = {}
    for item in trace:
        if isinstance(item, str) and "@" in item:
            what, _, val = item.rpartition("@")
            if val.isdecimal():
                stages[what] = stages.get(what, 0) + int(val)
    try:
        raw_calls = json.loads(row.get("model_calls") or "[]")
    except ValueError:
        raw_calls = []
    calls = [
        {
            "kind": call.get("kind"),
            "model": call.get("model"),
            "fallback": bool(call.get("fallback")),
            "duration_ms": call.get("duration_ms"),
            "status": call.get("status"),
            "input_tokens": call.get("input_tokens"),
            "output_tokens": call.get("output_tokens"),
            "cached_tokens": call.get("cached_tokens"),
        }
        for call in raw_calls if isinstance(call, dict)
    ]
    return {
        "at": row["at"],
        "scope": row["scope"],
        "model": row["model"],
        "fallback": bool(row["fallback"]),
        "outcome": row["outcome"],
        "duration_ms": row["duration_ms"],
        "first_bubble_ms": row.get("first_bubble_ms"),
        "last_bubble_ms": row.get("last_bubble_ms"),
        "stages_ms": stages,
        "model_calls": calls,
    }


async def _self_inspect(args: dict, ctx: TurnCtx) -> str:
    from dom.capabilities import desktop_hosts, desktop_status
    from dom.studio.profile import switch_states
    from dom.plugins.manifest import SCOPES
    from dom.traits import task_mode, vibe

    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: self_inspect is available only in the owner's DM"
    enabled = {scope: [tool.name for tool in await enabled_tools(app, scope)] for scope in SCOPES}
    today = await vibe(app.home, app.store, app.dispatch.now())
    task = await task_mode(app.store, ctx.chat_id, app.dispatch.now())
    payload: dict[str, object] = {
        "version": VERSION,
        "repository": app.config.update.repo_url,
        "changelog_latest": own_code.latest_release(own_code.installed_base()),
        "tools": enabled,
        "approved_plugins": [
            {"name": plugin.manifest.name, "hash": plugin.hash} for plugin in await app.plugins.approved()
        ],
        "plugin_sandbox_failure": app.plugin_runner.last_failure,
        "desktop": desktop_status(app, "owner_dm"),
        "gifs": "on" if app.gifs is not None else "off (set KLIPY_API_KEY in .env to turn them on)",
        "studio": app.studio.status if app.studio and app.config.studio.enabled else "off (configure [studio])",
        # Each switch as it really stands, cloud backups included.
        "studio_features": await asyncio.to_thread(
            switch_states, app.config, bool(app.studio is not None and app.studio.ready),
        ),
        "desktop_hosts": desktop_hosts(app),
        "models": {kind: endpoint.model or endpoint.provider or "" for kind, endpoint in app.config.models.items()},
        "last_errors": await app.store.recent_turn_errors(),
        "preset": {
            "everywhere": await global_preset_name(app.store),
            "today": {
                "mode": today.mode,  # auto: you pick; locked: the owner chose
                "preset": today.preset.name if today.preset else None,
                "your_reason": today.reason or None,
                "how": today.source or None,
            },
            "task_mode": {"on": True, "reason": task.reason, "until": task.until.isoformat()} if task else {"on": False},
            "chats_with_own": [
                {"title": row["title"] or row["chat_id"], "preset": row["preset"]}
                for row in await app.store.list_chats("approved") if row["preset"]
            ],
        },
    }
    if args.get("include_timings") is True:
        raw_limit = args.get("limit", 10)
        limit = max(1, min(raw_limit if type(raw_limit) is int else 10, 20))
        payload["recent_timings"] = [_turn_timing_summary(row) for row in await app.store.recent_turns(limit=limit)]
    return json.dumps(payload)


SELF_INSPECT_TOOL = Tool(
    name="self_inspect",
    description=(
        "Read Dom's version, public repository URL, newest changelog entry, available tools by chat scope, approved plugins, desktop status, GIFs and outbound media status, "
        "models, the preset used everywhere with the chats that override it, and recent errors. "
        "Pass include_timings=true only when the owner asks why a reply was slow to inspect recent reply timings from the turn log. "
        "When someone asks about you, answer in your own words like a person would; "
        "give exact numbers or internal names only when asked for them."
    ),
    schema={
        "type": "object",
        "properties": {
            "include_timings": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        },
        "additionalProperties": False,
    },
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


async def _message_chat(args: dict, ctx: TurnCtx) -> str:
    if ctx.app is None or not ctx.is_owner_dm():
        return "Only the owner can publish messages to other chats."
    return await ctx.app.dispatch.cross_chat.message_chat(
        ctx.chat_id, args.get("chat"), args.get("text"), args.get("reason"),
    )


async def _tell_owner(args: dict, ctx: TurnCtx) -> str:
    if ctx.app is None or ctx.scope not in ("group", "friend_dm") or ctx.chat_id == ctx.owner_id:
        return "This is only for contacting the owner from another conversation."
    if ctx.sender_id is not None and ctx.sender_id == ctx.owner_id:
        return "The owner is the one talking to you here. Say it to him in this chat."
    return await ctx.app.dispatch.cross_chat.tell_owner(ctx.chat_id, args.get("text"), args.get("reason"))


_DELIVERY_FIELDS = {
    "text": {"type": "string", "maxLength": MAX_TEXT},
    "reason": {"type": "string", "maxLength": MAX_REASON},
}
MESSAGE_CHAT = Tool(
    name="message_chat",
    description=(
        "Draft one ordinary text message to an approved group or friend's DM for the owner to approve. "
        "Write as you would text that chat yourself. Only say the owner asked if he wants that said."
    ),
    schema={
        "type": "object",
        "properties": {"chat": {"type": "string", "description": "The chat title or numeric ID"}, **_DELIVERY_FIELDS},
        "required": ["chat", "text", "reason"],
    },
    scopes={"owner_dm"},
    handler=_message_chat,
)
TELL_OWNER = Tool(
    name="tell_owner",
    description=(
        "Use this when something in this conversation needs the owner's attention. "
        "Write as yourself texting him. Don't repeat what an alert already said."
    ),
    schema={"type": "object", "properties": _DELIVERY_FIELDS, "required": ["text", "reason"]},
    scopes={"group", "friend_dm"},
    handler=_tell_owner,
)


async def enabled_tools(app: App, scope: Scope) -> list[Tool]:
    """Offered tools in cache-stable order: shared tools first, owner-only tools last.

    Scopes stay separate (groups and friends' DMs never see owner-only tool names). The vibe switch
    and shared plugins sit at the end of the shared block, and desktop, T3 and owner-only plugins at
    the end of the owner block, so a change there reshapes only what follows it."""
    from dom.devices import FIND_DEVICES_TOOL
    from dom.memory_edit import PROPOSE_MEMORY_EDIT_TOOL
    from dom.plugins.tools import DRAFT_PLUGIN_TOOL, offered

    shared: list[Tool] = [tool for tool in _shared_core_tools(app.search_client) if scope in tool.scopes]
    if await global_preset_name(app.store) == AUTO and scope in SWITCH_VIBE_TOOL.scopes:
        shared.append(SWITCH_VIBE_TOOL)
    plugin_tools = sorted(await offered(app, scope), key=lambda tool: tool.name)
    shared.extend(tool for tool in plugin_tools if not _is_owner_only(tool))
    if scope != "owner_dm":
        return [*shared, TELL_OWNER]

    owner_only: list[Tool] = [
        REMIND_TOOL,
        GOALS_TOOL,
        DRAFT_PLUGIN_TOOL,
        WRITE_BRIEF_TOOL,
        SELF_INSPECT_TOOL,
        OWN_CODE_TOOL,
        FIND_DEVICES_TOOL,
        TASK_MODE_TOOL,
    ]
    if app.config.memory_self_edit:  # a setting, so it changes rarely; sits before the tools that come and go
        owner_only.append(PROPOSE_MEMORY_EDIT_TOOL)
    if app.desktop_worker is not None and app.desktop_worker.available():
        owner_only.append(START_DESKTOP_TASK)
    if app.t3 is not None and app.t3.available():
        from dom.t3.tools import TOOLS

        owner_only.extend(TOOLS)
    owner_only.extend(tool for tool in plugin_tools if _is_owner_only(tool))
    return [*shared, *owner_only, MESSAGE_CHAT]

"""What only the owner sees: the "/" command menu, the /status overview with its wake/sleep/leave buttons and /chats."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from dom import memory_export, memory_import, proposals, release, skills, traits
from dom._version import VERSION
from dom.deletion import (
    STALE_MESSAGE,
    hash_matches,
    prune_deletions,
    stage_deletion,
    stamp_matches,
    stamp_token,
)
from dom.desktop import files as desktop_files
from dom.desktop.worker import ACTIVE as DESKTOP_ACTIVE
from dom.plugins import surfaces
from dom.plugins.manifest import NETWORK_NOTICE, validate_name as validate_plugin_name
from dom.telegram.executor import retry_telegram, send_text
from dom.traits import AUTO, GLOBAL_PRESET_KEY, ensure_pick, load_preset, vibe

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.owner")

OWNER_COMMANDS = [
    BotCommand(command="status", description="Where Dom is and what he's doing"),
    BotCommand(command="wake", description="Wake Dom up"),
    BotCommand(command="sleep", description="Send Dom to sleep"),
    BotCommand(command="pending", description="Chats waiting for your approval"),
    BotCommand(command="chats", description="Every group and DM Dom knows about"),
    BotCommand(command="studio", description="Check media generation status"),
    BotCommand(command="preset", description="Change Dom's persona in a chat"),
    BotCommand(command="proposals", description="Review pending or delete settled proposals"),
    BotCommand(command="plugins", description="List, disable and delete plugins"),
    BotCommand(command="skills", description="List, retire, restore and delete skills"),
    BotCommand(command="reminders", description="List, cancel and delete reminders"),
    BotCommand(command="t3", description="View T3 sign-in, machines and audit history"),
    BotCommand(command="tasks", description="List, cancel and delete desktop tasks"),
    BotCommand(command="ownwork", description="List, cancel and delete self-directed tasks"),
    BotCommand(command="stickers", description="Add or remove a sticker set"),
    BotCommand(command="import", description="Bring memories from another assistant"),
    BotCommand(command="export", description="Get a zip of everything Dom remembers"),
    BotCommand(command="update", description="Check for and install a new release"),
]

PROPOSALS_PER_PAGE = 10
BULK_KINDS = ("memory_fact", "skill", "lan_address")  # what "Approve all" applies; the rest keep their own button
MAX_MESSAGE = 4096  # Telegram's limit for a message's text
MAX_BUTTONS = 100  # Telegram's limit for one keyboard
MAX_MEMBER_LOOKUPS = 30  # get_chat_member_count calls per /chats, 0.2 s apart
_STATUS_ORDER = (("approved", "Approved"), ("pending_approval", "Pending"), ("ignored", "Ignored"), ("left", "Left"))
_CHATS_HEAD = "| Chat | Type | Members |\n|---|---|---|"
_ICONS = {"asleep": "🌙", "online": "☀️", "offline": "💤"}


@dataclass(frozen=True)
class View:
    """One owner message: rich-message markdown, the plain text sent instead if rich messages are refused, and its keyboard."""

    markdown: str
    plain: str
    markup: InlineKeyboardMarkup


def _cell(text: str) -> str:
    """A title or value safe inside a markdown table cell: nothing in it may be read as table or emphasis syntax."""
    for char in "\\|*_`":
        text = text.replace(char, "\\" + char)
    return text


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [_row(header), "|" + "---|" * len(header), *map(_row, rows)]


async def send_view(bot, chat_id: int, view: View, *, sleep=None) -> None:
    """Send `view` as a rich message; if Telegram refuses rich messages, send its plain text with the same keyboard."""
    kw = {"sleep": sleep} if sleep is not None else {}
    try:
        await retry_telegram(
            lambda: bot.send_rich_message(
                chat_id=chat_id, rich_message=InputRichMessage(markdown=view.markdown), reply_markup=view.markup,
            ),
            **kw,
        )
    except TelegramBadRequest as error:
        log.warning("rich message refused, sending plain text instead: %s", error)
        await send_text(bot, chat_id, view.plain, reply_markup=view.markup, literal=True, **kw)


async def delete_message(bot, chat_id: int, message_id: int, *, sleep=None) -> None:
    """Remove an old owner message. Rich messages cannot be edited in place, so a change means delete and send anew."""
    kw = {"sleep": sleep} if sleep is not None else {}
    try:
        await retry_telegram(lambda: bot.delete_message(chat_id, message_id), **kw)
    except TelegramBadRequest:  # already deleted by the owner
        log.info("the message was already gone")


def _is_group(chat: dict) -> bool:
    return str(chat["chat_id"]).startswith("-")


def _short(title: str, limit: int = 20) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"


async def publish_menu(bot, owner_id: int, extra: list[BotCommand] | None = None) -> bool:
    """Show the owner the command menu (core, then approved plugin commands) and everyone else none.

    A failure is logged, never raised. Returns True when set_my_commands succeeded.
    """
    commands = OWNER_COMMANDS + (extra or [])[:surfaces.MAX_MENU - len(OWNER_COMMANDS)]
    ok = True
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=owner_id))
    except Exception:
        ok = False
        log.exception("could not set the owner's command menu")
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception:
        log.exception("could not clear the default command menu")
    return ok


async def refresh_menu(app: App) -> bool:
    if app.owner_id is None:
        app.menu_dirty = False
        return True
    try:
        extra = await surfaces.menu_commands(app)
        ok = await publish_menu(app.bot, app.owner_id, extra)
    except Exception:
        log.exception("could not refresh the owner's command menu")
        ok = False
    app.menu_dirty = not ok
    return ok


def display_title(chat: dict, owner_id: int | None) -> str:
    chat_id = str(chat["chat_id"])
    if chat_id == str(owner_id):
        return "your DM"
    name = " ".join((chat["title"] or "").split())
    if chat_id.startswith("-"):
        return (name or chat_id)[:60]
    return f"DM with {(name or ' '.join((chat.get('peer_name') or '').split()) or chat_id)[:50]}"


def _one_line(parts: list[str], limit: int = 400) -> str:
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def record_event(app: App, text: str) -> None:
    """Note an owner action without interrupting its response if the event cannot be stored."""
    try:
        await app.store.record_owner_event(text)
    except Exception:
        log.exception("could not record an owner event")


@dataclass(frozen=True)
class PresenceChange:
    message: str
    spend_paused: bool = False


async def override_chat(app: App, chat_id: int, command: str, who: str) -> PresenceChange | None:
    resolved = int(await app.store.resolve_chat_id(chat_id))
    chat = next((c for c in await app.store.list_approved_chats() if c["chat_id"] == str(resolved)), None)
    if chat is None or not await app.dispatch.override(resolved, command):
        return None
    title = display_title(chat, app.owner_id)
    if command == "/wake":
        await record_event(app, f"{who} woke Dom in {title}")
        if await app.dispatch.spend_paused(resolved):
            return PresenceChange(f"Dom is up in {title}, but friends' chats are paused by today's spend cap", True)
        return PresenceChange(f"Dom is up in {title}")
    await record_event(app, f"{who} sent Dom to sleep in {title}")
    return PresenceChange(f"Dom is asleep in {title}")


LEAVABLE = ("approved", "pending_approval", "ignored")


async def leavable_chat(app: App, chat_id: int) -> dict | None:
    resolved = int(await app.store.resolve_chat_id(chat_id))
    if resolved >= 0:
        return None
    return next((
        c for c in await app.store.list_chats()
        if c["chat_id"] == str(resolved) and c["authorization_status"] in LEAVABLE
    ), None)


async def leave_chat(app: App, chat_id: int, who: str) -> tuple[str, bool]:
    resolved = int(await app.store.resolve_chat_id(chat_id))
    async with app.dispatch._dispatch_slot(resolved, wait_media=False):
        chat = await leavable_chat(app, resolved)
        if chat is None:
            return "unknown chat", False
        title = display_title(chat, app.owner_id)
        try:
            await retry_telegram(lambda: app.bot.leave_chat(resolved), sleep=app.sleep)
            result = f"Dom left {title}"
        except (TelegramBadRequest, TelegramForbiddenError):
            result = f"Dom wasn't in {title} anymore"
        except Exception as error:
            log.exception("could not leave chat %s", resolved)
            await record_event(app, f"{who} tapped Leave on {title}: Dom couldn't leave")
            message = (getattr(error, "message", "") or str(error) or type(error).__name__)[:100]
            return f"Couldn't leave: {message}", False
        await app.store.set_chat_auth_status(resolved, "left")
        await record_event(app, f"{who} tapped Leave on {title}: {result}")
        return result, True


async def approved_chat(app: App, chat_id: int | str) -> dict | None:
    resolved = await app.store.resolve_chat_id(chat_id)
    return next((c for c in await app.store.list_chats("approved") if c["chat_id"] == resolved), None)


async def set_preset(app: App, chat: dict, name: str) -> str | None:
    preset = load_preset(app.home, name) if name else None
    if (name and preset is None) or not await app.store.set_chat_preset(chat["chat_id"], name):
        return None
    title = display_title(chat, app.owner_id)
    label = preset.title if preset else "None"
    message = f"Preset for {title}: {label}"
    await record_event(app, message)
    return message


async def set_traits(app: App, chat: dict, raw: dict[str, object]) -> str | None:
    """Save per-chat slider overrides the same way set_preset saves a preset: validated, then logged as an event."""
    cleaned = traits.validate_overrides(raw)
    if cleaned is None:
        return None
    _, existing = await app.store.get_chat_personality(chat["chat_id"])
    if not await app.store.set_chat_traits(chat["chat_id"], traits.normalise(traits.decode(existing)) | cleaned):
        return None
    summary = ", ".join(f"{name} {round(value * 100)}%" for name, value in cleaned.items())
    message = f"Traits for {display_title(chat, app.owner_id)}: {summary}"
    await record_event(app, message)
    return message


async def reset_traits(app: App, chat: dict) -> str | None:
    if not await app.store.set_chat_traits(chat["chat_id"], {}):
        return None
    message = f"Traits for {display_title(chat, app.owner_id)}: reset to default"
    await record_event(app, message)
    return message


PENDING_DECISIONS = {"approve": "approved", "ignore": "ignored"}


async def decide_pending_chat(app: App, chat_id: int, decision: str, who: str) -> tuple[dict | None, str]:
    """Approve or ignore a pending group or stranger DM and record an owner event."""
    status = PENDING_DECISIONS.get(decision)
    if status is None or app.owner_id is None:
        raise ValueError("Choose approve or ignore.")
    resolved = await app.store.resolve_chat_id(chat_id)
    chat = await app.store.resolve_pending_chat(resolved, status)
    if chat is None:
        return None, "already handled"
    label = "Approved" if decision == "approve" else "Ignored"
    await record_event(app, f"{who} {label.lower()} {display_title(chat, app.owner_id)}")
    return chat, label


async def set_global_preset(app: App, name: str, who: str) -> str | None:
    """Lock one preset everywhere, clear it, or hand the choice to Dom with `auto` (he needs a preset to pick from)."""
    preset = load_preset(app.home, name) if name and name != AUTO else None
    if name and name != AUTO and preset is None:
        return None
    if name == AUTO and not await ensure_pick(app.home, app.store, app.dispatch.now()):
        return None
    await app.store.kv_set(GLOBAL_PRESET_KEY, name)
    label = "Auto (Dom picks)" if name == AUTO else preset.title if preset else "None"
    message = f"{who} set Dom's preset everywhere to {label}"
    await record_event(app, message)
    return message


async def chats_summary(app: App) -> str:
    """What /chats listed, in one line: the groups with their titles, then each DM, the owner's last."""
    known = [c for c in await app.store.list_chats() if c["authorization_status"] in ("approved", "pending_approval")]
    groups = [display_title(c, app.owner_id) for c in known if _is_group(c)]
    dms = [c for c in known if not _is_group(c)]
    dms.sort(key=lambda c: c["chat_id"] == str(app.owner_id))
    parts = [f"{len(groups)} group{'s' if len(groups) != 1 else ''} ({', '.join(groups)})"] if groups else []
    return _one_line(parts + [display_title(c, app.owner_id) for c in dms]) or "no chats"


async def status_summary(app: App) -> str:
    """What /status showed, in one line: each approved chat with its state."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    return _one_line([f"{display_title(c, app.owner_id)} {state}" for c, (state, _) in zip(chats, states)])


async def pending_summary(app: App) -> str:
    pending = await app.store.list_pending_chats()
    if not pending:
        return "nothing waiting"
    return f"{len(pending)} waiting ({_one_line([display_title(c, app.owner_id) for c in pending])})"


def _container_tag(task: dict) -> str:
    """Name a task's container unless it is the default one."""
    return "" if task.get("container", "default") == "default" else f" on {task['container']}"


async def _send(app: App, text: str, **kw) -> None:
    await send_text(app.bot, app.owner_id, text, sleep=app.sleep, **kw)


class _StaleSkill(Exception):
    pass


async def show_tasks(app: App) -> None:
    if app.owner_id is None:
        return
    rows = await app.store.desktop_tasks(app.owner_id)
    lines = [
        f"#{row['id']} · {row['status'].replace('_', ' ')}{_container_tag(row)} · {_short(' '.join(row['brief'].split()), 120)}"
        for row in rows
    ]
    buttons = [
        [InlineKeyboardButton(text=f"Cancel #{row['id']}", callback_data=f"desktop:cancel:{row['id']}")]
        if row["status"] in DESKTOP_ACTIVE else
        [InlineKeyboardButton(
            text=f"Delete #{row['id']}", callback_data=f"desktop:del:{row['id']}:{stamp_token(row['created_at'])}",
        )]
        for row in rows
    ]
    await _send(
        app, "\n".join(lines) or "No desktop tasks yet.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


async def show_own_work(app: App) -> None:
    if app.owner_id is None:
        return
    rows = await app.store.self_tasks(limit=20)
    if not rows:
        await _send(app, "No self-directed tasks yet.")
        return
    for start in range(0, len(rows), 10):
        lines, buttons = [], []
        for row in rows[start:start + 10]:
            goal = _short(" ".join(row["goal"].split()), 120)
            lines.append(f"#{row['id']} · {row['status']} · {goal}")
            if row["status"] in ("planned", "running"):
                buttons.append([InlineKeyboardButton(text=f"Cancel #{row['id']}", callback_data=f"own:cancel:{row['id']}")])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"Delete #{row['id']}",
                    callback_data=f"own:del:{row['id']}:{stamp_token(row['announced_at'])}",
                )])
        await _send(
            app, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
        )


async def delete_desktop_task(app: App, task_id: int, expected_created_at: str) -> tuple[bool, str]:
    """Archive and delete one finished desktop task and any bound inbox files."""
    if app.owner_id is None:
        return False, "There is no owner."
    if not expected_created_at:
        return False, STALE_MESSAGE
    row = await app.store.get_desktop_task(app.owner_id, task_id)
    if row is None:
        return False, f"Desktop task #{task_id} not found."
    if row["status"] in DESKTOP_ACTIVE:
        return False, f"Cannot delete desktop task #{task_id} while it is {row['status'].replace('_', ' ')}."
    if not stamp_matches(row["created_at"], expected_created_at):
        return False, STALE_MESSAGE
    try:
        inbox_dir = desktop_files._inbox(app.home.root) / f"task-{task_id}"
        if inbox_dir.is_symlink():
            raise ValueError("Task inbox path cannot be a symlink.")
    except ValueError as error:
        return False, str(error)
    event_at = datetime.now(timezone.utc).isoformat()
    event_text = f"Deleted desktop task #{task_id}"
    archive = None
    try:
        archive = await asyncio.to_thread(
            stage_deletion,
            app.home.root,
            "desktop_task",
            task_id,
            event_text=event_text,
            event_at=event_at,
            rows={"desktop_task": row},
            sources=[inbox_dir] if inbox_dir.exists() else [],
        )
        await asyncio.to_thread(archive.move_in)
    except (OSError, ValueError) as error:
        if archive is not None:
            await asyncio.to_thread(archive.rollback)
        log.warning("could not archive desktop task %s: %s", task_id, type(error).__name__)
        return False, "Could not archive desktop task before deleting."
    try:
        result = await app.store.delete_desktop_task(
            app.owner_id, task_id, expected_created_at, event_text, event_at,
        )
    except sqlite3.Error as error:
        log.warning("could not delete desktop task %s: %s", task_id, type(error).__name__)
        await asyncio.to_thread(archive.rollback)
        return False, "Could not delete desktop task."
    if result != "deleted":
        await asyncio.to_thread(archive.rollback)
        if result == "stale":
            return False, STALE_MESSAGE
        if result == "active":
            return False, f"Cannot delete desktop task #{task_id} while it is active."
        return False, f"Desktop task #{task_id} not found."
    await asyncio.to_thread(archive.commit)
    await asyncio.to_thread(prune_deletions, app.home.root)
    return True, f"Deleted desktop task #{task_id}."


async def skill_callback(app: App, action: str, name: str, digest: str = "") -> str:
    """Build a Telegram callback_data <= 64 bytes for a skill action, using a KV token for long names."""
    direct = f"skill:{action}:{name}:{digest[:12]}" if digest else f"skill:{action}:{name}"
    if len(direct.encode("utf-8")) <= 64:
        return direct
    token = secrets.token_hex(6)
    await app.store.kv_set(f"skill-cb:{token}", json.dumps({"name": name, "hash": digest}))
    return f"skill:{action}:~{token}"


async def resolve_skill_callback(app: App, payload: str) -> tuple[str, str] | None:
    """Resolve `name[:hash]` or `~token` from a skill callback into `(name, expected_hash)`."""
    if payload.startswith("~"):
        raw = await app.store.kv_get(f"skill-cb:{payload[1:]}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            name, digest = data.get("name"), data.get("hash", "")
            return (name, digest) if isinstance(name, str) and isinstance(digest, str) else None
        except (ValueError, AttributeError):
            return None
    name, _, digest = payload.partition(":")
    return (name, digest) if name else None


async def show_skills(app: App) -> None:
    if app.owner_id is None:
        return
    rows = await asyncio.to_thread(skills.skills_inventory, app.home)
    if not rows:
        await _send(app, "No skills yet.")
        return
    for start in range(0, len(rows), 10):
        lines, buttons = [], []
        for row in rows[start:start + 10]:
            name = str(row["name"])
            digest = str(row["hash"])
            retired = bool(row["retired"])
            status = "retired" if retired else "active"
            desc = _short(str(row["description"]), 120)
            lines.append(f"{name} · {status} · {desc}")
            toggle = (
                InlineKeyboardButton(text=f"Restore {name}", callback_data=await skill_callback(app, "restore", name))
                if retired else
                InlineKeyboardButton(text=f"Retire {name}", callback_data=await skill_callback(app, "retire", name))
            )
            delete_btn = InlineKeyboardButton(
                text=f"Delete {name}", callback_data=await skill_callback(app, "del", name, digest),
            )
            buttons.append([toggle, delete_btn])
        await _send(
            app, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


async def retire_skill(app: App, name: str) -> tuple[bool, str]:
    """Retire an approved skill: keep files on disk, stop Dom from listing or using it."""
    try:
        changed = await asyncio.to_thread(skills.retire_skill, app.home, name)
    except FileNotFoundError:
        return False, f"Skill {name} not found."
    except ValueError as error:
        return False, str(error)
    if not changed:
        return False, f"Skill {name} is already retired."
    await record_event(app, f"Retired skill {name}")
    return True, f"Retired skill {name}."


async def restore_skill(app: App, name: str) -> tuple[bool, str]:
    """Restore a retired skill so Dom can list and use it again."""
    try:
        changed = await asyncio.to_thread(skills.restore_skill, app.home, name)
    except FileNotFoundError:
        return False, f"Skill {name} not found."
    except ValueError as error:
        return False, str(error)
    if not changed:
        return False, f"Skill {name} is already active."
    await record_event(app, f"Restored skill {name}")
    return True, f"Restored skill {name}."


async def delete_skill(app: App, name: str, expected_hash: str) -> tuple[bool, str]:
    """Archive and remove a skill's files under SKILL_LOCK, verifying its content hash."""
    skills._validate_name(name)
    if not expected_hash:
        return False, STALE_MESSAGE
    public = app.home.scoped(allow_private=False)

    def inspect() -> tuple[object, str]:
        with skills.SKILL_LOCK:
            folder = public._regular_path(f"skills/{name}")
            if not folder.exists() or not folder.is_dir():
                raise FileNotFoundError(f"Skill {name} not found.")
            live = skills.skill_hash(public, name)
            if not hash_matches(live, expected_hash):
                raise _StaleSkill
            return folder, live

    try:
        folder, live_hash = await asyncio.to_thread(inspect)
    except FileNotFoundError:
        return False, f"Skill {name} not found."
    except _StaleSkill:
        return False, STALE_MESSAGE
    except ValueError as error:
        return False, str(error)

    event_at = datetime.now(timezone.utc).isoformat()
    event_text = f"Deleted skill {name}"
    try:
        archive = await asyncio.to_thread(
            stage_deletion,
            app.home.root,
            "skill",
            name,
            event_text=event_text,
            event_at=event_at,
            rows={"skill": {"name": name, "hash": live_hash}},
            sources=[folder],
        )
    except (OSError, ValueError) as error:
        log.warning("could not archive skill %s: %s", name, type(error).__name__)
        return False, "Could not archive skill before deleting."

    def prepare() -> None:
        # Runs inside store._tx() (lock order: store._lock -> SKILL_LOCK, matching decide_proposal).
        with skills.SKILL_LOCK:
            current_folder = public._regular_path(f"skills/{name}")
            if not current_folder.exists() or not current_folder.is_dir():
                raise FileNotFoundError(f"Skill {name} not found.")
            live = skills.skill_hash(public, name)
            if not hash_matches(live, expected_hash):
                raise _StaleSkill
            archive.move_in()

    def rollback_files() -> None:
        with skills.SKILL_LOCK:
            archive.rollback()

    try:
        await app.store.commit_skill_delete(event_text, event_at, prepare, rollback_files)
    except _StaleSkill:
        await asyncio.to_thread(rollback_files)
        return False, STALE_MESSAGE
    except FileNotFoundError:
        await asyncio.to_thread(rollback_files)
        return False, f"Skill {name} not found."
    except (OSError, ValueError, sqlite3.Error) as error:
        log.warning("could not delete skill %s: %s", name, type(error).__name__)
        await asyncio.to_thread(rollback_files)
        return False, "Could not delete skill."
    await asyncio.to_thread(archive.commit)
    await asyncio.to_thread(prune_deletions, app.home.root)
    return True, f"Deleted skill {name}."


async def send_delete_confirm(app: App, prompt: str, yes_data: str, no_data: str) -> None:
    """Send a two-button delete confirmation prompt in the owner's DM."""
    if app.owner_id is None:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, delete", callback_data=yes_data),
        InlineKeyboardButton(text="Cancel", callback_data=no_data),
    ]])
    await _send(app, prompt, reply_markup=markup)


async def render_status(app: App) -> View:
    """One table row and one Wake/Sleep(/Leave, for groups) button row per approved chat, the owner's DM first,
    then by recent activity."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    tz = ZoneInfo(app.config.timezone)
    pending = len(await app.store.list_pending_chats())
    models = app.config.models
    footer = "Pending approval: {} · Chat model: {} · Triage model: {}".format(
        pending, *(models[name].model if name in models else "n/a" for name in ("chat", "triage"))
    )

    count, spent = await app.dispatch.usage.today(tz)
    model = getattr(app.client, "last_model", None)
    fallback_at = getattr(app.client, "last_fallback_at", None)
    fallback = ""
    if fallback_at is not None and fallback_at.astimezone(tz).date() == app.dispatch.now().astimezone(tz).date():
        fallback = f" (fallback at {fallback_at.astimezone(tz):%H:%M})"
    footer += f"\nToday: {count} calls · ${spent:.2f}\nLast reply: {model or '—'}{fallback}"
    current = await app.store.desktop_tasks(app.owner_id, active=True) if app.owner_id is not None else []
    for task in current:
        footer += f"\nDesktop task #{task['id']}{_container_tag(task)} · {task['status'].replace('_', ' ')} · {task['steps']} steps"


    table, plain, buttons, used, button_count = [], [], [], len(footer) * 2 + 160, 0  # room for the footer, heading, table head and "more" line
    for chat, (state, until) in zip(chats, states):
        unread, _ = await app.store.get_unread(chat["chat_id"])
        title = display_title(chat, app.owner_id)
        when = f" until {until.astimezone(tz):%H:%M}" if until else ""
        line = f"{_ICONS[state]} {title}: {state}{when} · {unread} unread"
        cells = [_cell(title), f"{_ICONS[state]} {state}", f"{until.astimezone(tz):%H:%M}" if until else "—", str(unread)]
        row = [
            InlineKeyboardButton(text=f"☀️ Wake {_short(title)}", callback_data=f"wake:{chat['chat_id']}"),
            InlineKeyboardButton(text="🌙 Sleep", callback_data=f"sleep:{chat['chat_id']}"),
        ]
        if _is_group(chat):
            row.append(InlineKeyboardButton(text="🚪 Leave", callback_data=f"leave:{chat['chat_id']}"))
        cost = max(len(line), len(_row(cells))) + 1
        if used + cost > MAX_MESSAGE or button_count + len(row) > MAX_BUTTONS:
            break
        used += cost
        button_count += len(row)
        table.append(cells)
        plain.append(line)
        buttons.append(row)
    more = [f"…and {len(chats) - len(table)} more"] if len(chats) > len(table) else []
    markdown = "\n".join(["**Dom's chats**", "", *_table(["Chat", "State", "Until", "Unread"], table), "", *more, *([""] if more else []), _cell(footer)])
    return View(markdown, "\n".join(["Dom's chats", "", *plain, *more, "", footer]), InlineKeyboardMarkup(inline_keyboard=buttons))


async def render_chats(app: App) -> View:
    """Every chat Dom knows, one table per status. Groups show their member count (the first MAX_MEMBER_LOOKUPS
    of them, "?" when Telegram refuses); approved and pending groups get a Leave button."""
    known = await app.store.list_chats()
    by_status = [(label, [c for c in known if c["authorization_status"] == status]) for status, label in _STATUS_ORDER]
    total = sum(len(chats) for _, chats in by_status)

    md, plain, buttons, used, lookups, shown = [], [], [], 160, 0, 0  # room for the heading and the "more" line
    full = False
    for label, chats in by_status:
        cells_list, lines = [], []
        for chat in chats:
            if full:
                break
            title = display_title(chat, app.owner_id)
            members = None
            if _is_group(chat) and label != "Left" and lookups < MAX_MEMBER_LOOKUPS:
                if lookups:
                    await app.sleep(0.2)
                lookups += 1
                try:
                    members = await app.bot.get_chat_member_count(int(chat["chat_id"]))
                except Exception:
                    log.warning("could not count the members of chat %s", chat["chat_id"])
                    members = "?"
            count = "" if members is None else f" · {members} members" if members != 1 else " · 1 member"
            cells = [_cell(title), "Group" if _is_group(chat) else "DM", "—" if members is None else str(members)]
            line = f"• {title}{count}"
            cost = max(len(line), len(_row(cells))) + 1 + (0 if cells_list else len(label) + len(_CHATS_HEAD) + 8)
            leave = label in ("Approved", "Pending") and _is_group(chat)
            if used + cost > MAX_MESSAGE or len(buttons) + leave > MAX_BUTTONS:
                full = True
                break
            used += cost
            shown += 1
            cells_list.append(cells)
            lines.append(line)
            if leave:
                buttons.append([InlineKeyboardButton(text=f"🚪 Leave {_short(title)}", callback_data=f"leave:{chat['chat_id']}")])
        if cells_list:
            md += ["", f"**{label}**", "", *_table(["Chat", "Type", "Members"], cells_list)]
            plain += ["", label, *lines]
    if total > shown:
        md += ["", f"…and {total - shown} more"]
        plain += ["", f"…and {total - shown} more"]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if not shown:
        return View("Dom doesn't know any chats yet.", "Dom doesn't know any chats yet.", markup)
    return View("\n".join(["**Dom's chats**", *md]), "\n".join(["Dom's chats", *plain]), markup)


async def _preset_buttons(app: App, text: str, choices: list[tuple[str, str]]) -> None:
    for start in range(0, len(choices), MAX_BUTTONS):
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)]
            for label, data in choices[start:start + MAX_BUTTONS]
        ])
        await _send(app, text, reply_markup=markup)


async def show_presets(app: App) -> None:
    shared = await vibe(app.home, app.store, app.dispatch.now())
    label = _short(shared.preset.title, 30) if shared.preset else "None"
    choices = [(f"Everywhere · {'Auto: ' if shared.mode == 'auto' else ''}{label}", "preset:all")]
    for chat in await app.store.list_chats("approved"):
        preset = load_preset(app.home, chat["preset"]) if chat["preset"] else None
        title = display_title(chat, app.owner_id)
        label = _short(preset.title, 30) if preset else "uses Everywhere"
        choices.append((f"{_short(title, 30)} · {label}", f"preset:{chat['chat_id']}"))
    await _preset_buttons(app, "Choose where to set a preset.", choices)


async def choose_preset(app: App, chat: dict | None) -> None:
    """Preset choices for one chat, or for every chat when `chat` is None."""
    prefix = f"presetset:{chat['chat_id'] if chat else 'all'}:"
    choices = []
    for name in app.home.preset_names():
        data = prefix + name
        if len(data.encode("utf-8")) <= 64 and (preset := load_preset(app.home, name)) is not None:
            choices.append((_short(preset.title, 64), data))
    if chat is None:
        choices.append(("Auto (Dom picks daily)", prefix + AUTO))
    choices.append(("None", prefix))
    where = display_title(chat, app.owner_id) if chat else "every chat"
    await _preset_buttons(app, f"Choose a preset for {where}.", choices)


async def show_plugins(app: App) -> None:
    rows = await app.plugins.listing()
    if not rows:
        await _send(app, "No plugins are installed.")
        return
    for start in range(0, len(rows), 20):
        lines, buttons = [], []
        for row in rows[start:start + 20]:
            name = row["name"]
            status = row["status"] + ("" if row["available"] else " · files unavailable")
            lines.append(f"{name} · {status}" + (f" · {NETWORK_NOTICE}" if row["network"] else ""))
            row_buttons = []
            if row["status"] != "disabled":
                row_buttons.append(InlineKeyboardButton(text=f"Disable {name}", callback_data=f"plugin:disable:{name}"))
            row_buttons.append(InlineKeyboardButton(
                text=f"Delete {name}", callback_data=f"plugin:del:{name}:{row['hash'][:16]}",
            ))
            buttons.append(row_buttons)
        await _send(
            app, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
        )


async def disable_plugin(app: App, name: str) -> str:
    row = next((row for row in await app.plugins.listing() if row["name"] == name), None)
    return await app.plugins.decide(name, row["hash"], "disable") if row else "Unknown plugin"


async def delete_plugin(app: App, name: str, expected_hash: str) -> str:
    try:
        validate_plugin_name(name)
    except ValueError:
        return "Unknown plugin"
    result = await app.plugins.delete(name, expected_hash)
    if result == "Deleted":
        await app.proposals.settle_cards()
    return result


async def show_proposals(app: App, page: int = 1) -> None:
    rows = await app.proposals.pending()
    if not rows:
        await _send(app, "No pending proposals.")
        return
    pages = (len(rows) + PROPOSALS_PER_PAGE - 1) // PROPOSALS_PER_PAGE
    page = min(max(1, page), pages)
    start = (page - 1) * PROPOSALS_PER_PAGE
    shown = rows[start:start + PROPOSALS_PER_PAGE]
    lines = [f"Pending proposals ({page}/{pages})"]
    keyboard = []
    for row in shown:
        text = " ".join(proposals.summary(row).split())
        preview = text.encode("utf-16-le")[:720].decode("utf-16-le", errors="ignore")
        controls = proposals.buttons(row["id"], numbered=len(shown) > 1)
        if preview != text or row["kind"] == "cross_chat":
            if preview != text:
                preview += "…"
            if row["kind"] in ("memory_edit", "cross_chat"):  # review the exact draft or full diff
                controls = [button for button in controls if not button.callback_data.startswith("prop:yes:")]
            controls.append(InlineKeyboardButton(text=f"Details #{row['id']}", callback_data=f"prop:show:{row['id']}"))
        if row["kind"] == "plugin" and (review := proposals.review_button(app, json.loads(row["body_json"]))):
            controls.append(review)
        lines.append(preview)
        keyboard.append(controls)
    # IDs are ascending and never reused. The range refers to this page even if other proposals are decided.
    keyboard.append([InlineKeyboardButton(
        text="Approve all on this page", callback_data=f"prop:all:{shown[0]['id']}:{shown[-1]['id']}",
    )])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton(text="Previous", callback_data=f"prop:page:{page - 1}"))
    if page < pages:
        navigation.append(InlineKeyboardButton(text="Next", callback_data=f"prop:page:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    await _send(
        app, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        disable_notification=True,
    )


async def approve_proposal_page(app: App, first: int, last: int) -> str:
    """Approve the memory facts, skills and LAN address changes on one page, each through its own decision path and
    validation. A soul edit changes who Dom is and a plugin runs code, so each keeps its own Approve button."""
    rows = [row for row in await app.proposals.pending() if first <= row["id"] <= last]
    if not rows or len(rows) > PROPOSALS_PER_PAGE:
        return "already handled"
    bulk = [row for row in rows if row["kind"] in BULK_KINDS]
    souls = sum(row["kind"] == "soul_edit" for row in rows)
    held = (" 1 soul edit needs its own Approve." if souls == 1 else f" {souls} soul edits need their own Approve.") if souls else ""
    edits = sum(row["kind"] == "memory_edit" for row in rows)
    if edits:
        held += " 1 memory edit needs its own Approve." if edits == 1 else f" {edits} memory edits need their own Approve."
    if any(row["kind"] == "cross_chat" for row in rows):
        held += " Messages to other chats need their own approval."
    if any(row["kind"] == "plugin" for row in rows):
        held += " Plugins need their own review and approval."
    results = [await app.proposals.decide(row["id"], True) for row in bulk]
    approved = results.count("Approved")
    errors = [result for result in results if result not in ("Approved", "already handled")]
    if not approved and not errors:
        return held.strip() or "already handled"
    if errors:
        return f"Approved {approved}. {len(errors)} still pending. {errors[0]}"[:180]
    return f"Approved {approved}.{held}"


async def show_settled_proposals(app: App) -> None:
    rows = await app.proposals.settled(limit=20)
    if not rows:
        await _send(app, "No settled proposals.")
        return
    for start in range(0, len(rows), 10):
        lines, buttons = [], []
        for row in rows[start:start + 10]:
            title = _short(" ".join(row["title"].split()), 120)
            lines.append(f"#{row['id']} · {row['status']} · {title}")
            buttons.append([InlineKeyboardButton(
                text=f"Delete #{row['id']}",
                callback_data=f"prop:del:{row['id']}:{stamp_token(row['created_at'])}",
            )])
        await _send(
            app, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            disable_notification=True,
        )


async def show_reminders(app: App, *, include_settled: bool = False) -> None:
    reminders = await app.dispatch.reminders.list_all() if include_settled else await app.dispatch.reminders.active()
    if not reminders:
        await _send(app, "No reminders." if include_settled else "No active reminders.")
        return
    tz = ZoneInfo(app.config.timezone)
    for start in range(0, len(reminders), 10):
        lines, buttons = [], []
        for row in reminders[start:start + 10]:
            due = datetime.fromisoformat(row["due_at"]).astimezone(tz)
            repeat = f", {row['repeat']}" if row["repeat"] else ""
            if row["status"] != "active":
                status = f", {row['status']}"
            elif row.get("undeliverable"):
                status = ", undeliverable"
            else:
                status = ""
            preview = " ".join(row["text"].split())[:130]
            lines.append(f"#{row['id']} {due:%Y-%m-%d %H:%M} ({row['importance']}{repeat}{status}) {preview}")
            del_data = f"rem:del:{row['id']}:{stamp_token(row['created_at'])}"
            if row["status"] == "active":
                buttons.append([
                    InlineKeyboardButton(text=f"Cancel #{row['id']}", callback_data=f"rem:cancel:{row['id']}"),
                    InlineKeyboardButton(text=f"Delete #{row['id']}", callback_data=del_data),
                ])
            else:
                buttons.append([InlineKeyboardButton(text=f"Delete #{row['id']}", callback_data=del_data)])
        await _send(
            app, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


async def edit_stickers(app: App, args: list[str]) -> None:
    if len(args) != 2 or args[0] not in ("add", "remove"):
        await _send(app, "Use /stickers add <set_name> or /stickers remove <set_name>.")
        return
    result = await change_sticker_set(app, *args)
    await _send(app, result)


async def change_sticker_set(app: App, action: str, name: str) -> str:
    if action not in ("add", "remove"):
        raise ValueError("Choose Add or Remove.")
    try:
        result = await app.stickers.change(action, name)
    except (OSError, ValueError) as error:
        result = str(error) if isinstance(error, ValueError) else "Could not update stickers.toml."
    await app.store.record_owner_event(result)
    return result


async def refresh_sticker_descriptions(app: App) -> str:
    if app.media_describer is None:
        return "Sticker descriptions unavailable: media model is not configured."
    try:
        pending = await app.stickers.refresh_descriptions()
    except (OSError, ValueError) as error:
        result = str(error) if isinstance(error, ValueError) else "Sticker descriptions could not be refreshed."
    else:
        if pending:
            noun = "sticker" if pending == 1 else "stickers"
            result = f"Sticker descriptions: describing {pending} {noun} in the background."
        else:
            result = "Sticker descriptions: no stickers to describe."
    await app.store.record_owner_event(result)
    return result


async def export_memory(app: App) -> str | None:
    """Send the owner a zip of Dom's memory as a document. Returns an error line for the DM, or None when it was sent."""
    try:
        data = await asyncio.to_thread(memory_export.build_export, app.home.root)
    except memory_export.ExportTooLarge as exc:
        return str(exc)
    except OSError:
        log.exception("could not build the memory export")
        return "Couldn't build the export. Check that Dom can read his notes."
    name = memory_export.export_name(app.dispatch.now())
    await retry_telegram(
        lambda: app.bot.send_document(app.owner_id, BufferedInputFile(data, filename=name), caption="Everything Dom remembers, as a zip."),
        sleep=app.sleep,
    )
    await record_event(app, "Ran /export")
    return None


async def import_memory(app: App, message: Message, text: str) -> tuple[str, str, str | None]:
    """Prepare the DM response, the owner event and, for a bare /import, the prompt to send as a code block."""
    line, _, inline = text.partition("\n")
    args = line.split()[1:]
    event = "Ran /import"
    code = None
    if not args and message.document is None:
        response = (
            "Paste the prompt below into ChatGPT, Claude, Muse or any other assistant (tap it to copy). Reply here with /import <service> "
            "on the first line and the export below. You can also reply to the export with /import <service>, or "
            "attach a .txt or .md document with that caption."
        )
        code = memory_import.export_prompt().strip()
    elif len(args) == 1 and args[0].lower() == "siri":
        response = "Siri has no exportable memory."
        event = "Ran /import siri"
    elif len(args) != 1 or args[0].lower() not in memory_import.SERVICES:
        response = "Use /import chatgpt, /import claude, /import muse or /import other."
    else:
        service = args[0].lower()
        try:
            if message.document is not None:
                source = await memory_import.download_text(app.bot, message.document)
            elif inline.strip():
                source = inline
            elif (
                (reply := message.reply_to_message) is not None
                and reply.chat.id == app.owner_id and reply.chat.type == "private" and reply.text
            ):
                source = reply.text
            else:
                raise ValueError("Reply to the export or attach a .txt or .md document with /import <service>.")
            parsed = await app.memory_import.run(service, source)
        except (OSError, ValueError) as error:
            response = str(error) if isinstance(error, ValueError) else "Could not read the import or existing notes."
            event = f"Could not import memories from {service}"
        else:
            response = memory_import.summary(service, parsed)
            event = response
    return response, event, code


async def show_t3(app: App) -> None:
    if app.t3 is None:
        await _send(app, "T3 bridge off.")
    else:
        await app.t3.show()


# ---- /update -----------------------------------------------------------------------

UPDATE_OFFER_KEY = "update:offer"
_update_reports: set[asyncio.Task] = set()


async def offer_update(app: App) -> None:
    """Show the newest release newer than this one, with its changelog and a one-time confirm button."""
    url = app.config.update.repo_url
    try:
        layout = release.Layout.installed()
        offer = await asyncio.to_thread(release.check, layout, url, VERSION)
    except release.DeployError as error:
        await _send(app, str(error))
        return
    except (OSError, subprocess.SubprocessError):
        log.warning("could not check %s for releases", url, exc_info=True)
        await _send(app, f"Could not check {url} for releases.")
        return
    if offer is None:
        await app.store.kv_set(UPDATE_OFFER_KEY, "")
        await _send(app, f"Dom is up to date ({VERSION}).")
        return
    nonce = secrets.token_hex(8)
    await app.store.kv_set(UPDATE_OFFER_KEY, json.dumps({"nonce": nonce, "tag": offer.tag, "commit": offer.commit}))
    text = f"{offer.tag} is available (running {VERSION}).\n\n{offer.notes}"[:MAX_MESSAGE]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Update to {offer.tag}", callback_data=f"update:go:{nonce}"),
        InlineKeyboardButton(text="Not now", callback_data=f"update:no:{nonce}"),
    ]])
    await _send(app, text, reply_markup=markup)


async def decide_update(app: App, decision: str, nonce: str) -> str:
    """Use the offer at most once: deploy it on "go", drop it on "no". Returns the button toast."""
    async with app.locks[UPDATE_OFFER_KEY]:
        raw = await app.store.kv_get(UPDATE_OFFER_KEY)
        offer = json.loads(raw) if raw else None
        if not offer or not hmac.compare_digest(offer["nonce"], nonce):
            return "already handled"
        await app.store.kv_set(UPDATE_OFFER_KEY, "")
    if decision != "go":
        return "Not now"
    chosen = release.Release(offer["tag"], offer["commit"], "")
    try:
        layout = release.Layout.installed()
        await asyncio.to_thread(release.launch, layout, app.home.root, chosen)
    except release.DeployError as error:
        await _send(app, str(error))
        return "Not started"
    await record_event(app, f"Started the update to {chosen.tag}")
    await _send(
        app, f"Updating to {chosen.tag}. Dom restarts on the way and reports back here when it is done.",
    )
    watch_update(app)
    return f"Updating to {chosen.tag}"


def watch_update(app: App) -> None:
    """If a deploy is in progress or finished unreported, send its outcome to the owner when it is done."""
    if app.owner_id is None or release.read_state(app.home.root) is None or _update_reports:
        return

    async def send(text: str) -> None:
        await _send(app, text)
        await record_event(app, text[:250])

    task = asyncio.create_task(release.deliver_result(app.home.root, send, sleep=asyncio.sleep))
    _update_reports.add(task)
    task.add_done_callback(_update_reports.discard)

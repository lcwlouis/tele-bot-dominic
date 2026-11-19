"""What only the owner sees: the "/" command menu, the /status overview with its wake/sleep/leave buttons and /chats."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.owner")

OWNER_COMMANDS = [
    BotCommand(command="status", description="Where Dom is and what he's doing"),
    BotCommand(command="wake", description="Wake Dom up"),
    BotCommand(command="sleep", description="Send Dom to sleep"),
    BotCommand(command="pending", description="Chats waiting for your approval"),
    BotCommand(command="chats", description="Every group and DM Dom knows about"),
]

MAX_MESSAGE = 4096  # Telegram's limit for a message's text
MAX_BUTTONS = 100  # Telegram's limit for one keyboard
MAX_MEMBER_LOOKUPS = 30  # get_chat_member_count calls per /chats, 0.2 s apart
_STATUS_ORDER = (("approved", "Approved"), ("pending_approval", "Pending"), ("ignored", "Ignored"), ("left", "Left"))
_ICONS = {"asleep": "🌙", "online": "☀️", "offline": "💤"}


def _is_group(chat: dict) -> bool:
    return str(chat["chat_id"]).startswith("-")


def _short(title: str, limit: int = 20) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"


async def publish_menu(bot, owner_id: int) -> None:
    """Show the owner the command menu and everyone else none. A failure is logged, never raised."""
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    except Exception:
        log.exception("could not set the owner's command menu")
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception:
        log.exception("could not clear the default command menu")


def display_title(chat: dict, owner_id: int | None) -> str:
    chat_id = str(chat["chat_id"])
    if chat_id == str(owner_id):
        return "your DM"
    name = " ".join((chat["title"] or "").split())
    if chat_id.startswith("-"):
        return (name or chat_id)[:60]
    return f"DM with {(name or ' '.join((chat.get('peer_name') or '').split()) or chat_id)[:50]}"


async def render_status(app: App) -> tuple[str, InlineKeyboardMarkup]:
    """One line and one Wake/Sleep(/Leave, for groups) button row per approved chat, the owner's DM first,
    then by recent activity."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    tz = ZoneInfo(app.config.timezone)
    pending = len(await app.store.list_pending_chats())
    models = app.config.models
    footer = "Pending approval: {} · Chat model: {} · Triage model: {}".format(
        pending, *(models[name].model if name in models else "n/a" for name in ("chat", "triage"))
    )

    lines, rows, used, button_count = [], [], len(footer) + 80, 0  # room for the footer, the heading and the "more" line
    for chat, (state, until) in zip(chats, states):
        unread, _ = await app.store.get_unread(chat["chat_id"])
        when = f" until {until.astimezone(tz):%H:%M}" if until else ""
        line = f"{_ICONS[state]} {display_title(chat, app.owner_id)}: {state}{when} · {unread} unread"
        row = [
            InlineKeyboardButton(text=f"☀️ {_short(display_title(chat, app.owner_id))}", callback_data=f"wake:{chat['chat_id']}"),
            InlineKeyboardButton(text="🌙 Sleep", callback_data=f"sleep:{chat['chat_id']}"),
        ]
        if _is_group(chat):
            row.append(InlineKeyboardButton(text="🚪 Leave", callback_data=f"leave:{chat['chat_id']}"))
        if used + len(line) + 1 > MAX_MESSAGE or button_count + len(row) > MAX_BUTTONS:
            break
        used += len(line) + 1
        button_count += len(row)
        lines.append(line)
        rows.append(row)
    if len(chats) > len(lines):
        lines.append(f"…and {len(chats) - len(lines)} more")
    text = "\n".join(["Dom's chats", "", *lines, "", footer])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_chats(app: App) -> tuple[str, InlineKeyboardMarkup]:
    """Every chat Dom knows, grouped by status. Groups show their member count (the first MAX_MEMBER_LOOKUPS
    of them, "?" when Telegram refuses); approved and pending groups get a Leave button."""
    known = await app.store.list_chats()
    by_status = [(label, [c for c in known if c["authorization_status"] == status]) for status, label in _STATUS_ORDER]
    total = sum(len(chats) for _, chats in by_status)

    lines, rows, used, lookups, shown = [], [], 80, 0, 0  # room for the heading and the "more" line
    full = False
    for label, chats in by_status:
        heading_added = False
        for chat in chats:
            if full:
                break
            title = display_title(chat, app.owner_id)
            count = ""
            if _is_group(chat) and label != "Left" and lookups < MAX_MEMBER_LOOKUPS:
                if lookups:
                    await app.sleep(0.2)
                lookups += 1
                try:
                    members = await app.bot.get_chat_member_count(int(chat["chat_id"]))
                except Exception:
                    log.warning("could not count the members of chat %s", chat["chat_id"])
                    members = "?"
                count = f" · {members} members" if members != 1 else " · 1 member"
            line = f"• {title}{count}"
            cost = len(line) + 1 + (0 if heading_added else len(label) + 2)
            leave = label in ("Approved", "Pending") and _is_group(chat)
            if used + cost > MAX_MESSAGE or len(rows) + leave > MAX_BUTTONS:
                full = True
                break
            if not heading_added:
                lines += ["", label]
                heading_added = True
            used += cost
            shown += 1
            lines.append(line)
            if leave:
                rows.append([InlineKeyboardButton(text=f"🚪 Leave {_short(title)}", callback_data=f"leave:{chat['chat_id']}")])
    if total > shown:
        lines += ["", f"…and {total - shown} more"]
    return "\n".join(["Dom's chats", *lines]) if shown else "Dom doesn't know any chats yet.", InlineKeyboardMarkup(inline_keyboard=rows)

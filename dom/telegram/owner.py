"""What only the owner sees: the "/" command menu and the /status overview with its wake/sleep buttons."""

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
]

MAX_MESSAGE = 4096  # Telegram's limit for a message's text
MAX_CHATS = 40  # two buttons a row; Telegram allows at most 100 buttons on a keyboard
_ICONS = {"asleep": "🌙", "online": "☀️", "offline": "💤"}


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
    return f"DM with {(name or ' '.join((chat['peer_name'] or '').split()) or chat_id)[:50]}"


async def render_status(app: App) -> tuple[str, InlineKeyboardMarkup]:
    """One line and one Wake/Sleep button row per approved chat, the owner's DM first, then by recent activity."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    tz = ZoneInfo(app.config.timezone)
    pending = len(await app.store.list_pending_chats())
    models = app.config.models
    footer = "Pending approval: {} · Chat model: {} · Triage model: {}".format(
        pending, *(models[name].model if name in models else "n/a" for name in ("chat", "triage"))
    )

    lines, rows, used = [], [], len(footer) + 80  # room for the footer, the heading and the "more" line
    for chat, (state, until) in zip(chats[:MAX_CHATS], states):
        unread, _ = await app.store.get_unread(chat["chat_id"])
        when = f" until {until.astimezone(tz):%H:%M}" if until else ""
        line = f"{_ICONS[state]} {display_title(chat, app.owner_id)}: {state}{when} · {unread} unread"
        if used + len(line) + 1 > MAX_MESSAGE:
            break
        used += len(line) + 1
        lines.append(line)
        rows.append([
            InlineKeyboardButton(text=f"☀️ {_short(display_title(chat, app.owner_id))}", callback_data=f"wake:{chat['chat_id']}"),
            InlineKeyboardButton(text="🌙 Sleep", callback_data=f"sleep:{chat['chat_id']}"),
        ])
    if len(chats) > len(lines):
        lines.append(f"…and {len(chats) - len(lines)} more")
    text = "\n".join(["Dom's chats", "", *lines, "", footer])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

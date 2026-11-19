"""aiogram router: record messages, gate by approval, run turns, handle owner approvals."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from dom.agent import run_turn
from dom.claim import try_claim
from dom.config import Config, load_config
from dom.home import Home
from dom.llm import ChatClient
from dom.models import IncomingMessage, StoredMessage
from dom.prompt import build_messages
from dom.store import Store
from dom.telegram import executor, gate
from dom.telegram.convert import to_incoming
from dom.tools import TurnCtx, core_tools

log = logging.getLogger("dom.handlers")
CHAT_TYPES = ("private", "group", "supergroup")


@dataclass
class App:
    config: Config
    home: Home
    store: Store
    client: ChatClient
    bot: Any
    bot_username: str
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    locks: defaultdict = field(default_factory=lambda: defaultdict(asyncio.Lock))

    @property
    def owner_id(self) -> int | None:
        return self.store.owner_id


# ---- triggers --------------------------------------------------------------------

def _is_mention(text: str, username: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_])@{re.escape(username)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def _is_command_for_us(text: str, username: str) -> bool:
    if not text.startswith("/"):
        return False
    _, _, target = text.split()[0].partition("@")
    return not target or target.lower() == username.lower()


def should_respond(app: App, message: Message, msg: IncomingMessage) -> bool:
    """DMs always get a turn; groups only on @mention, a reply to Dom, or a command for Dom."""
    if msg.chat_type == "private":
        return True
    username = app.bot_username
    replied = message.reply_to_message
    replied_to_dom = (
        replied is not None
        and replied.from_user is not None
        and (replied.from_user.username or "").lower() == username.lower()
    )
    return replied_to_dom or _is_mention(msg.text, username) or _is_command_for_us(msg.text, username)


# ---- recording and turns -----------------------------------------------------------

async def _record(app: App, msg: IncomingMessage) -> None:
    await app.store.resolve_person(msg.sender_id, msg.sender_handle, msg.sender_name)
    await app.store.record_new_message(
        StoredMessage(
            chat_id=str(msg.chat_id),
            msg_id=msg.msg_id,
            reply_to_msg_id=msg.reply_to,
            sender_user_id=msg.sender_id,
            sender_handle=msg.sender_handle or "",
            sender_name=msg.sender_name,
            is_bot=msg.is_bot,
            visibility_scope=app.store.visibility_for(msg.chat_id),
            text=msg.text or (f"[{msg.media['type']}]" if msg.media else ""),
            media_json=json.dumps(msg.media) if msg.media else None,
            created_at=msg.date.isoformat(),
        )
    )


def _turn_note(app: App, msg: IncomingMessage, scope: str) -> str:
    now = datetime.now(ZoneInfo(app.config.timezone))
    where = f'chat "{msg.chat_title}"' if msg.chat_title else "a direct message"
    return f"It is {now:%A %Y-%m-%d %H:%M} ({app.config.timezone}). You are in {where} ({scope})."


async def _run_chat_turn(app: App, msg: IncomingMessage) -> None:
    scope = app.store.scope_for(msg.chat_id, msg.chat_type)
    window = await app.store.get_recent_chat_window(msg.chat_id, app.config.window_size)
    participants = list(dict.fromkeys(
        [m.sender_user_id for m in window if m.role == "user" and m.sender_user_id is not None] + [msg.sender_id]
    ))
    messages = build_messages(
        app.home, scope, msg.chat_id, participants,
        app.store.format_chat_window_xml(str(msg.chat_id), window), _turn_note(app, msg, scope),
    )
    ctx = TurnCtx(
        scope=scope, chat_id=msg.chat_id, sender_id=msg.sender_id,
        home=app.home, store=app.store, owner_id=app.owner_id,
    )
    actions = await run_turn(app.client, messages, core_tools(), ctx)
    await executor.perform(app.bot, app.store, msg.chat_id, msg.msg_id, actions, sleep=app.sleep)


def _claim_code(text: str, username: str) -> str | None:
    """The code in `/claim <code>` (or `/claim@bot <code>`), or None if this is not a claim."""
    parts = text.split()
    if not parts:
        return None
    command, _, target = parts[0].partition("@")
    if command != "/claim" or (target and target.lower() != username.lower()):
        return None
    return parts[1] if len(parts) > 1 else ""


async def _handle_claim(app: App, msg: IncomingMessage, code: str) -> None:
    if try_claim(app.config.home, app.config, msg.sender_id, msg.chat_type, code):
        await app.store.set_owner(msg.sender_id)
        app.config = load_config(app.config.home)
        await app.bot.send_message(msg.chat_id, "Done: you are my owner now. Other chats will ask you before I talk in them.")
    elif msg.chat_type == "private":
        await app.bot.send_message(msg.chat_id, "That code did not work.")


async def handle_message(app: App, message: Message) -> None:
    if message.chat.type not in CHAT_TYPES:
        return
    msg = to_incoming(message)
    if msg.is_bot:
        return
    if app.owner_id is None:  # until an owner exists, the only thing Dom answers is /claim
        code = _claim_code(msg.text, app.bot_username)
        if code is not None:
            await _handle_claim(app, msg, code)
            return
    decision = await gate.decide(app.store, msg, app.owner_id)
    if decision == "drop":
        return
    await _record(app, msg)
    if decision == "hold":
        kind = "chat" if msg.chat_type == "private" else "group"
        await gate.request_approval(
            app.bot, app.store, app.owner_id, msg.chat_id,
            msg.chat_title or msg.sender_name, kind, msg.sender_handle or msg.sender_name,
        )
        return
    if not should_respond(app, message, msg):
        return
    async with app.locks[msg.chat_id]:
        try:
            await _run_chat_turn(app, msg)
        except Exception:  # one failed turn must not stop the bot
            log.exception("turn failed in chat %s", msg.chat_id)


async def handle_edited_message(app: App, message: Message) -> None:
    if message.chat.type in CHAT_TYPES:
        await app.store.record_message_edit(message.chat.id, message.message_id, message.text or message.caption or "")


# ---- owner decisions -----------------------------------------------------------------

_DECISIONS = {"approve": "approved", "ignore": "ignored"}


async def handle_callback(app: App, query: CallbackQuery) -> None:
    owner_id = app.owner_id
    if owner_id is None or query.from_user.id != owner_id:
        return
    action, _, raw_chat_id = (query.data or "").partition(":")
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return
    if action not in _DECISIONS:
        return
    chat = await app.store.set_chat_auth_status(str(chat_id), _DECISIONS[action])
    label = "Approved" if action == "approve" else "Ignored"
    await app.bot.answer_callback_query(query.id, text=label)
    if query.message is not None:
        await app.bot.edit_message_text(
            f"{label}: {chat['title'] or chat_id}", chat_id=query.message.chat.id, message_id=query.message.message_id
        )


async def handle_my_chat_member(app: App, update: ChatMemberUpdated) -> None:
    """Dom was added to a group: it starts pending and the owner is asked."""
    if update.chat.type not in ("group", "supergroup") or update.new_chat_member.status not in ("member", "administrator"):
        return
    owner_id = app.owner_id
    if owner_id is None:
        return
    inviter = f"@{update.from_user.username}" if update.from_user.username else update.from_user.full_name
    await app.store.get_chat_auth_status(update.chat.id)
    await gate.request_approval(
        app.bot, app.store, owner_id, update.chat.id, update.chat.title or str(update.chat.id), "group", inviter
    )


def build_router(app: App) -> Router:
    router = Router()

    @router.message()
    async def on_message(message: Message) -> None:
        await handle_message(app, message)

    @router.edited_message()
    async def on_edited(message: Message) -> None:
        await handle_edited_message(app, message)

    @router.callback_query(F.data.startswith(("approve:", "ignore:")))
    async def on_callback(query: CallbackQuery) -> None:
        await handle_callback(app, query)

    @router.my_chat_member()
    async def on_my_chat_member(update: ChatMemberUpdated) -> None:
        await handle_my_chat_member(app, update)

    return router

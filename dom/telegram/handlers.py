"""aiogram router: record messages, gate by approval, run turns, handle owner approvals."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import Random
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from dom.claim import try_claim
from dom.config import Config, load_config
from dom.dispatch import Dispatcher
from dom.home import Home
from dom.jev import JevClient
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.models import IncomingMessage, StoredMessage
from dom.presence import Presence
from dom.store import Store
from dom.telegram import gate
from dom.telegram.convert import to_incoming

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
    locks: defaultdict = field(default_factory=lambda: defaultdict(asyncio.Lock))  # one turn at a time per chat
    ingest_locks: defaultdict = field(default_factory=lambda: defaultdict(asyncio.Lock))  # decide + record, in order

    jev: JevClient | None = None
    bot_id: int | None = None
    media_describer: MediaDescriber | None = None
    dispatch: Dispatcher = field(init=False)

    def __post_init__(self) -> None:
        def now() -> datetime:
            return datetime.now(timezone.utc)

        presence = Presence(self.store, ZoneInfo(self.config.timezone), Random(), now)
        self.dispatch = Dispatcher(self, presence, self.jev, self.sleep, now)

    @property
    def owner_id(self) -> int | None:
        return self.store.owner_id


# ---- triggers --------------------------------------------------------------------

def _command_name(text: str, username: str) -> str | None:
    """`/cmd` from `/cmd`, `/cmd args` or `/cmd@bot args` addressed to us; otherwise None."""
    if not text.startswith("/"):
        return None
    name, _, target = text.split()[0].partition("@")
    return name if not target or target.lower() == username.lower() else None


async def _record(app: App, msg: IncomingMessage, *, describe_media: bool = False) -> None:
    new_media = (
        describe_media and app.media_describer is not None and msg.media is not None
        and not await app.store.has_message(msg.chat_id, msg.msg_id)
    )
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
            text=msg.text or (f"[{msg.media['kind']}]" if msg.media else ""),
            media_json=json.dumps({**msg.media, "description": None}) if msg.media else None,
            created_at=msg.date.isoformat(),
        )
    )
    if new_media:
        app.dispatch.describe_media(msg)


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


def _is_own_message(app: App, msg: IncomingMessage) -> bool:
    if not msg.is_bot:
        return False
    if app.bot_id is not None:
        return msg.sender_id == app.bot_id
    return bool(app.bot_username and (msg.sender_handle or "").lower() == f"@{app.bot_username.lower()}")


async def handle_message(app: App, message: Message) -> None:
    if message.chat.type not in CHAT_TYPES:
        return
    msg = to_incoming(message)
    if _is_own_message(app, msg):
        return
    if app.owner_id is None and not msg.is_bot:  # until an owner exists, the only thing Dom answers is /claim
        code = _claim_code(msg.text, app.bot_username)
        if code is not None:
            await _handle_claim(app, msg, code)
            return
    if not msg.is_bot and msg.chat_type == "private" and msg.chat_id == msg.sender_id == app.owner_id:
        command = _command_name(msg.text, app.bot_username)
        if command == "/pending":
            await gate.resend_pending(app.bot, app.store, app.owner_id)
            return
        if command in ("/wake", "/sleep"):
            parts = msg.text.split()
            try:
                target = msg.chat_id if len(parts) == 1 else int(parts[1])
            except ValueError:
                return
            if len(parts) <= 2:
                await app.dispatch.override(target, command)
            return
    async with app.ingest_locks[msg.chat_id]:
        decision = await gate.decide(app.store, msg, app.owner_id)
        if decision == "drop":
            return
        await _record(app, msg, describe_media=decision == "process")
        if decision == "process":
            app.dispatch.notify(msg)
    if decision == "hold":
        kind = "chat" if msg.chat_type == "private" else "group"
        await gate.request_approval(
            app.bot, app.store, app.owner_id, msg.chat_id,
            msg.chat_title or msg.sender_name, kind, msg.sender_handle or msg.sender_name,
        )
        return

async def handle_edited_message(app: App, message: Message) -> None:
    if message.chat.type not in CHAT_TYPES:
        return
    msg = to_incoming(message)
    if _is_own_message(app, msg):
        return
    async with app.ingest_locks[msg.chat_id]:  # after the original's own decide + record has finished
        edited = await app.store.record_message_edit(
            msg.chat_id, msg.msg_id, msg.text, dom_was_online_and_read=False,
        )
        if edited is None:
            decision = await gate.decide(app.store, msg, app.owner_id)
            if decision != "drop":
                # The original never reached us. Keep this version as a new message.
                await _record(app, msg, describe_media=decision == "process")


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
    chat = await app.store.resolve_pending_chat(str(chat_id), _DECISIONS[action])
    if chat is None:
        await app.bot.answer_callback_query(query.id, text="already handled")
        return
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

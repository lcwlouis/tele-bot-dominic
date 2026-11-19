"""aiogram router: record messages, gate by approval, run turns, handle owner approvals."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import Random
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from dom.claim import try_claim
from dom.config import Config, load_config
from dom.dispatch import Dispatcher
from dom.home import Home
from dom.jev import JevClient
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.models import IncomingMessage, StoredMessage
from dom.presence import Presence
from dom.proposals import Proposals
from dom.store import Store
from dom.search import SearchClient
from dom.traits import load_preset
from dom.telegram import gate, owner
from dom.telegram.convert import to_incoming

CHAT_TYPES = ("private", "group", "supergroup")

log = logging.getLogger("dom.handlers")


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
    search_client: SearchClient | None = None
    dispatch: Dispatcher = field(init=False)
    proposals: Proposals = field(init=False)

    def __post_init__(self) -> None:
        def now() -> datetime:
            return datetime.now(timezone.utc)

        self.proposals = Proposals(self)
        presence = Presence(self.store, ZoneInfo(self.config.timezone), Random(), now)
        self.dispatch = Dispatcher(self, presence, self.jev, self.sleep, now)

    @property
    def owner_id(self) -> int | None:
        return self.store.owner_id


# ---- owner events ------------------------------------------------------------------

async def _owner_event(app: App, text: str) -> None:
    """Note an owner command or button press in the owner's DM history. Never lets a failure reach the owner."""
    try:
        await app.store.record_owner_event(text)
    except Exception:
        log.exception("could not record an owner event")


def _person(name: str) -> str:
    return " ".join(name.split()) or "The owner"


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
    await app.store.set_chat_title(msg.chat_id, msg.chat_title)
    if new_media:
        await app.dispatch.describe_media(msg)


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
        await owner.publish_menu(app.bot, msg.sender_id)
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
        who = _person(msg.sender_name)
        if command == "/pending":
            await gate.resend_pending(app.bot, app.store, app.owner_id)
            await _owner_event(app, f"{who} ran /pending: {await owner.pending_summary(app)}")
            return
        if command == "/status":
            await owner.send_view(app.bot, msg.chat_id, await owner.render_status(app))
            await _owner_event(app, f"{who} ran /status: {await owner.status_summary(app)}")
            return
        if command == "/chats":
            await owner.send_view(app.bot, msg.chat_id, await owner.render_chats(app))
            await _owner_event(app, f"{who} ran /chats: {await owner.chats_summary(app)}")
            return
        if command == "/reminders":
            await owner.show_reminders(app)
            await _owner_event(app, f"{who} ran /reminders")
            return
        if command == "/proposals":
            await owner.show_proposals(app)
            await _owner_event(app, f"{who} ran /proposals")
            return
        if command == "/preset":
            await owner.show_presets(app)
            return
        if command in ("/wake", "/sleep"):
            parts = msg.text.split()
            try:
                target = msg.chat_id if len(parts) == 1 else int(parts[1])
            except ValueError:
                return
            if len(parts) <= 2:
                if not await app.dispatch.override(target, command):
                    await app.bot.send_message(msg.chat_id, "unknown chat")
                else:
                    if command == "/wake" and await app.dispatch.spend_paused(target):
                        await app.bot.send_message(msg.chat_id, await _spend_paused_notice(app, target))
                    elif len(parts) == 1:
                        await app.bot.send_message(msg.chat_id, "Dom is up." if command == "/wake" else "Dom is asleep.")
                    await _owner_event(app, await _override_event(app, who, command, target))
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


_OVERRIDES = {"wake": "/wake", "sleep": "/sleep"}


async def _chat_title(app: App, chat_id: int) -> str:
    chat = next((c for c in await app.store.list_approved_chats() if c["chat_id"] == str(chat_id)), {"chat_id": str(chat_id), "title": ""})
    return owner.display_title(chat, app.owner_id)


async def _spend_paused_notice(app: App, chat_id: int) -> str:
    return f"Dom is up in {await _chat_title(app, chat_id)}, but friends' chats are paused by today's spend cap"


async def _override_event(app: App, who: str, command: str, chat_id: int) -> str:
    title = await _chat_title(app, chat_id)
    return f"{who} woke Dom in {title}" if command == "/wake" else f"{who} sent Dom to sleep in {title}"


async def _toast(app: App, query: CallbackQuery, text: str | None = None) -> None:
    try:
        await app.bot.answer_callback_query(query.id, text=text)
    except TelegramBadRequest:  # a turn can outlast the callback's validity; the edit that follows still matters
        log.warning("could not answer callback %s", query.id)


async def _replace_with_prompt(app: App, query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Rich messages cannot be edited, so the owner's old /status message is deleted and the new one sent after it."""
    if query.message is None:
        return
    chat_id = query.message.chat.id
    await app.bot.send_message(chat_id, text, reply_markup=markup)
    await owner.delete_message(app.bot, chat_id, query.message.message_id)


async def _refresh_status(app: App, query: CallbackQuery) -> None:
    if query.message is None:
        return
    chat_id = query.message.chat.id
    await owner.send_view(app.bot, chat_id, await owner.render_status(app))
    await owner.delete_message(app.bot, chat_id, query.message.message_id)


async def _handle_override(app: App, query: CallbackQuery, action: str, raw_chat_id: str) -> None:
    """The Wake / Sleep buttons under /status: the owner's /wake or /sleep, then refresh that message."""
    if app.owner_id is None or query.from_user.id != app.owner_id:
        await _toast(app, query, "not allowed")
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        await _toast(app, query, "unknown chat")
        return
    chat = next((c for c in await app.store.list_approved_chats() if c["chat_id"] == str(chat_id)), None)
    if chat is None or not await app.dispatch.override(chat_id, _OVERRIDES[action]):
        await _toast(app, query, "unknown chat")
        return
    title = owner.display_title(chat, app.owner_id)
    if action == "wake" and await app.dispatch.spend_paused(chat_id):
        await _toast(app, query, await _spend_paused_notice(app, chat_id))
    else:
        await _toast(app, query, f"Dom is up in {title}" if action == "wake" else f"Dom is asleep in {title}")
    await _owner_event(app, await _override_event(app, _person(query.from_user.full_name), _OVERRIDES[action], chat_id))
    await _refresh_status(app, query)


_LEAVABLE = ("approved", "pending_approval", "ignored")


async def _leavable_chat(app: App, raw_chat_id: str) -> dict | None:
    """A known group Dom could still be in. Read-only: never creates a row."""
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return None
    if chat_id >= 0:
        return None
    return next(
        (c for c in await app.store.list_chats() if c["chat_id"] == str(chat_id) and c["authorization_status"] in _LEAVABLE),
        None,
    )


async def _handle_leave(app: App, query: CallbackQuery, action: str, raw_chat_id: str) -> None:
    """The Leave buttons and `status:refresh` (the confirmation's Cancel). The owner confirms before Dom leaves."""
    if app.owner_id is None or query.from_user.id != app.owner_id:
        await _toast(app, query, "not allowed")
        return
    who = _person(query.from_user.full_name)
    if action == "status":
        await _toast(app, query)
        if raw_chat_id == "refresh":
            await _owner_event(app, f"{who} refreshed the /status list")
            await _refresh_status(app, query)
        return
    chat = await _leavable_chat(app, raw_chat_id)
    if chat is None:
        await _toast(app, query, "unknown chat")
        return
    title = owner.display_title(chat, app.owner_id)
    if action == "leave":
        await _toast(app, query)
        await _owner_event(app, f"{who} tapped Leave on {title} (asked to confirm)")
        await _replace_with_prompt(app, query, f"Leave {title}? Friends will see Dom leave.", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Yes, leave", callback_data=f"leaveyes:{chat['chat_id']}"),
            InlineKeyboardButton(text="Cancel", callback_data="status:refresh"),
        ]]))
        return
    try:
        await app.bot.leave_chat(int(chat["chat_id"]))
        result = f"Dom left {title}"
    except (TelegramBadRequest, TelegramForbiddenError):  # Telegram says Dom is not in that chat (any more)
        result = f"Dom wasn't in {title} anymore"
    except Exception as error:
        log.exception("could not leave chat %s", chat["chat_id"])
        await _toast(app, query, f"Couldn't leave: {(getattr(error, 'message', '') or str(error) or type(error).__name__)[:100]}")
        await _owner_event(app, f"{who} tapped Leave on {title}: Dom couldn't leave")
        return
    await app.store.set_chat_auth_status(chat["chat_id"], "left")
    await _owner_event(app, f"{who} tapped Leave on {title}: {result}")
    await _toast(app, query, result)
    await _refresh_status(app, query)


async def _handle_preset(app: App, query: CallbackQuery, action: str, data: str) -> None:
    owner_id = app.owner_id
    if (
        owner_id is None or query.from_user.is_bot or query.from_user.id != owner_id
        or query.message is None or query.message.chat.id != owner_id or query.message.chat.type != "private"
    ):
        await _toast(app, query, "not allowed")
        return
    raw_id, _, name = data.partition(":")
    chat = next((c for c in await app.store.list_chats("approved") if c["chat_id"] == raw_id), None)
    if chat is None:
        await _toast(app, query, "unknown chat")
        return
    if action == "preset":
        await _toast(app, query)
        await owner.choose_preset(app, chat)
        return

    preset = load_preset(app.home, name) if name else None
    if (name and preset is None) or not await app.store.set_chat_preset(raw_id, name):
        await _toast(app, query, "preset or chat unavailable")
        return
    title = owner.display_title(chat, owner_id)
    label = preset.title if preset else "None"
    text = f"Preset for {title}: {label}"
    await _owner_event(app, text)
    await _toast(app, query)
    await app.bot.send_message(owner_id, text)


async def handle_callback(app: App, query: CallbackQuery) -> None:
    action, _, raw_chat_id = (query.data or "").partition(":")
    if action in ("preset", "presetset"):
        await _handle_preset(app, query, action, raw_chat_id)
        return
    if action in _OVERRIDES:
        await _handle_override(app, query, action, raw_chat_id)
        return
    if action in ("leave", "leaveyes", "status"):
        await _handle_leave(app, query, action, raw_chat_id)
        return
    owner_id = app.owner_id
    if owner_id is None or query.from_user.id != owner_id:
        return
    if action in ("prop", "rem"):
        decision, _, raw_id = raw_chat_id.partition(":")
        if query.from_user.is_bot or not (raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 19):
            return
        item_id = int(raw_id)
        if not 0 < item_id < 2**63:
            return
        if action == "prop" and decision in ("yes", "no"):
            await _toast(app, query, await app.proposals.decide(item_id, decision == "yes"))
        if action == "rem" and decision == "cancel":
            changed = await app.dispatch.reminders.cancel(item_id)
            await _toast(app, query, "Cancelled" if changed else "already handled")
        return
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
    await _owner_event(app, f"{_person(query.from_user.full_name)} {label.lower()} {owner.display_title(chat, owner_id)}")
    await app.bot.answer_callback_query(query.id, text=label)
    if query.message is not None:
        await app.bot.edit_message_text(
            f"{label}: {chat['title'] or chat_id}", chat_id=query.message.chat.id, message_id=query.message.message_id
        )


async def _note_removed(app: App, update: ChatMemberUpdated, owner_id: int) -> None:
    """Dom is no longer in a group it knew: stop listening to it and tell the owner once."""
    chat = next((c for c in await app.store.list_chats() if c["chat_id"] == str(update.chat.id)), None)
    if chat is None or chat["authorization_status"] not in _LEAVABLE:  # unknown, already left, or blocked
        return
    await app.store.set_chat_auth_status(chat["chat_id"], "left")
    title = update.chat.title or chat["title"] or chat["chat_id"]
    own_choice = update.new_chat_member.status == "left" and update.from_user.id == app.bot_id
    await app.bot.send_message(owner_id, f"Dom left {title}" if own_choice else f"Dom was removed from {title}")


async def handle_my_chat_member(app: App, update: ChatMemberUpdated) -> None:
    """Dom was added to a group (it starts pending and the owner is asked) or removed from one (it is `left`)."""
    owner_id = app.owner_id
    if update.chat.type not in ("group", "supergroup") or owner_id is None:
        return
    status = update.new_chat_member.status
    if status in ("left", "kicked"):
        await _note_removed(app, update, owner_id)
        return
    if status not in ("member", "administrator"):
        return
    inviter = f"@{update.from_user.username}" if update.from_user.username else update.from_user.full_name
    if (await app.store.get_chat_auth_status(update.chat.id))["authorization_status"] in ("left", "ignored"):
        await app.store.set_chat_auth_status(update.chat.id, "pending_approval", approval_prompt_sent=False)
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

    @router.callback_query(F.data.startswith(("approve:", "ignore:", "wake:", "sleep:", "leave:", "leaveyes:", "status:", "prop:", "rem:")))
    async def on_callback(query: CallbackQuery) -> None:
        await handle_callback(app, query)

    @router.my_chat_member()
    async def on_my_chat_member(update: ChatMemberUpdated) -> None:
        await handle_my_chat_member(app, update)

    return router

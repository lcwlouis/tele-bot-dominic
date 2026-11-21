"""aiogram router: record messages, gate by approval, run turns, handle owner approvals."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import Random
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity,
)

from dom.claim import try_claim
from dom.config import Config, load_config
from dom.dispatch import Dispatcher
from dom.desktop import files as desktop_files
from dom.desktop.fleet import DesktopFleet
from dom.t3.tools import Bridge
from dom.home import Home
from dom.jev import JevClient
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.memory_import import MemoryImport
from dom.models import IncomingMessage, StoredMessage
from dom.presence import Presence
from dom.proposals import Proposals
from dom.plugins.registry import PluginRegistry
from dom.plugins import surfaces
from dom.plugins.runner import PluginRunner
from dom.gifs import GifSearch
from dom.stickers import Stickers
from dom.store import Store
from dom.search import SearchClient
from dom.selfwork import SelfWork
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
    gifs: GifSearch | None = None
    desktop_worker: DesktopFleet | None = field(init=False, default=None)  # every desktop container's worker
    t3: Bridge | None = field(init=False, default=None)
    dispatch: Dispatcher = field(init=False)
    proposals: Proposals = field(init=False)
    stickers: Stickers = field(init=False)
    memory_import: MemoryImport = field(init=False)
    plugins: PluginRegistry = field(init=False)
    plugin_runner: PluginRunner = field(init=False)
    self_work: SelfWork = field(init=False)
    plugin_command_times: dict[int, float] = field(default_factory=dict)  # per-chat cooldown outside the owner DM

    def __post_init__(self) -> None:
        def now() -> datetime:
            return datetime.now(timezone.utc)

        self.plugins = PluginRegistry(self.home, self.store, on_change=lambda: owner.refresh_menu(self))
        self.plugin_runner = PluginRunner(home=self.home.root)
        self.proposals = Proposals(self)
        self.stickers = Stickers(self.home, self.bot, lambda: self.dispatch.now())
        self.memory_import = MemoryImport(self)
        presence = Presence(self.store, ZoneInfo(self.config.timezone), Random(), now)
        if self.config.t3.enabled:
            self.t3 = Bridge(self)
        self.dispatch = Dispatcher(self, presence, self.jev, self.sleep, now)
        self.self_work = SelfWork(self)
        if self.config.desktop_hosts:
            self.desktop_worker = DesktopFleet(self)

    @property
    def owner_id(self) -> int | None:
        return self.store.owner_id


# ---- owner events ------------------------------------------------------------------

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
        await owner.refresh_menu(app)
    elif msg.chat_type == "private":
        await app.bot.send_message(msg.chat_id, "That code did not work.")


def _is_own_message(app: App, msg: IncomingMessage) -> bool:
    if not msg.is_bot:
        return False
    if app.bot_id is not None:
        return msg.sender_id == app.bot_id
    return bool(app.bot_username and (msg.sender_handle or "").lower() == f"@{app.bot_username.lower()}")


_DESKTOP_COMMAND = re.compile(r"/desktop(?:@([A-Za-z0-9_]+))?(?:\s|$)", re.IGNORECASE)


def _desktop_command(text: str, bot_username: str | None) -> bool:
    """The exact /desktop token, optionally addressed to this bot, then whitespace or the end."""
    match = _DESKTOP_COMMAND.match(text)
    return match is not None and (match[1] is None or match[1].lower() == (bot_username or "").lower())


async def _receive_desktop_document(app: App, message: Message, msg: IncomingMessage) -> IncomingMessage:
    document = message.document
    if document is None or msg.media is None:
        return msg
    try:
        if (document.file_size or 0) > desktop_files.MAX_FILE_BYTES:
            raise ValueError("Desktop documents can be at most 20 MB.")
        with desktop_files.BoundedFile() as content:
            downloaded = await app.bot.download(document, destination=content)
            if downloaded is None:
                raise OSError("no document returned")
            name = desktop_files.stage_inbox(app.home.root, document.file_name, content.getvalue())
    except ValueError as error:
        await app.bot.send_message(msg.chat_id, str(error))
        return msg
    except Exception as error:
        log.warning("could not stage a desktop document (%s)", type(error).__name__)
        await app.bot.send_message(msg.chat_id, "Couldn't save the document for the desktop.")
        return msg
    received = f"file received: inbox/{name}"
    await owner.record_event(app, received)
    return msg.model_copy(update={"media": {**msg.media, "desktop_inbox": name}})


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
    command = _command_name(msg.text, app.bot_username)
    owner_dm = (
        message.from_user is not None and not msg.is_bot and message.sender_chat is None
        and msg.chat_type == "private" and msg.chat_id == msg.sender_id == app.owner_id
    )
    if command == "/plugins":
        if owner_dm:
            await owner.show_plugins(app)
            await owner.record_event(app, f"{_person(msg.sender_name)} ran /plugins")
        return
    if command == "/t3":
        if owner_dm:
            await owner.show_t3(app)
        return
    if command == "/tasks":
        if owner_dm:
            await owner.show_tasks(app)
            await owner.record_event(app, f"{_person(msg.sender_name)} ran /tasks")
        return
    if command == "/import":
        if owner_dm:
            response, event, code = await owner.import_memory(app, message, msg.text)
            await owner.record_event(app, event)
            await app.bot.send_message(msg.chat_id, response)
            if code:  # its own message as one code block: one tap copies it with its line breaks
                await app.bot.send_message(msg.chat_id, code, entities=[
                    MessageEntity(type="pre", offset=0, length=len(code.encode("utf-16-le")) // 2),
                ])
        return
    if owner_dm:
        who = _person(msg.sender_name)
        if command == "/pending":
            await gate.resend_pending(app.bot, app.store, app.owner_id)
            await owner.record_event(app, f"{who} ran /pending: {await owner.pending_summary(app)}")
            return
        if command == "/status":
            await owner.send_view(app.bot, msg.chat_id, await owner.render_status(app))
            await owner.record_event(app, f"{who} ran /status: {await owner.status_summary(app)}")
            return
        if command == "/chats":
            await owner.send_view(app.bot, msg.chat_id, await owner.render_chats(app))
            await owner.record_event(app, f"{who} ran /chats: {await owner.chats_summary(app)}")
            return
        if command == "/stickers":
            await owner.edit_stickers(app, msg.text.split()[1:])
            return
        if command == "/reminders":
            await owner.show_reminders(app)
            await owner.record_event(app, f"{who} ran /reminders")
            return
        if command == "/proposals":
            await owner.show_proposals(app)
            await owner.record_event(app, f"{who} ran /proposals")
            return
        if command == "/preset":
            await owner.show_presets(app)
            return
        if command == "/update":
            await owner.offer_update(app)
            await owner.record_event(app, f"{who} ran /update")
            return
        if command in ("/wake", "/sleep"):
            parts = msg.text.split()
            try:
                target = msg.chat_id if len(parts) == 1 else int(parts[1])
            except ValueError:
                return
            if len(parts) <= 2:
                result = await owner.override_chat(app, target, command, who)
                if result is None:
                    await app.bot.send_message(msg.chat_id, "unknown chat")
                elif result.spend_paused:
                    await app.bot.send_message(msg.chat_id, result.message)
                elif len(parts) == 1:
                    await app.bot.send_message(msg.chat_id, "Dom is up." if command == "/wake" else "Dom is asleep.")
            return
    # Core commands are handled above, so an approved plugin can never shadow one.
    if command is not None and await surfaces.handle_command(app, message, msg, command):
        return
    async with app.ingest_locks[msg.chat_id]:
        decision = await gate.decide(app.store, msg, app.owner_id)
        if decision == "drop":
            return
        if (
            owner_dm and message.document is not None and app.config.desktop_mode != "off"
            and _desktop_command(msg.text, app.bot_username) and decision == "process" and not await app.store.has_message(msg.chat_id, msg.msg_id)
        ):
            msg = await _receive_desktop_document(app, message, msg)
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
    if _is_own_message(app, msg) or _command_name(msg.text, app.bot_username) == "/import":
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


_OVERRIDES = {"wake": "/wake", "sleep": "/sleep"}


async def _toast(app: App, query: CallbackQuery, text: str | None = None) -> None:
    try:
        await app.bot.answer_callback_query(query.id, text=text)
    except TelegramBadRequest:  # a turn can outlast the callback's validity; the edit that follows still matters
        log.warning("could not answer callback %s", query.id)


SETTLED = {"Approved": "✅ Approved", "Rejected": "❌ Rejected", "Cancelled": "Cancelled", "already handled": "Already handled"}
_PROPOSAL_BUTTON = re.compile(r"^prop:(?:yes|no):(\d+)$")
_REMINDER_BUTTON = re.compile(r"^rem:cancel:(\d+)$")
_PROPOSAL_LABELS = {"approved": "✅ Approved", "rejected": "❌ Rejected", "expired": "Expired"}


def _row_id(row: list[InlineKeyboardButton], pattern: re.Pattern) -> int | None:
    for button in row:
        if (found := pattern.match(button.callback_data or "")) is not None:
            return int(found.group(1))
    return None


async def _settle_button(app: App, query: CallbackQuery, outcome: str, suffix: str = "") -> None:
    """Show on the card what has been settled, on buttons that do nothing, so a stale Approve or Cancel cannot be
    tapped again. Every proposal and reminder row is rebuilt from the database, not from the tapped message alone:
    on a /proposals list two quick taps each carry the keyboard as it was before the other's edit."""
    label = SETTLED.get(outcome)
    message = query.message
    if label is None or message is None:
        return
    markup = getattr(message, "reply_markup", None)
    rows = list(markup.inline_keyboard) if markup is not None else []
    proposals = [pid for row in rows if (pid := _row_id(row, _PROPOSAL_BUTTON)) is not None]
    statuses = await app.store.proposal_statuses(proposals)
    reminders = [rid for row in rows if (rid := _row_id(row, _REMINDER_BUTTON)) is not None]
    active = {row["id"] for row in await app.dispatch.reminders.active()} if reminders else set()
    numbered = len(proposals) > 1
    keyboard = []
    for row in rows:
        data = [button.callback_data or "" for button in row]
        if (pid := _row_id(row, _PROPOSAL_BUTTON)) is not None and statuses.get(pid, "pending") != "pending":
            text = _PROPOSAL_LABELS.get(statuses[pid], "Handled") + (f" #{pid}" if numbered else "")
            keyboard.append([InlineKeyboardButton(text=text, callback_data="noop")])
        elif (rid := _row_id(row, _REMINDER_BUTTON)) is not None and rid not in active:
            ended = "Cancelled" if query.data == f"rem:cancel:{rid}" else "Ended"  # another may have fired since
            keyboard.append([InlineKeyboardButton(text=f"{ended} #{rid}", callback_data="noop")])
        elif any(item.startswith("prop:all:") for item in data) and all(statuses.get(p) != "pending" for p in proposals):
            continue  # nothing left on the page to approve
        else:
            keyboard.append(row)
    if not rows:  # the tapped message's keyboard is unknown: show the outcome alone
        keyboard = [[InlineKeyboardButton(text=f"{label}{suffix}", callback_data="noop")]]
    try:
        await app.bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
    except TelegramBadRequest:  # too old to edit, or already showing it; the toast still told the owner
        log.info("could not mark the handled button in chat %s", message.chat.id)


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
    result = await owner.override_chat(app, chat_id, _OVERRIDES[action], _person(query.from_user.full_name))
    await _toast(app, query, result.message if result else "unknown chat")
    if result is not None:
        await _refresh_status(app, query)


async def _handle_leave(app: App, query: CallbackQuery, action: str, raw_chat_id: str) -> None:
    """The Leave buttons and `status:refresh` (the confirmation's Cancel). The owner confirms before Dom leaves."""
    if app.owner_id is None or query.from_user.id != app.owner_id:
        await _toast(app, query, "not allowed")
        return
    who = _person(query.from_user.full_name)
    if action == "status":
        await _toast(app, query)
        if raw_chat_id == "refresh":
            await owner.record_event(app, f"{who} refreshed the /status list")
            await _refresh_status(app, query)
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        await _toast(app, query, "unknown chat")
        return
    if action == "leaveyes":
        result, left = await owner.leave_chat(app, chat_id, who)
        await _toast(app, query, result)
        if left:
            await _refresh_status(app, query)
        return
    chat = await owner.leavable_chat(app, chat_id)
    if chat is None:
        await _toast(app, query, "unknown chat")
        return
    title = owner.display_title(chat, app.owner_id)
    await _toast(app, query)
    await owner.record_event(app, f"{who} tapped Leave on {title} (asked to confirm)")
    await _replace_with_prompt(app, query, f"Leave {title}? Friends will see Dom leave.", InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes, leave", callback_data=f"leaveyes:{chat['chat_id']}"),
        InlineKeyboardButton(text="Cancel", callback_data="status:refresh"),
    ]]))


async def _handle_preset(app: App, query: CallbackQuery, action: str, data: str) -> None:
    owner_id = app.owner_id
    if (
        owner_id is None or query.from_user.is_bot or query.from_user.id != owner_id
        or query.message is None or query.message.chat.id != owner_id or query.message.chat.type != "private"
    ):
        await _toast(app, query, "not allowed")
        return
    raw_id, _, name = data.partition(":")
    if raw_id == "all":
        if action == "preset":
            await _toast(app, query)
            await owner.choose_preset(app, None)
            return
        text = await owner.set_global_preset(app, name, _person(query.from_user.full_name))
        await _toast(app, query, None if text else "preset unavailable")
        if text:
            await app.bot.send_message(owner_id, text)
        return
    chat = await owner.approved_chat(app, raw_id)
    if chat is None:
        await _toast(app, query, "unknown chat")
        return
    if action == "preset":
        await _toast(app, query)
        await owner.choose_preset(app, chat)
        return

    text = await owner.set_preset(app, chat, name)
    if text is None:
        await _toast(app, query, "preset or chat unavailable")
        return
    await _toast(app, query)
    await app.bot.send_message(owner_id, text)


def _item_id(raw: str) -> int | None:
    if raw.isascii() and raw.isdecimal() and len(raw) <= 19:
        number = int(raw)
        if 0 < number < 2**63:
            return number
    return None


async def _handle_proposal(app: App, query: CallbackQuery, data: str) -> None:
    action, _, payload = data.partition(":")
    if action == "all":
        first, _, last = payload.partition(":")
        first_id, last_id = _item_id(first), _item_id(last)
        if first_id is not None and last_id is not None:
            await _toast(app, query, await owner.approve_proposal_page(app, first_id, last_id))
        return
    proposal_id = _item_id(payload)
    if proposal_id is None:
        return
    if action in ("yes", "no"):
        outcome = await app.proposals.decide(proposal_id, action == "yes")
        await _toast(app, query, outcome)
        await _settle_button(app, query, outcome)
    elif action == "page":
        await _toast(app, query)
        await owner.show_proposals(app, proposal_id)
        await owner.record_event(app, "Paged through pending proposals")
    elif action == "show":
        row = next((row for row in await app.proposals.pending() if row["id"] == proposal_id), None)
        await _toast(app, query, None if row else "already handled")
        if row:
            await app.proposals.send(row)
            await owner.record_event(app, f"Viewed proposal #{proposal_id}")


async def handle_callback(app: App, query: CallbackQuery) -> None:
    action, _, raw_chat_id = (query.data or "").partition(":")
    if action == "noop":  # a button that only shows how something was settled
        await _toast(app, query)
        return
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
    if action in {"t3send", "t3cancel", "t3reconnect"}:
        if (app.t3 is not None and not query.from_user.is_bot and query.message is not None
                and query.message.chat.id == owner_id and query.message.chat.type == "private"):
            await _toast(app, query)
            await app.t3.callback(owner_id, action, raw_chat_id)
        return
    if action == "desktop":
        if (
            app.desktop_worker is None or query.from_user.is_bot or query.message is None
            or query.message.chat.id != owner_id or query.message.chat.type != "private"
        ):
            return
        decision, _, payload = raw_chat_id.partition(":")
        raw_id, _, raw_steps = payload.partition(":")
        task_id = _item_id(raw_id)
        if task_id is not None:
            if decision == "cancel" and not raw_steps:
                changed = await app.desktop_worker.cancel(owner_id, task_id)
                await _toast(app, query, "Cancelled" if changed else "already handled")
            elif decision == "done" and (steps := _item_id(raw_steps)) is not None:
                changed = await app.desktop_worker.resume(owner_id, task_id, steps)
                await _toast(app, query, "Resumed" if changed else "already handled")
        return
    if action == "own":
        decision, _, raw_id = raw_chat_id.partition(":")
        if (
            not query.from_user.is_bot and query.message is not None and query.message.chat.id == owner_id
            and query.message.chat.type == "private" and decision == "cancel" and (item_id := _item_id(raw_id)) is not None
        ):
            changed = await app.self_work.cancel(item_id)
            await _toast(app, query, "Cancelled" if changed else "already handled")
        return
    if action in ("prop", "plugin"):
        if (
            query.from_user.is_bot or query.message is None or query.message.chat.id != owner_id
            or query.message.chat.type != "private"
        ):
            return
        if action == "prop":
            await _handle_proposal(app, query, raw_chat_id)
        elif raw_chat_id.startswith("disable:"):
            await _toast(app, query, await owner.disable_plugin(app, raw_chat_id.removeprefix("disable:")))
        return
    if action == "update":
        if (
            query.from_user.is_bot or query.message is None or query.message.chat.id != owner_id
            or query.message.chat.type != "private"
        ):
            return
        decision, _, nonce = raw_chat_id.partition(":")
        if decision in ("go", "no"):
            await _toast(app, query, await owner.decide_update(app, decision, nonce))
        return
    if action == "rem":
        decision, _, raw_id = raw_chat_id.partition(":")
        if not query.from_user.is_bot and decision == "cancel" and (item_id := _item_id(raw_id)) is not None:
            changed = await app.dispatch.reminders.cancel(item_id)
            outcome = "Cancelled" if changed else "already handled"
            await _toast(app, query, outcome)
            await _settle_button(app, query, outcome, f" #{item_id}")
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return
    if action not in owner.PENDING_DECISIONS:
        return
    chat, label = await owner.decide_pending_chat(app, chat_id, action, _person(query.from_user.full_name))
    await app.bot.answer_callback_query(query.id, text=label)
    if chat is not None and query.message is not None:
        await app.bot.edit_message_text(
            f"{label}: {chat['title'] or chat_id}", chat_id=query.message.chat.id, message_id=query.message.message_id
        )


async def _note_removed(app: App, update: ChatMemberUpdated, owner_id: int) -> None:
    """Dom is no longer in a group it knew: stop listening to it and tell the owner once."""
    chat = next((c for c in await app.store.list_chats() if c["chat_id"] == str(update.chat.id)), None)
    if chat is None or chat["authorization_status"] not in owner.LEAVABLE:  # unknown, already left, or blocked
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

    # Every button goes to handle_callback, which checks the sender and ignores unknown data.
    # A prefix list here once dropped /preset and plugin buttons without any error.
    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await handle_callback(app, query)

    @router.my_chat_member()
    async def on_my_chat_member(update: ChatMemberUpdated) -> None:
        await handle_my_chat_member(app, update)

    return router

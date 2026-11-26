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
from typing import Any, Awaitable, Callable, Collection
from zoneinfo import ZoneInfo

import httpx
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from dom.claim import try_claim
from dom.config import MODEL_ROLES, Config, load_config
from dom.deletion import STALE_MESSAGE, hash_matches, stamp_matches, stamp_token
from dom.dispatch import Dispatcher
from dom.desktop import files as desktop_files
from dom.desktop.fleet import DesktopFleet
from dom.embedding import EmbeddingRouter, routed_embedding_client
from dom.t3.tools import Bridge
from dom.home import Home
from dom.jev import JevClient
from dom.knowledge import KnowledgeIndex
from dom.llm import ChatClient, FallbackClient, routed_client
from dom.media import MediaDescriber
from dom.memory_import import MemoryImport
from dom.models import IncomingMessage, StoredMessage
from dom.presence import Presence
from dom.proposals import Proposals
from dom.plugins.registry import PluginRegistry
from dom.plugins import surfaces
from dom.plugins.runner import PluginRunner
from dom.gifs import GifSearch
from dom.goals import Goals
from dom.noticings import Noticings
from dom.studio.client import StudioClient
from dom.studio.cloud import CloudStudio
from dom.studio.policy import StudioQuota
from dom.stickers import Stickers
from dom.store import Store
from dom.search import SearchClient, SearchRouter, routed_search_client
from dom.selfwork import SelfWork
from dom.telegram import gate, owner
from dom.telegram.convert import to_incoming
from dom.telegram.executor import edit_text, retry_telegram, send_text

CHAT_TYPES = ("private", "group", "supergroup")

log = logging.getLogger("dom.handlers")


@dataclass
class App:
    config: Config
    home: Home
    store: Store
    client: ChatClient | FallbackClient
    bot: Any
    bot_username: str
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    locks: defaultdict = field(default_factory=lambda: defaultdict(asyncio.Lock))  # one turn at a time per chat
    ingest_locks: defaultdict = field(default_factory=lambda: defaultdict(asyncio.Lock))  # decide + record, in order

    jev: JevClient | None = None
    bot_id: int | None = None
    media_describer: MediaDescriber | None = None
    search_client: SearchClient | SearchRouter | None = None
    gifs: GifSearch | None = None
    studio: StudioClient | None = None
    studio_http: httpx.AsyncClient | None = None  # the studio's client, which ignores proxy settings (cli.py)
    cloud_studio: CloudStudio | None = None
    studio_quota: StudioQuota = field(init=False)
    draft_client: ChatClient | FallbackClient | None = None
    escalation_client: FallbackClient | None = None
    embedding_client: EmbeddingRouter | None = None
    knowledge: KnowledgeIndex = field(init=False)
    desktop_worker: DesktopFleet | None = field(init=False, default=None)  # every desktop container's worker
    t3: Bridge | None = field(init=False, default=None)
    dispatch: Dispatcher = field(init=False)
    proposals: Proposals = field(init=False)
    stickers: Stickers = field(init=False)
    memory_import: MemoryImport = field(init=False)
    plugins: PluginRegistry = field(init=False)
    plugin_runner: PluginRunner = field(init=False)
    self_work: SelfWork = field(init=False)
    goals: Goals = field(init=False)
    noticings: Noticings = field(init=False)
    plugin_command_times: dict[int, float] = field(default_factory=dict)  # per-chat cooldown outside the owner DM
    menu_dirty: bool = False  # True if set_my_commands failed and must be retried on the next background tick

    def __post_init__(self) -> None:
        def now() -> datetime:
            return datetime.now(timezone.utc)

        self.studio_quota = StudioQuota(self.store)
        self.plugin_runner = PluginRunner(home=self.home.root, store=self.store)
        self.plugins = PluginRegistry(self.home, self.store, on_change=lambda: owner.refresh_menu(self), runner=self.plugin_runner)
        self.proposals = Proposals(self)
        self.stickers = Stickers(
            self.home,
            self.bot,
            lambda: self.dispatch.now(),
            describer=lambda: self.media_describer,
            spawn=lambda coro: self.dispatch._spawn(coro),
            request_context=lambda: self.dispatch.sticker_media_request(),
        )
        self.memory_import = MemoryImport(self)
        presence = Presence(self.store, ZoneInfo(self.config.timezone), Random(), now)
        if self.config.t3.enabled:
            self.t3 = Bridge(self)
        self.dispatch = Dispatcher(self, presence, self.jev, self.sleep, now)
        async def embedding_allowed() -> bool:
            return not await self.dispatch.usage.over_cap(self.config, self.bot, sleep=self.sleep)
        self.knowledge = KnowledgeIndex(
            self.store, self.home, self.embedding_client, allowed=embedding_allowed, chat_id=self.owner_id,
        )
        self.self_work = SelfWork(self)
        self.goals = Goals(self)
        self.noticings = Noticings(self)
        if self.config.desktop_hosts:
            self.desktop_worker = DesktopFleet(self)
        if self.cloud_studio is not None and self.cloud_studio.running_chat_keys is None:
            self.cloud_studio.running_chat_keys = self.running_chat_keys

    def running_chat_keys(self) -> frozenset[str]:
        """Every API key currently held in memory by running chat, triage, media, drafting, escalation or search
        clients, so a media key rotated to a running chat client's old value without a restart is still refused."""
        keys: set[str] = set()
        for holder in (
            self.client,
            getattr(self.media_describer, "client", None),
            self.draft_client,
            self.escalation_client,
            self.jev,
            self.search_client,
        ):
            if holder is None:
                continue
            children = getattr(holder, "clients", None)
            for item in children if children is not None else (holder,):
                key = getattr(item, "api_key", None)
                if isinstance(key, str) and key:
                    keys.add(key)
        return frozenset(keys)

    @property
    def owner_id(self) -> int | None:
        return self.store.owner_id

    def model_routes(self, saved: Config, studio_chains: Collection[str] = ()) -> ModelRoutes:
        """Build every client for `saved`'s routes (and triage's decisions client) without touching the running ones,
        so a route that can't be built raises before anything is published."""
        routed = (*MODEL_ROLES, "triage")
        models = {name: endpoint for name, endpoint in self.config.models.items() if name not in routed}
        models.update({name: saved.models[name] for name in routed if name in saved.models})
        # A provider save can move studio backups to a new key variable: only those chains are taken from `saved`, so
        # other studio settings on disk wait for their own save. Backups are read from the config on each use.
        studio = self.config.studio.model_copy(
            update={f"{chain}_fallbacks": getattr(saved.studio, f"{chain}_fallbacks") for chain in studio_chains})
        config = self.config.model_copy(update={"models": models, "provider_setups": saved.provider_setups,
                                                "studio": studio})
        # model_copy skips validation, and this mix of saved routes and running studio settings was never validated
        # as a whole: re-check the key rules so a chat route and a live studio backup can't end up sharing a key.
        config.check_key_rules()
        current = self.client.primary if isinstance(self.client, FallbackClient) else self.client
        chat, media, draft, escalation = (routed_client(config, role, current.http, current.usage)
                                          for role in ("chat", "media", "drafting", "escalation"))
        search = routed_search_client(config, current.http, current.usage)
        embedding = routed_embedding_client(config, current.http, current.usage)
        return ModelRoutes(config, chat, media, draft, escalation, search, embedding, self._triage_client(config))

    def use_model_routes(self, routes: ModelRoutes) -> None:
        """Swap in routes built by model_routes for new calls. In-flight calls keep theirs."""
        if isinstance(self.client, FallbackClient):
            routes.chat.last_model = self.client.last_model
            routes.chat.last_fallback_at = self.client.last_fallback_at
        if routes.media.available:
            if self.media_describer is None:
                self.media_describer = MediaDescriber(routes.media, self.bot)
            else:
                self.media_describer.client = routes.media
        else:
            self.media_describer = None
        self.jev = self.dispatch.jev = routes.jev
        self.search_client = routes.search
        if self.knowledge.stage(routes.embedding):
            log.info("embedding model staged; confirm reindex before it serves searches")
        self.config, self.client, self.draft_client = routes.config, routes.chat, routes.draft
        self.escalation_client = routes.escalation

    def apply_model_routes(self, saved: Config) -> None:
        self.use_model_routes(self.model_routes(saved))

    def refresh_triage(self) -> None:
        """Pick up a triage key added to .env since the decisions client was built (or one that was removed)."""
        self.jev = self.dispatch.jev = self._triage_client(self.config)

    def _triage_client(self, config: Config) -> JevClient | None:
        """Triage's decisions client for `config`: the running one while its endpoint and key value are unchanged.
        Without a key value triage, and the leak checks that use it, stay off, as at startup."""
        triage = config.models["triage"]
        key = config.secret(triage.api_key_env) if triage.api_key_env else None
        if self.jev is not None and triage == self.config.models.get("triage") and key == self.jev.api_key:
            return self.jev
        current = self.client.primary if isinstance(self.client, FallbackClient) else self.client
        return JevClient(current.http, key, triage.model, triage.base_url, usage=current.usage) if key else None


@dataclass(frozen=True)
class ModelRoutes:
    config: Config
    chat: FallbackClient
    media: FallbackClient
    draft: FallbackClient
    escalation: FallbackClient
    search: SearchRouter | None
    embedding: EmbeddingRouter | None
    jev: JevClient | None


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


async def _record(app: App, msg: IncomingMessage, *, describe_media: bool = False, notify: bool = False) -> None:
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
            thread_id=msg.thread_id,
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
    if notify:
        app.dispatch.notify(msg)
    await app.store.set_chat_title(msg.chat_id, msg.chat_title)
    if notify and msg.mentions_owner:
        await app.dispatch.cross_chat.alert(msg)


def _claim_code(text: str, username: str) -> str | None:
    """The code in `/claim <code>` (or `/claim@bot <code>`), or None if this is not a claim."""
    parts = text.split()
    if not parts:
        return None
    command, _, target = parts[0].partition("@")
    if command != "/claim" or (target and target.lower() != username.lower()):
        return None
    return parts[1] if len(parts) > 1 else ""


async def _send(app: App, chat_id: int, text: str, **kw: Any) -> Any:
    return await send_text(app.bot, chat_id, text, sleep=app.sleep, **kw)


async def _handle_claim(app: App, msg: IncomingMessage, code: str) -> None:
    if try_claim(app.config.home, app.config, msg.sender_id, msg.chat_type, code):
        await app.store.set_owner(msg.sender_id)
        await app.store.resolve_person(msg.sender_id, msg.sender_handle, msg.sender_name)
        app.config = load_config(app.config.home)
        await _send(app, msg.chat_id, "Done: you are my owner now. Other chats will ask you before I talk in them.")
        await owner.refresh_menu(app)
    elif msg.chat_type == "private":
        await _send(app, msg.chat_id, "That code did not work.")


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


DOCUMENT_TIMEOUT = 60  # seconds a /desktop document may take to download while the owner DM's ingest waits


async def _receive_desktop_document(app: App, message: Message, msg: IncomingMessage) -> IncomingMessage:
    document = message.document
    if document is None or msg.media is None:
        return msg
    try:
        if (document.file_size or 0) > desktop_files.MAX_FILE_BYTES:
            raise ValueError("Desktop documents can be at most 20 MB.")
        with desktop_files.BoundedFile() as content:
            async with asyncio.timeout(DOCUMENT_TIMEOUT):
                downloaded = await app.bot.download(document, destination=content)
            if downloaded is None:
                raise OSError("no document returned")
            name = desktop_files.stage_inbox(app.home.root, document.file_name, content.getvalue())
    except ValueError as error:
        await _send(app, msg.chat_id, str(error))
        return msg
    except Exception as error:
        log.warning("could not stage a desktop document (%s)", type(error).__name__)
        await _send(app, msg.chat_id, "Couldn't save the document for the desktop.")
        return msg
    received = f"file received: inbox/{name}"
    await owner.record_event(app, received)
    return msg.model_copy(update={"media": {**msg.media, "desktop_inbox": name}})


async def _handle_migration(app: App, old_chat_id: int, new_chat_id: int, title: str) -> None:
    """Handle Telegram's group -> supergroup migration service messages via the shared migration helper."""
    await app.dispatch.migrate_chat(old_chat_id, new_chat_id, title=title)


async def handle_message(app: App, message: Message) -> None:
    if message.chat.type not in CHAT_TYPES:
        return
    if message.migrate_to_chat_id is not None:
        await _handle_migration(app, message.chat.id, message.migrate_to_chat_id, message.chat.title or "")
        return
    if message.migrate_from_chat_id is not None:
        await _handle_migration(app, message.migrate_from_chat_id, message.chat.id, message.chat.title or "")
        return
    msg = to_incoming(message)
    if _is_own_message(app, msg):
        return
    # Any message from a chat proves it is reachable again, including commands handled before the gate.
    await app.store.set_chat_reachable(msg.chat_id, True)
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
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "delete":
                if error := await _confirm_delete_plugin(app, args[1]):
                    await _send(app, msg.chat_id, error)
            else:
                await owner.show_plugins(app)
                await owner.record_event(app, f"{_person(msg.sender_name)} ran /plugins")
        return
    if command == "/studio":
        if owner_dm:
            from dom.studio.profile import status_text

            await _send(app, msg.chat_id, await status_text(app))
        return
    if command == "/skills":
        if owner_dm:
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "retire":
                _, reply = await owner.retire_skill(app, args[1])
                await _send(app, msg.chat_id, reply)
            elif len(args) == 2 and args[0].lower() == "restore":
                _, reply = await owner.restore_skill(app, args[1])
                await _send(app, msg.chat_id, reply)
            elif len(args) == 2 and args[0].lower() == "delete":
                if error := await _confirm_delete_skill(app, args[1]):
                    await _send(app, msg.chat_id, error)
            else:
                await owner.show_skills(app)
                await owner.record_event(app, f"{_person(msg.sender_name)} ran /skills")
        return
    if command == "/ownwork":
        if owner_dm:
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "delete" and (task_id := _item_id(args[1])) is not None:
                if error := await _confirm_delete_own_task(app, task_id):
                    await _send(app, msg.chat_id, error)
            else:
                await owner.show_own_work(app)
                await owner.record_event(app, f"{_person(msg.sender_name)} ran /ownwork")
        return
    if command == "/t3":
        if owner_dm:
            await owner.show_t3(app)
        return
    if command == "/tasks":
        if owner_dm:
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "delete" and (task_id := _item_id(args[1])) is not None:
                if error := await _confirm_delete_desktop_task(app, task_id):
                    await _send(app, msg.chat_id, error)
            else:
                await owner.show_tasks(app)
                await owner.record_event(app, f"{_person(msg.sender_name)} ran /tasks")
        return
    if command == "/export":
        if owner_dm and (error := await owner.export_memory(app)):
            await _send(app, msg.chat_id, error)
        return
    if command == "/import":
        if owner_dm:
            response, event, code = await owner.import_memory(app, message, msg.text)
            await owner.record_event(app, event)
            await _send(app, msg.chat_id, response)
            if code:  # its own message as one code block: one tap copies it with its line breaks
                await _send(app, msg.chat_id, code, pre=True)
        return
    if owner_dm:
        who = _person(msg.sender_name)
        if command == "/pending":
            await gate.resend_pending(app.bot, app.store, app.owner_id, sleep=app.sleep)
            await owner.record_event(app, f"{who} ran /pending: {await owner.pending_summary(app)}")
            return
        if command == "/status":
            await owner.show_view(app, msg.chat_id, await owner.render_status(app))
            await owner.record_event(app, f"{who} ran /status: {await owner.status_summary(app)}")
            return
        if command == "/chats":
            await owner.show_view(app, msg.chat_id, await owner.render_chats(app))
            await owner.record_event(app, f"{who} ran /chats: {await owner.chats_summary(app)}")
            return
        if command == "/stickers":
            await owner.edit_stickers(app, msg.text.split()[1:])
            return
        if command == "/reminders":
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "delete" and (item_id := _item_id(args[1])) is not None:
                if error := await _confirm_delete_reminder(app, item_id):
                    await _send(app, msg.chat_id, error)
            elif len(args) == 1 and args[0].lower() == "all":
                await owner.show_reminders(app, include_settled=True)
                await owner.record_event(app, f"{who} ran /reminders all")
            else:
                await owner.show_reminders(app)
                await owner.record_event(app, f"{who} ran /reminders")
            return
        if command == "/proposals":
            args = msg.text.split()[1:]
            if len(args) == 2 and args[0].lower() == "delete" and (item_id := _item_id(args[1])) is not None:
                if error := await _confirm_delete_proposal(app, item_id):
                    await _send(app, msg.chat_id, error)
            elif len(args) == 1 and args[0].lower() == "settled":
                await owner.show_settled_proposals(app)
                await owner.record_event(app, f"{who} ran /proposals settled")
            else:
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
                    await _send(app, msg.chat_id, "unknown chat")
                elif result.spend_paused:
                    await _send(app, msg.chat_id, result.message)
                elif len(parts) == 1:
                    await _send(app, msg.chat_id, "Dom is up." if command == "/wake" else "Dom is asleep.")
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
            # Under the ingest lock on purpose: the owner's next message must not be recorded, answered or marked
            # read before this one. The download is bounded by DOCUMENT_TIMEOUT so the wait stays short.
            msg = await _receive_desktop_document(app, message, msg)
        if decision == "process" and app.owner_id is not None and msg.chat_type != "private" and not msg.is_bot:
            entities = message.entities or message.caption_entities or ()
            if message.reply_to_message or any(e.type in ("mention", "text_mention") for e in entities):
                handle = await app.store.person_handle(app.owner_id) if any(e.type == "mention" for e in entities) else ""
                msg = to_incoming(message, owner_id=app.owner_id, owner_handle=handle)
        await _record(app, msg, describe_media=decision == "process", notify=decision == "process")
    if decision == "hold":
        kind = "chat" if msg.chat_type == "private" else "group"
        await gate.request_approval(
            app.bot, app.store, app.owner_id, msg.chat_id,
            msg.chat_title or msg.sender_name, kind, msg.sender_handle or msg.sender_name, msg.text,
            sleep=app.sleep,
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


TOAST_MAX_WAIT = 3.0  # a callback query answer is worthless once Telegram has made the tap time out


async def _toast(app: App, query: CallbackQuery, text: str | None = None) -> None:
    """Best-effort: the state change and the button edit that follow matter more than the acknowledgement, so no
    failure here (expired callback, flood wait, network) is allowed to abort the handler."""
    try:
        try:
            await app.bot.answer_callback_query(query.id, text=text)
        except TelegramRetryAfter as exc:
            if exc.retry_after > TOAST_MAX_WAIT:
                raise
            await app.sleep(exc.retry_after)
            await app.bot.answer_callback_query(query.id, text=text)
    except Exception as error:  # a turn can outlast the callback's validity
        log.warning("could not answer callback %s (%s)", query.id, type(error).__name__)


SETTLED = {"Expired": "Expired", "Approved": "✅ Approved", "Rejected": "❌ Rejected", "Cancelled": "Cancelled", "already handled": "Already handled"}
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
        await retry_telegram(
            lambda: app.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=message.message_id,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            ),
            sleep=app.sleep,
        )
    except TelegramBadRequest:  # too old to edit, or already showing it; the toast still told the owner
        log.info("could not mark the handled button in chat %s", message.chat.id)


async def _clear_buttons(app: App, query: CallbackQuery) -> None:
    """Remove a card's inline keyboard once closed, used or stale."""
    if query.message is None:
        return
    try:
        await retry_telegram(
            lambda: app.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None,
            ),
            sleep=app.sleep,
        )
    except (TelegramBadRequest, TelegramRetryAfter):
        log.info("could not clear card buttons in chat %s", query.message.chat.id)


async def _close_card(app: App, query: CallbackQuery) -> None:
    """Close removes the whole card. Telegram refuses to delete messages older than 48 hours, so then only its buttons go."""
    if query.message is None:
        return
    await app.store.forget_menu_card(query.message.chat.id, query.message.message_id)  # nothing left to expire
    try:
        await retry_telegram(
            lambda: app.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id),
            sleep=app.sleep,
        )
    except (TelegramBadRequest, TelegramRetryAfter):
        await _clear_buttons(app, query)


async def _replace_with_prompt(app: App, query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Rich messages cannot be edited, so the owner's old /status message is deleted and the new one sent after it."""
    if query.message is None:
        return
    chat_id = query.message.chat.id
    await _send(app, chat_id, text, reply_markup=markup)
    await owner.delete_message(app.bot, chat_id, query.message.message_id, sleep=app.sleep)


async def _refresh_status(app: App, query: CallbackQuery) -> None:
    if query.message is None:
        return
    chat_id = query.message.chat.id
    await owner.show_view(app, chat_id, await owner.render_status(app))
    await owner.delete_message(app.bot, chat_id, query.message.message_id, sleep=app.sleep)


async def _handle_override(app: App, query: CallbackQuery, action: str, raw_chat_id: str) -> None:
    """The Wake / Sleep buttons under /status: the owner's /wake or /sleep, then refresh that message."""
    if app.owner_id is None or query.from_user.id != app.owner_id:
        await _toast(app, query, "not allowed")
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        await _toast(app, query, "unknown chat")
        await _clear_buttons(app, query)
        return
    result = await owner.override_chat(app, chat_id, _OVERRIDES[action], _person(query.from_user.full_name))
    await _toast(app, query, result.message if result else "unknown chat")
    if result is not None:
        await _refresh_status(app, query)
    else:
        await _clear_buttons(app, query)


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
        await _clear_buttons(app, query)
        return
    if action == "leaveyes":
        result, left = await owner.leave_chat(app, chat_id, who)
        await _toast(app, query, result)
        if left:
            await _refresh_status(app, query)
        else:
            await _clear_buttons(app, query)
        return
    chat = await owner.leavable_chat(app, chat_id)
    if chat is None:
        await _toast(app, query, "unknown chat")
        await _clear_buttons(app, query)
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
            await _clear_buttons(app, query)
            await owner.choose_preset(app, None)
            return
        text = await owner.set_global_preset(app, name, _person(query.from_user.full_name))
        await _toast(app, query, None if text else "preset unavailable")
        await _clear_buttons(app, query)
        if text:
            await _send(app, owner_id, text)
        return
    chat = await owner.approved_chat(app, raw_id)
    if chat is None:
        await _toast(app, query, "unknown chat")
        await _clear_buttons(app, query)
        return
    if action == "preset":
        await _toast(app, query)
        await _clear_buttons(app, query)
        await owner.choose_preset(app, chat)
        return

    text = await owner.set_preset(app, chat, name)
    if text is None:
        await _toast(app, query, "preset or chat unavailable")
        await _clear_buttons(app, query)
        return
    await _toast(app, query)
    await _clear_buttons(app, query)
    await _send(app, owner_id, text)


def _item_id(raw: str) -> int | None:
    if raw.isascii() and raw.isdecimal() and len(raw) <= 19:
        number = int(raw)
        if 0 < number < 2**63:
            return number
    return None


async def _settle_confirm(app: App, query: CallbackQuery, label: str) -> None:
    if query.message is None:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])
    try:
        await retry_telegram(
            lambda: app.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id, message_id=query.message.message_id,
                reply_markup=markup,
            ),
            sleep=app.sleep,
        )
    except (TelegramBadRequest, TelegramRetryAfter):  # the delete is done either way; a second tap answers "stale"
        log.info("could not settle confirmation buttons in chat %s", query.message.chat.id)


async def _confirm_delete_plugin(app: App, name: str, expected_hash: str | None = None) -> str | None:
    from dom.plugins.manifest import validate_name

    try:
        validate_name(name)
    except ValueError:
        return "Unknown plugin"
    row = next((r for r in await app.plugins.listing() if r["name"] == name), None)
    if row is None:
        return "Unknown plugin"
    if expected_hash is not None and not hash_matches(row["hash"], expected_hash):
        return STALE_MESSAGE
    digest = row["hash"][:16]
    await owner.send_delete_confirm(
        app,
        f"Delete plugin {name}? Its files and database state are moved to the deletion archive before removal.",
        f"plugin:delyes:{name}:{digest}", f"plugin:delno:{name}",
    )
    return None


async def _confirm_delete_skill(app: App, name: str, expected_hash: str | None = None) -> str | None:
    from dom import skills

    try:
        skills._validate_name(name)
        live_hash = await asyncio.to_thread(skills.skill_hash, app.home, name)
    except (ValueError, FileNotFoundError):
        return f"Skill {name} not found."
    if expected_hash is not None and not hash_matches(live_hash, expected_hash):
        return STALE_MESSAGE
    yes_data = await owner.skill_callback(app, "delyes", name, live_hash)
    no_data = await owner.skill_callback(app, "delno", name)
    await owner.send_delete_confirm(
        app,
        f"Delete skill {name}? Its files are moved to the deletion archive before removal.",
        yes_data, no_data,
    )
    return None


async def _confirm_delete_reminder(app: App, reminder_id: int, expected_stamp: str | None = None) -> str | None:
    row = await app.store.get_reminder(app.owner_id, reminder_id)
    if row is None:
        return f"Reminder #{reminder_id} not found."
    if expected_stamp is not None and not stamp_matches(row["created_at"], expected_stamp):
        return STALE_MESSAGE
    token = stamp_token(row["created_at"])
    await owner.send_delete_confirm(
        app, f"Delete reminder #{reminder_id}? A copy is saved to the deletion archive.",
        f"rem:delyes:{reminder_id}:{token}", f"rem:delno:{reminder_id}",
    )
    return None


async def _confirm_delete_desktop_task(app: App, task_id: int, expected_stamp: str | None = None) -> str | None:
    row = await app.store.get_desktop_task(app.owner_id, task_id) if app.owner_id is not None else None
    if row is None:
        return f"Desktop task #{task_id} not found."
    if row["status"] in ("queued", "running", "waiting_owner"):
        return f"Cannot delete desktop task #{task_id} while it is {row['status'].replace('_', ' ')}."
    if expected_stamp is not None and not stamp_matches(row["created_at"], expected_stamp):
        return STALE_MESSAGE
    token = stamp_token(row["created_at"])
    await owner.send_delete_confirm(
        app, f"Delete desktop task #{task_id}? Its record and attachments are moved to the deletion archive.",
        f"desktop:delyes:{task_id}:{token}", f"desktop:delno:{task_id}",
    )
    return None


async def _confirm_delete_own_task(app: App, task_id: int, expected_stamp: str | None = None) -> str | None:
    row = await app.store.get_self_task(task_id)
    if row is None:
        return f"Task #{task_id} not found."
    if row["status"] in ("planned", "running"):
        return f"Cannot delete task #{task_id} while it is {row['status']}."
    if expected_stamp is not None and not stamp_matches(row["announced_at"], expected_stamp):
        return STALE_MESSAGE
    token = stamp_token(row["announced_at"])
    await owner.send_delete_confirm(
        app, f"Delete self-directed task #{task_id} and its screenshots? They are moved to the deletion archive.",
        f"own:delyes:{task_id}:{token}", f"own:delno:{task_id}",
    )
    return None


async def _confirm_delete_proposal(app: App, proposal_id: int, expected_stamp: str | None = None) -> str | None:
    row = await app.store.get_proposal(proposal_id)
    if row is None:
        return f"Proposal #{proposal_id} not found."
    if row["status"] == "pending":
        return f"Proposal #{proposal_id} is still pending. Approve or reject it before deleting."
    if expected_stamp is not None and not stamp_matches(row["created_at"], expected_stamp):
        return STALE_MESSAGE
    token = stamp_token(row["created_at"])
    await owner.send_delete_confirm(
        app, f"Delete settled proposal #{proposal_id}? A copy is saved to the deletion archive.",
        f"prop:delyes:{proposal_id}:{token}", f"prop:delno:{proposal_id}",
    )
    return None


async def _handle_proposal(app: App, query: CallbackQuery, data: str) -> None:
    action, _, payload = data.partition(":")
    if action == "all":
        first, _, last = payload.partition(":")
        first_id, last_id = _item_id(first), _item_id(last)
        if first_id is not None and last_id is not None:
            outcome = await owner.approve_proposal_page(app, first_id, last_id)
            await _toast(app, query, outcome)
            await _settle_button(app, query, "already handled")
        return
    raw_id, _, token = payload.partition(":")
    proposal_id = _item_id(raw_id)
    if proposal_id is None:
        return
    if action in ("yes", "no") and not token:
        outcome = await app.proposals.decide(proposal_id, action == "yes")
        await _toast(app, query, outcome)
        await _settle_button(app, query, outcome)
    elif action == "page" and not token:
        await _toast(app, query)
        await _clear_buttons(app, query)
        await owner.show_proposals(app, proposal_id)
        await owner.record_event(app, "Paged through pending proposals")
    elif action == "show" and not token:
        row = next((row for row in await app.proposals.pending() if row["id"] == proposal_id), None)
        await _toast(app, query, None if row else "already handled")
        if row:
            await app.proposals.send(row)
            await owner.record_event(app, f"Viewed proposal #{proposal_id}")
        else:
            await _settle_button(app, query, "already handled")
    elif action == "del":
        error = await _confirm_delete_proposal(app, proposal_id, token or None)
        await _toast(app, query, error)
    elif action == "delyes":
        ok, message = await app.proposals.delete(proposal_id, token)
        await _toast(app, query, message)
        if ok:
            await _settle_confirm(app, query, f"Deleted #{proposal_id}")
    elif action == "delno":
        await _toast(app, query, "Cancelled")
        await _settle_confirm(app, query, "Cancelled")


async def handle_callback(app: App, query: CallbackQuery) -> None:
    action, _, raw_chat_id = (query.data or "").partition(":")
    if action == "noop":  # a button that only shows how something was settled
        await _toast(app, query)
        return
    if action == "card" and raw_chat_id == "close":
        if app.owner_id is None or query.from_user.is_bot or query.from_user.id != app.owner_id:
            await _toast(app, query, "not allowed")
            return
        await _toast(app, query)
        await _close_card(app, query)
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
    if action == "escalation":
        if (not query.from_user.is_bot and query.message is not None
                and query.message.chat.id == owner_id and query.message.chat.type == "private"):
            decision, _, token = raw_chat_id.partition(":")
            if decision in ("switch", "keep", "retry", "dismiss"):
                outcome = app.dispatch.escalation.choose(token, decision, owner_id, query.message.message_id)
                await _toast(app, query, outcome)
        return
    if action in {"t3send", "t3cancel", "t3reconnect"}:
        if (app.t3 is not None and not query.from_user.is_bot and query.message is not None
                and query.message.chat.id == owner_id and query.message.chat.type == "private"):
            await _toast(app, query)
            await app.t3.callback(owner_id, action, raw_chat_id)
        return
    if action == "desktop":
        if (
            query.from_user.is_bot or query.message is None
            or query.message.chat.id != owner_id or query.message.chat.type != "private"
        ):
            return
        decision, _, payload = raw_chat_id.partition(":")
        raw_id, _, rest = payload.partition(":")
        task_id = _item_id(raw_id)
        if task_id is not None:
            if decision == "cancel" and not rest and app.desktop_worker is not None:
                changed = await app.desktop_worker.cancel(owner_id, task_id)
                await _toast(app, query, "Cancelled" if changed else "already handled")
                await _clear_buttons(app, query)
            elif decision == "done" and app.desktop_worker is not None and (steps := _item_id(rest)) is not None:
                changed = await app.desktop_worker.resume(owner_id, task_id, steps)
                await _toast(app, query, "Resumed" if changed else "already handled")
            elif decision in ("more", "wrap") and app.desktop_worker is not None and rest:
                changed = await app.desktop_worker.extend(owner_id, task_id, rest, decision == "more")
                await _toast(app, query, ("Keeping going" if decision == "more" else "Wrapping up") if changed else "already handled")
            elif decision == "del":
                error = await _confirm_delete_desktop_task(app, task_id, rest or None)
                await _toast(app, query, error)
            elif decision == "delyes":
                ok, message = await owner.delete_desktop_task(app, task_id, rest)
                await _toast(app, query, message)
                if ok:
                    await _settle_confirm(app, query, f"Deleted #{task_id}")
            elif decision == "delno":
                await _toast(app, query, "Cancelled")
                await _settle_confirm(app, query, "Cancelled")
        return
    if action == "notice":
        if (
            query.from_user.is_bot or query.message is None
            or query.message.chat.id != owner_id or query.message.chat.type != "private"
        ):
            return
        decision, _, raw_id = raw_chat_id.partition(":")
        item_id = _item_id(raw_id)
        if item_id is not None and decision in ("do", "no"):
            accept = decision == "do"
            ok, message, _ = await app.noticings.decide(item_id, accept=accept, settle_telegram=False)
            await _toast(app, query, message if ok else "already handled")
            if ok:
                await _settle_confirm(app, query, "✅ Let's do it" if accept else "Not now")
            else:
                await _settle_confirm(app, query, "Already handled")
        return
    if action == "own":
        if (
            query.from_user.is_bot or query.message is None
            or query.message.chat.id != owner_id or query.message.chat.type != "private"
        ):
            return
        decision, _, payload = raw_chat_id.partition(":")
        raw_id, _, token = payload.partition(":")
        item_id = _item_id(raw_id)
        if item_id is not None:
            if decision == "cancel" and not token:
                changed = await app.self_work.cancel(item_id)
                await _toast(app, query, "Cancelled" if changed else "already handled")
                await _clear_buttons(app, query)
            elif decision == "del":
                error = await _confirm_delete_own_task(app, item_id, token or None)
                await _toast(app, query, error)
            elif decision == "delyes":
                ok, message = await app.self_work.delete(item_id, token)
                await _toast(app, query, message)
                if ok:
                    await _settle_confirm(app, query, f"Deleted #{item_id}")
            elif decision == "delno":
                await _toast(app, query, "Cancelled")
                await _settle_confirm(app, query, "Cancelled")
        return
    if action in ("prop", "plugin", "skill"):
        if (
            query.from_user.is_bot or query.message is None or query.message.chat.id != owner_id
            or query.message.chat.type != "private"
        ):
            return
        if action == "prop":
            await _handle_proposal(app, query, raw_chat_id)
        elif action == "skill":
            decision, _, payload = raw_chat_id.partition(":")
            resolved = await owner.resolve_skill_callback(app, payload)
            if decision == "delno":
                await _toast(app, query, "Cancelled")
                await _settle_confirm(app, query, "Cancelled")
            elif resolved is None:
                await _toast(app, query, "Unknown skill")
            else:
                name, digest = resolved
                if decision == "retire":
                    _, message = await owner.retire_skill(app, name)
                    await _toast(app, query, message)
                    await _clear_buttons(app, query)
                elif decision == "restore":
                    _, message = await owner.restore_skill(app, name)
                    await _toast(app, query, message)
                    await _clear_buttons(app, query)
                elif decision == "del":
                    error = await _confirm_delete_skill(app, name, digest or None)
                    await _toast(app, query, error)
                elif decision == "delyes":
                    ok, message = await owner.delete_skill(app, name, digest)
                    await _toast(app, query, message)
                    if ok:
                        await _settle_confirm(app, query, f"Deleted {name}")
        elif raw_chat_id.startswith("disable:"):
            await _toast(app, query, await owner.disable_plugin(app, raw_chat_id.removeprefix("disable:")))
            await _clear_buttons(app, query)
        elif raw_chat_id.startswith("del:"):
            name, _, digest = raw_chat_id.removeprefix("del:").partition(":")
            error = await _confirm_delete_plugin(app, name, digest or None)
            await _toast(app, query, error)
        elif raw_chat_id.startswith("delyes:"):
            name, _, digest = raw_chat_id.removeprefix("delyes:").partition(":")
            outcome = await owner.delete_plugin(app, name, digest)
            await _toast(app, query, outcome)
            if outcome == "Deleted":
                await _settle_confirm(app, query, f"Deleted {name}")
        elif raw_chat_id.startswith("delno:"):
            await _toast(app, query, "Cancelled")
            await _settle_confirm(app, query, "Cancelled")
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
            await _clear_buttons(app, query)
        return
    if action == "rem":
        decision, _, payload = raw_chat_id.partition(":")
        raw_id, _, token = payload.partition(":")
        if (
            query.from_user.is_bot or query.message is None
            or query.message.chat.id != owner_id or query.message.chat.type != "private"
            or (item_id := _item_id(raw_id)) is None
        ):
            return
        if decision == "cancel" and not token:
            changed = await app.dispatch.reminders.cancel(item_id)
            outcome = "Cancelled" if changed else "already handled"
            await _toast(app, query, outcome)
            await _settle_button(app, query, outcome, f" #{item_id}")
        elif decision == "del":
            error = await _confirm_delete_reminder(app, item_id, token or None)
            await _toast(app, query, error)
        elif decision == "delyes":
            ok, message = await app.dispatch.reminders.delete(item_id, token)
            await _toast(app, query, message)
            if ok:
                await _settle_confirm(app, query, f"Deleted #{item_id}")
        elif decision == "delno":
            await _toast(app, query, "Cancelled")
            await _settle_confirm(app, query, "Cancelled")
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return
    if action not in owner.PENDING_DECISIONS:
        return
    chat, label = await owner.decide_pending_chat(app, chat_id, action, _person(query.from_user.full_name))
    await _toast(app, query, label)  # a tap from before a restart is too old to answer, but the card still updates
    if chat is not None and query.message is not None:
        await edit_text(
            app.bot, query.message.chat.id, query.message.message_id, f"{label}: {chat['title'] or chat_id}",
            sleep=app.sleep, literal=True,
        )
    elif chat is None:
        await _clear_buttons(app, query)


async def _note_removed(app: App, update: ChatMemberUpdated, owner_id: int) -> None:
    """Dom is no longer in a group it knew: stop listening to it and tell the owner once."""
    chat = next((c for c in await app.store.list_chats() if c["chat_id"] == str(update.chat.id)), None)
    if chat is None or chat["authorization_status"] not in owner.LEAVABLE:  # unknown, already left, or blocked
        return
    await app.store.set_chat_auth_status(chat["chat_id"], "left")
    title = update.chat.title or chat["title"] or chat["chat_id"]
    own_choice = update.new_chat_member.status == "left" and update.from_user.id == app.bot_id
    await _send(app, owner_id, f"Dom left {title}" if own_choice else f"Dom was removed from {title}", literal=True)


async def handle_my_chat_member(app: App, update: ChatMemberUpdated) -> None:
    """Dom was added to a group (it starts pending and the owner is asked) or removed from one (it is `left`)."""
    owner_id = app.owner_id
    if update.chat.type not in ("group", "supergroup") or owner_id is None:
        return
    status = update.new_chat_member.status
    app.dispatch.note_admin(update.chat.id, status in ("administrator", "creator"))
    if status in ("left", "kicked"):
        await _note_removed(app, update, owner_id)
        return
    if status not in ("member", "administrator"):
        return
    await app.store.set_chat_reachable(update.chat.id, True)
    inviter = f"@{update.from_user.username}" if update.from_user.username else update.from_user.full_name
    if (await app.store.get_chat_auth_status(update.chat.id))["authorization_status"] in ("left", "ignored"):
        await app.store.set_chat_auth_status(update.chat.id, "pending_approval", approval_prompt_sent=False)
    await gate.request_approval(
        app.bot, app.store, owner_id, update.chat.id, update.chat.title or str(update.chat.id), "group", inviter,
        sleep=app.sleep,
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

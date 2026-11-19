"""Debounce incoming messages and dispatch presence-aware turns."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from dom.actions import Actions, Bubble
from dom.agent import run_turn
from dom.home import PrivateAccessError
from dom.jev import JevClient, JevUnavailable
from dom.models import IncomingMessage, Scope, StoredMessage
from dom.presence import Habits, Presence, State, parse_habits
from dom.prompt import build_messages
from dom.telegram import executor, gate
from dom.tools import TurnCtx, core_tools
from dom.triage import persona_card, triage_offline, triage_online

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.dispatch")
TICK_SECONDS = 60
DEBOUNCE_CAP = timedelta(seconds=15)  # a steady stream of messages cannot postpone a turn longer
MEDIA_WAIT_SECONDS = 15
TRIAGE_WINDOW = 15
MEDIA_CAP = 10  # descriptions per chat within MEDIA_CAP_WINDOW
MEDIA_CAP_WINDOW = timedelta(minutes=10)
CATCH_UP_MEDIA = 5  # unread messages checked for undescribed media when Dom comes online
BOT_DEPTH_LIMIT = 6
BOT_REPLY_INTERVAL = timedelta(seconds=10)
BOT_DEDUP_WINDOW = timedelta(seconds=60)
BOT_EXIT_NOTE = "Leave this bot conversation now, in character, in one short message."


class Dispatcher:
    def __init__(
        self, app: App, presence: Presence, jev: JevClient | None,
        sleep: Callable[[float], Awaitable[None]], now: Callable[[], datetime],
    ):
        self.app, self.presence, self.jev = app, presence, jev
        self.sleep, self.now = sleep, now
        self._debounces: dict[int, asyncio.Task] = {}
        self._first_notify: dict[int, datetime] = {}
        self._latest: dict[int, IncomingMessage] = {}
        self._media_tasks: dict[int, set[asyncio.Task]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._polls: dict[int, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None
        self._media_times: dict[int, deque[datetime]] = {}
        self._bot_replies: dict[int, datetime] = {}
        self._bots_in_flight: set[int] = set()

    def notify(self, msg: IncomingMessage) -> None:
        latest = self._latest.get(msg.chat_id)
        if latest is None or msg.msg_id >= latest.msg_id:
            self._latest[msg.chat_id] = msg
        waited = self.now() - self._first_notify.setdefault(msg.chat_id, self.now())
        previous = self._debounces.get(msg.chat_id)
        if previous is not None:
            if waited >= DEBOUNCE_CAP:
                return  # the pending timer fires on its own and sees this message
            previous.cancel()
        delay = min(self.presence.rng.uniform(2, 6), (DEBOUNCE_CAP - waited).total_seconds())
        self._debounces[msg.chat_id] = self._spawn(self._after_debounce(msg.chat_id, delay))

    def _debounce_pending(self, chat_id: int) -> bool:
        """A debounce older than its cap is overdue, not pending: it must not hold off other dispatches."""
        first = self._first_notify.get(chat_id)
        return chat_id in self._debounces and first is not None and self.now() - first <= DEBOUNCE_CAP

    async def describe_media(self, msg: IncomingMessage) -> None:
        # Attachments are only described while Dom is online; catch-up covers the rest.
        if msg.is_bot or not await self.presence.is_online(msg.chat_id, self._habits()):
            return
        task = self._spawn(self._describe_media(msg))
        self._media_tasks.setdefault(msg.chat_id, set()).add(task)
        task.add_done_callback(lambda done: self._clear_media_task(msg.chat_id, done))

    def _clear_media_task(self, chat_id: int, task: asyncio.Task) -> None:
        tasks = self._media_tasks[chat_id]
        tasks.discard(task)
        if not tasks:
            del self._media_tasks[chat_id]

    async def _describe_media(self, msg: IncomingMessage) -> None:
        async def authorize() -> bool:
            async with self.app.locks[msg.chat_id]:
                return await self._approved(msg.chat_id)

        await self._describe(msg.chat_id, msg.msg_id, msg.media, authorize)

    async def _describe(self, chat_id: int, msg_id: int, media: dict, authorize: Callable[[], Awaitable[bool]]) -> None:
        describer = self.app.media_describer
        if describer is None or not self._media_budget(chat_id):
            return
        try:
            description = await describer.describe(media, authorize=authorize)
            await self.app.store.set_media_description(chat_id, msg_id, description)
        except Exception:
            log.exception("could not record media description in chat %s", chat_id)

    def _media_budget(self, chat_id: int) -> bool:
        """Take one of the chat's MEDIA_CAP descriptions per MEDIA_CAP_WINDOW, if any is left."""
        now = self.now()
        times = self._media_times.setdefault(chat_id, deque())
        while times and now - times[0] >= MEDIA_CAP_WINDOW:
            times.popleft()
        if len(times) >= MEDIA_CAP:
            return False
        times.append(now)
        return True

    async def _describe_unread_media(self, chat_id: int) -> None:
        """Describe what arrived while Dom was away. The turn lock is already held, so approval is
        checked directly; the wait is bounded because an arrival-time description may hold the
        describer while it waits for that same lock."""
        async def authorize() -> bool:
            return await self._approved(chat_id)

        async def describe_all() -> None:
            for msg_id, media in await self.app.store.get_unread_media_to_describe(chat_id, CATCH_UP_MEDIA):
                await self._describe(chat_id, msg_id, media, authorize)

        try:
            await asyncio.wait_for(describe_all(), MEDIA_WAIT_SECONDS)
        except TimeoutError:
            log.warning("catch-up media descriptions timed out in chat %s", chat_id)

    async def _wait_media(self, chat_id: int) -> None:
        tasks = self._media_tasks.get(chat_id)
        if tasks:
            # Leave slow downloads and model calls running so their results can be stored later.
            await asyncio.wait(tuple(tasks), timeout=MEDIA_WAIT_SECONDS)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _after_debounce(self, chat_id: int, delay: float) -> None:
        await self.sleep(delay)
        # A new message may cancel the timer, but must never cancel a turn in progress.
        self._debounces.pop(chat_id)
        self._first_notify.pop(chat_id, None)
        await self._decide(chat_id)

    def start(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = self._spawn(self._run())

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self.sleep(TICK_SECONDS)
            try:
                await self._poll_chats()
            except Exception:
                log.exception("could not check chat presence")

    async def _poll_chats(self) -> list[asyncio.Task]:
        if self.app.owner_id is None:
            return []
        tasks = []
        for row in await self.app.store.list_chats("approved"):
            chat_id = int(row["chat_id"])
            if self._debounce_pending(chat_id):
                continue
            if chat_id not in self._polls:
                self._polls[chat_id] = self._spawn(self._poll_chat(chat_id))
            tasks.append(self._polls[chat_id])
        return tasks

    async def _poll_chat(self, chat_id: int) -> None:
        try:
            await self._decide(chat_id, background=True)
        finally:
            self._polls.pop(chat_id)

    async def tick(self) -> None:
        await asyncio.gather(*(await self._poll_chats()))

    def _habits(self) -> Habits:
        try:
            text = self.app.home.scoped(allow_private=False).read("memory/HABITS.md")
        except (PrivateAccessError, ValueError):
            text = ""
        return parse_habits(text)

    async def _approved(self, chat_id: int) -> bool:
        return await gate.decide_chat(self.app.store, chat_id, self.app.owner_id) == "process"

    async def override(self, chat_id: int, command: str) -> None:
        if command == "/wake":
            await self._wait_media(chat_id)
        async with self.app.locks[chat_id]:
            if not await self._approved(chat_id):
                return
            habits = self._habits()
            if command == "/sleep":
                await self.presence.sleep(chat_id, habits)
                return
            await self.presence.wake(chat_id, habits)
            try:
                await self._dispatch(chat_id, habits, catch_up=True)
            except Exception:
                log.exception("wake catch-up failed in chat %s", chat_id)

    async def _decide(self, chat_id: int, *, background: bool = False) -> None:
        await self._wait_media(chat_id)
        async with self.app.locks[chat_id]:
            # A newer message can restart the timer while we wait for media or the turn lock.
            if self._debounce_pending(chat_id) or not await self._approved(chat_id):
                return
            try:
                await self._dispatch(chat_id, self._habits(), background=background)
            except Exception:
                log.exception("turn failed in chat %s", chat_id)

    async def _dispatch(
        self, chat_id: int, habits: Habits, *, background: bool = False, catch_up: bool = False,
    ) -> None:
        store = self.app.store
        state, until = await self.presence.state(chat_id, habits)
        if state == "asleep" or (background and state == "online"):
            return
        if background and until is not None and until > self.now():
            return
        count, newest = await store.get_unread(chat_id)
        if newest is None:
            return

        # A human among the unread messages makes this an ordinary turn; the bots are just context.
        bots_only = newest.is_bot and not await store.has_unread_human(chat_id)
        bot_id = newest.sender_user_id if bots_only else None
        bot_depth = None
        if bots_only:
            bot_depth = await store.get_bot_depth(chat_id)
            if bot_depth > BOT_DEPTH_LIMIT or await self._suppress_bot(newest):
                await store.mark_chat_read(chat_id, newest.msg_id)
                return

        if bot_id is not None:
            self._bots_in_flight.add(bot_id)
        try:
            await self._respond(chat_id, habits, state, until, count, newest, background, catch_up, bot_depth)
        finally:
            if bot_id is not None:
                self._bots_in_flight.remove(bot_id)

    async def _suppress_bot(self, newest: StoredMessage) -> bool:
        if newest.sender_user_id is None:
            return False
        since = datetime.fromisoformat(newest.created_at) - BOT_DEDUP_WINDOW
        duplicate = bool(newest.text) and await self.app.store.has_recent_bot_duplicate(newest, since)
        last_reply = self._bot_replies.get(newest.sender_user_id)
        return (
            duplicate or newest.sender_user_id in self._bots_in_flight
            or (last_reply is not None and self.now() - last_reply < BOT_REPLY_INTERVAL)
        )

    async def _respond(
        self, chat_id: int, habits: Habits, state: State, until: datetime | None,
        count: int, newest: StoredMessage, background: bool, catch_up: bool, bot_depth: int | None,
    ) -> None:
        store = self.app.store
        final_bot_turn = bot_depth == BOT_DEPTH_LIMIT
        latest = self._latest.get(chat_id)
        live = latest if latest is not None and latest.msg_id == newest.msg_id else None
        chat_type = live.chat_type if live else ("group" if chat_id < 0 else "private")
        scope = store.scope_for(chat_id, chat_type)
        window = await self._window(chat_id, newest)
        if not await self._approved(chat_id):
            return

        if state == "offline":
            wake = background or count >= habits.wake_after_messages
            if not wake and scope == "group" and self.jev is not None:
                try:
                    triage = await triage_offline(self.jev, persona_card(self.app.home), self._triage_xml(chat_id, window))
                    wake = triage.kapoh is not None and triage.kapoh >= habits.kapoh_wake
                except JevUnavailable:
                    pass
            if not wake:
                return
            until = await self.presence.go_online(chat_id, habits)
            catch_up = True
        elif not catch_up and not final_bot_turn and self.jev is not None:
            try:
                triage = await triage_online(self.jev, persona_card(self.app.home), self._triage_xml(chat_id, window))
            except JevUnavailable:
                pass
            else:
                if triage.confident and triage.action == "ignore":
                    await store.mark_chat_read(chat_id, newest.msg_id)
                    return
                if triage.confident and triage.action == "react" and triage.emoji:
                    await executor.perform(self.app.bot, store, chat_id, newest.msg_id, Actions(react=triage.emoji))
                    await store.mark_chat_read(chat_id, newest.msg_id)
                    return

        if until is None or not await self._approved(chat_id):
            return
        if bot_depth is not None and not await store.advance_bot_depth(chat_id, newest.id, bot_depth):
            return
        if catch_up:
            await self._describe_unread_media(chat_id)
            window = await self._window(chat_id, newest)
        await self._run_turn(
            chat_id, scope, newest, live, window, until, count if catch_up else 0, final_bot_turn,
        )
        await store.mark_chat_read(chat_id, newest.msg_id)

    async def _window(self, chat_id: int, newest: StoredMessage) -> list[StoredMessage]:
        return await self.app.store.get_recent_chat_window(
            chat_id, max(TRIAGE_WINDOW, self.app.config.window_size), through_id=newest.id,
        )

    def _triage_xml(self, chat_id: int, window: list[StoredMessage]) -> str:
        return self.app.store.format_chat_window_xml(chat_id, window[-TRIAGE_WINDOW:])

    async def _run_turn(
        self, chat_id: int, scope: Scope, newest: StoredMessage, live: IncomingMessage | None,
        window: list[StoredMessage], until: datetime, away_count: int, final_bot_turn: bool,
    ) -> None:
        app = self.app
        now = self.now().astimezone(self.presence.tz)
        minutes = max(0, (until - now).total_seconds() / 60)
        title = live.chat_title if live else ""
        where = f'chat "{title}"' if title else ("a group" if scope == "group" else "a direct message")
        note = (
            f"It is {now:%A %Y-%m-%d %H:%M} ({app.config.timezone}). You are in {where} ({scope}).\n"
            f"state: online\nonline minutes left: {minutes:.1f}\n"
            f"messages that arrived while away: {away_count}\ndesktop: off"
        )
        if away_count:
            note += "\nyou just came online"
        if final_bot_turn:
            note += f"\n{BOT_EXIT_NOTE}"
        window_size = app.config.window_size
        window = window[-window_size:] if window_size > 0 else []
        participants = [m.sender_user_id for m in window if m.role == "user" and m.sender_user_id is not None]
        if newest.sender_user_id is not None:
            participants.append(newest.sender_user_id)
        messages = build_messages(
            app.home, scope, chat_id, participants, app.store.format_chat_window_xml(chat_id, window), note,
        )
        ctx = TurnCtx(scope, chat_id, newest.sender_user_id, app.home, app.store, app.owner_id, self.presence)
        try:
            actions = await run_turn(app.client, messages, core_tools(), ctx)
        except Exception:
            if not final_bot_turn:
                raise
            log.exception("model could not compose the bot exit in chat %s", chat_id)
            actions = Actions(no_response=True)
        if final_bot_turn:
            bubbles = actions.bubbles[:1] if not actions.no_response else []
            actions = Actions(bubbles=bubbles or [Bubble(text="I'll leave it there for now.")])
        try:
            await executor.perform(
                app.bot, app.store, chat_id, newest.msg_id, actions, sleep=app.sleep,
                thread_id=live.thread_id if live else None,
            )
        finally:
            if newest.is_bot and newest.sender_user_id is not None and actions.bubbles and not actions.no_response:
                self._bot_replies[newest.sender_user_id] = self.now()

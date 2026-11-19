"""Debounce incoming messages and dispatch presence-aware turns."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from contextlib import asynccontextmanager, nullcontext
from functools import partial
from datetime import datetime, timedelta
from collections.abc import AsyncIterator, Coroutine
from typing import TYPE_CHECKING, Any, AsyncContextManager, Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from dom.actions import Actions, Bubble
from dom.agent import run_turn
from dom.capabilities import describe
from dom.home import PrivateAccessError
from dom.jev import JevClient, JevUnavailable
from dom.jobs import DailyJobs
from dom.memory_pass import MemoryPass
from dom.models import IncomingMessage, Scope, StoredMessage
from dom.presence import Habits, Presence, State, parse_habits
from dom.prompt import build_messages
from dom.telegram import executor, gate
from dom.tools import TurnCtx, core_tools
from dom.traits import for_chat
from dom.triage import persona_card, triage_offline, triage_online, triage_urgency
from dom.usage import ModelCallSkipped, Usage, check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.dispatch")
TICK_SECONDS = 60
DEBOUNCE_CAP = timedelta(seconds=15)  # a steady stream of messages cannot postpone a turn longer
MEDIA_WAIT_SECONDS = 15
ORPHAN_AGE = timedelta(seconds=10)  # an online chat's unread message this old lost its debounce
TRIAGE_WINDOW = 15
OFFLINE_URGENCY = 1.5
ASLEEP_URGENCY = 2.5
URGENT_WAKE_MINUTES = 15
MEDIA_CAP = 10  # descriptions per chat within MEDIA_CAP_WINDOW
MEDIA_CAP_WINDOW = timedelta(minutes=10)
CATCH_UP_MEDIA = 5  # unread messages checked for undescribed media when Dom comes online
BOT_DEPTH_LIMIT = 6
BOT_REPLY_INTERVAL = timedelta(seconds=10)
BOT_DEDUP_WINDOW = timedelta(seconds=60)
MAX_TURN_FAILURES = 3  # consecutive failures after which a chat's unread messages are given up on
_CHAT_GONE = re.compile(r"chat not found|kicked|not a member", re.IGNORECASE)
MEMORY_PASS_INTERVAL = timedelta(minutes=5)  # per chat
MEMORY_PASS_MARGIN = 5  # start a pass this many messages before unprocessed ones scroll out of the chat window
BOT_EXIT_NOTE = "Leave this bot conversation now, in character, in one short message."


class Dispatcher:
    def __init__(
        self, app: App, presence: Presence, jev: JevClient | None,
        sleep: Callable[[float], Awaitable[None]], now: Callable[[], datetime],
    ):
        self.app, self.presence, self.jev = app, presence, jev
        self.sleep, self.now = sleep, now
        self.usage = Usage(app.store, now)
        self.jobs = DailyJobs(app, now)
        self._debounces: dict[int, asyncio.Task] = {}
        self._first_notify: dict[int, datetime] = {}
        self._latest: dict[int, IncomingMessage] = {}
        self._media_tasks: dict[int, set[asyncio.Task]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._polls: dict[int, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None
        self._failures: dict[int, int] = {}
        self._media_times: dict[int, deque[datetime]] = {}
        self._bot_replies: dict[tuple[int, int], datetime] = {}  # (chat_id, bot_id) -> last reply
        self._bots_in_flight: set[tuple[int, int]] = set()
        self._memory_running: set[int] = set()
        self._memory_last: dict[int, datetime] = {}  # when the chat's last pass finished
        self._memory_slot = asyncio.Semaphore(1)  # one pass at a time across chats
        presence.on_session_end = self.request_memory_pass

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

    def request_memory_pass(self, chat_id: int) -> None:
        """Start a background memory pass for the chat unless one is running or the last one was under
        MEMORY_PASS_INTERVAL ago. Never waits, so it is safe to call under the turn lock."""
        last = self._memory_last.get(chat_id)
        if chat_id in self._memory_running or (last is not None and self.now() - last < MEMORY_PASS_INTERVAL):
            return
        self._memory_running.add(chat_id)
        self._spawn(self._memory_pass(chat_id))

    async def _memory_pass(self, chat_id: int) -> None:
        ran = True  # a failed attempt counts too: do not hammer a model that is down
        try:
            async with self._memory_slot:
                ran = await MemoryPass(self.app).run(chat_id)
        except Exception:
            log.exception("memory pass failed in chat %s", chat_id)
        finally:
            if ran:
                self._memory_last[chat_id] = self.now()
            self._memory_running.discard(chat_id)

    def _debounce_pending(self, chat_id: int) -> bool:
        """A debounce older than its cap is overdue, not pending: it must not hold off other dispatches."""
        first = self._first_notify.get(chat_id)
        return chat_id in self._debounces and first is not None and self.now() - first <= DEBOUNCE_CAP

    async def describe_media(self, msg: IncomingMessage) -> None:
        # Attachments are only described while Dom is online; catch-up covers the rest.
        try:
            if msg.is_bot or not await self.presence.is_online(msg.chat_id, self._habits()):
                return
        except Exception:
            log.exception("could not check presence before describing media in chat %s", msg.chat_id)
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
        await self._describe(msg.chat_id, msg.msg_id, msg.media, self._media_request(msg.chat_id))

    @asynccontextmanager
    async def _media_request(self, chat_id: int, *, lock_held: bool = False) -> AsyncIterator[None]:
        # Downloads stay outside the turn lock. The model call itself runs under it.
        with model_request("media", chat_id, partial(self.model_allowed, chat_id)):
            async with (nullcontext() if lock_held else self.app.locks[chat_id]):
                await check_model_allowed()
                yield

    async def _describe(
        self, chat_id: int, msg_id: int, media: dict, request: AsyncContextManager[None],
    ) -> None:
        describer = self.app.media_describer
        if describer is None or not self._media_budget(chat_id):
            return
        try:
            description = await describer.describe(media, request=request)
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
        async def describe_all() -> None:
            for msg_id, media in await self.app.store.get_unread_media_to_describe(chat_id, CATCH_UP_MEDIA):
                await self._describe(chat_id, msg_id, media, self._media_request(chat_id, lock_held=True))

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
            await self.jobs.tick()

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
            self._polls.pop(chat_id, None)

    async def tick(self) -> None:
        await asyncio.gather(*(await self._poll_chats()))

    def _habits(self) -> Habits:
        try:
            text = self.app.home.scoped(allow_private=False).read("memory/HABITS.md")
        except (PrivateAccessError, ValueError):
            text = ""
        return parse_habits(text)

    def peek_presence(self, rows: list[dict]) -> list[tuple[State, datetime | None]]:
        """Presence of each stored row as the next turn would find it; writes nothing."""
        habits = self._habits()
        return [self.presence.peek(row, habits) for row in rows]

    async def _approved(self, chat_id: int) -> bool:
        return await gate.decide_chat(self.app.store, chat_id, self.app.owner_id) == "process"

    async def spend_paused(self, chat_id: int) -> bool:
        """Friends' chats pause once today's spend cap is reached; the owner's own chat never does."""
        return chat_id != self.app.owner_id and await self.usage.over_cap(self.app.config, self.app.bot)

    async def model_allowed(self, chat_id: int) -> bool:
        return await self._approved(chat_id) and not await self.spend_paused(chat_id)

    async def override(self, chat_id: int, command: str) -> bool:
        """The owner's /wake or /sleep. False if the chat is unknown or not approved; that lookup never
        creates a chat row."""
        if command == "/wake":
            await self._wait_media(chat_id)
        async with self.app.locks[chat_id]:
            if not await self.app.store.is_chat_approved(chat_id):
                return False
            habits = self._habits()
            if command == "/sleep":
                await self.presence.sleep(chat_id, habits)
                return True
            await self.presence.wake(chat_id, habits)
            await self._guarded_dispatch(chat_id, habits, catch_up=True)
            return True

    async def _decide(self, chat_id: int, *, background: bool = False) -> None:
        try:
            await self._wait_media(chat_id)
            async with self.app.locks[chat_id]:
                # A newer message can restart the timer while we wait for media or the turn lock.
                if self._debounce_pending(chat_id) or not await self._approved(chat_id):
                    return
                await self._guarded_dispatch(chat_id, self._habits(), background=background)
        except Exception:
            log.exception("could not decide on chat %s", chat_id)

    async def _guarded_dispatch(self, chat_id: int, habits: Habits, **kwargs: bool) -> None:
        """Run a dispatch; a chat whose turns keep failing is given up on instead of retried forever."""
        try:
            with model_request("triage", chat_id, partial(self.model_allowed, chat_id)):
                await self._dispatch(chat_id, habits, **kwargs)
        except ModelCallSkipped:
            return
        except Exception as exc:
            log.exception("turn failed in chat %s", chat_id)
            gone = isinstance(exc, TelegramForbiddenError) or (
                isinstance(exc, TelegramBadRequest) and _CHAT_GONE.search(str(exc)) is not None
            )
            failures = self._failures[chat_id] = self._failures.get(chat_id, 0) + 1
            if gone or failures >= MAX_TURN_FAILURES:
                log.warning("giving up on the unread messages in chat %s (%s)", chat_id, type(exc).__name__)
                self._failures.pop(chat_id, None)
                _, newest = await self.app.store.get_unread(chat_id)
                if newest is not None:
                    await self.app.store.mark_chat_read(chat_id, newest.msg_id)
        else:
            self._failures.pop(chat_id, None)

    async def _dispatch(
        self, chat_id: int, habits: Habits, *, background: bool = False, catch_up: bool = False,
    ) -> None:
        store = self.app.store
        state, until = await self.presence.state(chat_id, habits)
        if state == "asleep" and (background or chat_id != self.app.owner_id):
            return
        if background and state == "offline" and until is not None and until > self.now():
            return
        count, newest = await store.get_unread(chat_id)
        if newest is None:
            return
        if background and state == "online" and self.now() - datetime.fromisoformat(newest.created_at) < ORPHAN_AGE:
            return  # a message that fresh still has its debounce

        # A human among the unread messages makes this an ordinary turn; the bots are just context.
        bots_only = newest.is_bot and not await store.has_unread_human(chat_id)
        bot_key = (chat_id, newest.sender_user_id) if bots_only and newest.sender_user_id is not None else None
        bot_depth = None
        if bots_only:
            bot_depth = await store.get_bot_depth(chat_id)
            if bot_depth > BOT_DEPTH_LIMIT or await self._suppress_bot(chat_id, newest):
                await store.mark_chat_read(chat_id, newest.msg_id)
                return

        if bot_key is not None:
            self._bots_in_flight.add(bot_key)
        try:
            await self._respond(chat_id, habits, state, until, count, newest, background, catch_up, bot_depth)
        finally:
            if bot_key is not None:
                self._bots_in_flight.remove(bot_key)

    async def _suppress_bot(self, chat_id: int, newest: StoredMessage) -> bool:
        if newest.sender_user_id is None:
            return False
        key = (chat_id, newest.sender_user_id)
        since = datetime.fromisoformat(newest.created_at) - BOT_DEDUP_WINDOW
        duplicate = bool(newest.text) and await self.app.store.has_recent_bot_duplicate(newest, since)
        last_reply = self._bot_replies.get(key)
        return (
            duplicate or key in self._bots_in_flight
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
        await check_model_allowed()

        urgency = None
        count_wakes = state == "offline" and count >= habits.wake_after_messages
        if state != "online" and scope == "owner_dm" and not background and not count_wakes and self.jev is not None:
            try:
                urgency = await triage_urgency(self.jev, persona_card(self.app.home), self._triage_xml(chat_id, window))
            except JevUnavailable:
                pass

        if state == "asleep":
            if urgency is None or urgency < ASLEEP_URGENCY:
                return
            until = await self.presence.wake(chat_id, habits, minutes=URGENT_WAKE_MINUTES)
            catch_up = True
        elif state == "offline":
            wake = background or count_wakes or (
                urgency is not None and urgency >= OFFLINE_URGENCY
            )
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

        if until is None:
            return
        await check_model_allowed()
        if catch_up:
            await self._describe_unread_media(chat_id)
            window = await self._window(chat_id, newest)
            await check_model_allowed()
        if bot_depth is not None and not await store.advance_bot_depth(chat_id, newest.id, bot_depth):
            return
        with model_request("chat", chat_id, partial(self.model_allowed, chat_id)):
            await self._run_turn(
                chat_id, scope, newest, live, window, until, count if catch_up else 0, final_bot_turn, habits,
            )
        await store.mark_chat_read(chat_id, newest.msg_id)
        # Notes only need to catch what is about to leave Dom's view; the session-end pass covers the rest.
        if await store.count_new_for_memory(chat_id) >= max(1, self.app.config.window_size - MEMORY_PASS_MARGIN):
            self.request_memory_pass(chat_id)

    async def _window(self, chat_id: int, newest: StoredMessage) -> list[StoredMessage]:
        return await self.app.store.get_recent_chat_window(
            chat_id, max(TRIAGE_WINDOW, self.app.config.window_size), through_id=newest.id,
        )

    def _triage_xml(self, chat_id: int, window: list[StoredMessage]) -> str:
        chat = [m for m in window if m.role != "event"]  # events are the owner's commands, not chat content for Jev
        return self.app.store.format_chat_window_xml(chat_id, chat[-TRIAGE_WINDOW:])

    async def _run_turn(
        self, chat_id: int, scope: Scope, newest: StoredMessage, live: IncomingMessage | None,
        window: list[StoredMessage], until: datetime, away_count: int, final_bot_turn: bool, habits: Habits,
    ) -> None:
        app = self.app
        now = self.now().astimezone(self.presence.tz)
        minutes = max(0, (until - now).total_seconds() / 60)
        title = live.chat_title if live else ""
        where = f'chat "{title}"' if title else ("a group" if scope == "group" else "a direct message")
        note = (
            f"It is {now:%A %Y-%m-%d %H:%M} ({app.config.timezone}). You are in {where} ({scope}).\n"
            f"state: online\nonline minutes left: {minutes:.1f}\n"
            f"messages that arrived while away: {away_count}\n{describe(app, scope)}"
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
        sliders, preset = await for_chat(app.home, app.store, chat_id)
        messages = build_messages(
            app.home, scope, chat_id, participants, app.store.format_chat_window_xml(chat_id, window), note,
            sliders=sliders, preset=preset,
        )
        ctx = TurnCtx(
            scope, chat_id, newest.sender_user_id, app.home, app.store, app.owner_id, self.presence, habits,
        )
        try:
            actions = await run_turn(app.client, messages, core_tools(), ctx)
        except ModelCallSkipped:
            raise
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
                now = self.now()
                self._bot_replies = {k: t for k, t in self._bot_replies.items() if now - t < BOT_DEDUP_WINDOW}
                self._bot_replies[(chat_id, newest.sender_user_id)] = now

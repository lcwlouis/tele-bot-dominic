"""Debounce incoming messages and dispatch presence-aware turns."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from dom.actions import Actions
from dom.agent import run_turn
from dom.home import PrivateAccessError
from dom.jev import JevClient, JevUnavailable
from dom.models import IncomingMessage, Scope, StoredMessage
from dom.presence import Habits, Presence, parse_habits
from dom.prompt import build_messages
from dom.telegram import executor, gate
from dom.tools import TurnCtx, core_tools
from dom.triage import persona_card, triage_offline, triage_online

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.dispatch")
TICK_SECONDS = 60
TRIAGE_WINDOW = 15


class Dispatcher:
    def __init__(
        self, app: App, presence: Presence, jev: JevClient | None,
        sleep: Callable[[float], Awaitable[None]], now: Callable[[], datetime],
    ):
        self.app, self.presence, self.jev = app, presence, jev
        self.sleep, self.now = sleep, now
        self._debounces: dict[int, asyncio.Task] = {}
        self._latest: dict[int, IncomingMessage] = {}
        self._tasks: set[asyncio.Task] = set()
        self._polls: dict[int, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None

    def notify(self, msg: IncomingMessage) -> None:
        latest = self._latest.get(msg.chat_id)
        if latest is None or msg.msg_id >= latest.msg_id:
            self._latest[msg.chat_id] = msg
        previous = self._debounces.get(msg.chat_id)
        if previous is not None:
            previous.cancel()
        self._debounces[msg.chat_id] = self._spawn(self._after_debounce(msg.chat_id))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _after_debounce(self, chat_id: int) -> None:
        await self.sleep(self.presence.rng.uniform(2, 6))
        # A new message may cancel the timer, but must never cancel a turn in progress.
        self._debounces.pop(chat_id)
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
            if chat_id in self._debounces:
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
        async with self.app.locks[chat_id]:
            # Approval can change while the timer or the previous turn holds us up.
            if not await self._approved(chat_id):
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

        latest = self._latest.get(chat_id)
        live = latest if latest is not None and latest.msg_id == newest.msg_id else None
        chat_type = live.chat_type if live else ("group" if chat_id < 0 else "private")
        scope = store.scope_for(chat_id, chat_type)
        window = await store.get_recent_chat_window(
            chat_id, max(TRIAGE_WINDOW, self.app.config.window_size), through_id=newest.id,
        )
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
        elif not catch_up and self.jev is not None:
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
        await self._run_turn(chat_id, scope, newest, live, window, until, count if catch_up else 0)
        await store.mark_chat_read(chat_id, newest.msg_id)

    def _triage_xml(self, chat_id: int, window: list[StoredMessage]) -> str:
        return self.app.store.format_chat_window_xml(chat_id, window[-TRIAGE_WINDOW:])

    async def _run_turn(
        self, chat_id: int, scope: Scope, newest: StoredMessage, live: IncomingMessage | None,
        window: list[StoredMessage], until: datetime, away_count: int,
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
        window_size = app.config.window_size
        window = window[-window_size:] if window_size > 0 else []
        participants = [m.sender_user_id for m in window if m.role == "user" and m.sender_user_id is not None]
        if newest.sender_user_id is not None:
            participants.append(newest.sender_user_id)
        messages = build_messages(
            app.home, scope, chat_id, participants, app.store.format_chat_window_xml(chat_id, window), note,
        )
        ctx = TurnCtx(scope, chat_id, newest.sender_user_id, app.home, app.store, app.owner_id, self.presence)
        actions = await run_turn(app.client, messages, core_tools(), ctx)
        await executor.perform(
            app.bot, app.store, chat_id, newest.msg_id, actions, sleep=app.sleep,
            thread_id=live.thread_id if live else None,
        )

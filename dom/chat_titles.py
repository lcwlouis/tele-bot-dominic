"""Titles for group chats that Dom's notes name but this bot has no title for.

Notes copied from another instance name chats (`memory/chats/<id>.md`, `{chat <id>}` tags) that this bot may have no
row for, so the dashboard could only say "Group <id>". A background pass asks Telegram for each such group's title,
a few at a time, never from a reply. A title goes into the chats table when the chat has a row, else into the kv table
(`chat_title:<id>`). A chat Telegram refuses (the bot was never in it) is marked unknown there and not asked again
for a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter


if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.chat_titles")
KEY = "chat_title:"
RETRY = timedelta(days=1)  # before a refused chat is asked again
PER_PASS = 3  # Telegram calls in one pass: the rest wait for the next
GAP_SECONDS = 2.0  # between those calls
TITLE_MAX = 60
SCAN_FILES = 300  # note files read for {chat <id>} tags in one pass
SCAN_CHARS = 100_000  # read from each
SCAN_IDS = 500  # group IDs kept from the notes in one pass
_TAG = re.compile(r"\{chat\s+(-\d+)\}")
# What Telegram says when it will never answer for this chat (the bot was never in it, or has left); any other
# refusal may pass, so it is tried again on the normal schedule.
_DEFINITE = (
    "chat not found", "bot is not a member", "bot was kicked", "bot was blocked", "channel_private", "peer_id_invalid",
    "group chat was deactivated", "chat was deleted",
)


def _entry(value: str | None) -> dict:
    try:
        entry = json.loads(value or "")
    except ValueError:
        return {}
    return entry if isinstance(entry, dict) else {}


class ChatTitles:
    def __init__(self, app: App) -> None:
        self.app = app
        self._running = False  # a pass is under way; the check and the mark have no await between them
        self._wait_until: datetime | None = None  # Telegram asked us to wait, until then

    async def known(self) -> dict[str, str]:
        """The titles Telegram gave for chats with no row, by chat ID. Chats marked unknown have none."""
        entries = ((key.removeprefix(KEY), _entry(value)) for key, value in await self.app.store.kv_prefix(KEY))
        return {chat_id: entry["title"] for chat_id, entry in entries if isinstance(entry.get("title"), str) and entry["title"]}

    async def refresh(self) -> None:
        """Ask Telegram for the titles that are missing and due. Called from the background tick, never from a reply."""
        if self._running:
            return
        self._running = True
        try:
            await self._pass()
        except Exception:
            log.exception("could not look up chat titles")
        finally:
            self._running = False

    def _named_groups(self) -> list[str]:
        """The group IDs the notes name: by a chat note's file name, and by a `{chat <id>}` tag in any regular note."""
        home = self.app.home
        people, chats = home.list_memory_notes()
        found = {stem for note in chats if (stem := Path(note).stem).startswith("-") and stem[1:].isdigit()}
        for note in [*people, *chats, *home.list_entity_notes(), "MEMORY.md", "memory/JOURNAL.md"][:SCAN_FILES]:
            if len(found) >= SCAN_IDS:
                break
            try:
                found.update(_TAG.findall(home.read(note)[:SCAN_CHARS]))
            except (OSError, ValueError):
                continue
        return sorted(found)[:SCAN_IDS]

    async def _pass(self) -> None:
        store = self.app.store
        now = self.app.dispatch.now()
        if self._wait_until is not None and now < self._wait_until:
            return  # Telegram said to wait, and the wait is not over
        named = await asyncio.to_thread(self._named_groups)
        rows = {chat["chat_id"]: chat for chat in await store.list_chats()}
        cached = dict(await store.kv_prefix(KEY))
        asked = 0
        for chat_id in named:
            row = rows.get(chat_id)
            if row is not None and (row["title"] or "").strip():
                continue
            entry = _entry(cached.get(KEY + chat_id))
            if isinstance(entry.get("title"), str) and entry["title"]:
                if row is not None:  # a title asked for before the chat got its row
                    await store.adopt_chat_title(chat_id, entry["title"], KEY + chat_id, json.dumps(entry))
                continue
            if entry.get("at") and now - datetime.fromisoformat(entry["at"]) < RETRY:
                continue  # refused lately
            if asked >= PER_PASS:
                break
            if asked:
                await self.app.sleep(GAP_SECONDS)
            asked += 1
            try:
                chat = await self.app.bot.get_chat(int(chat_id))
            except TelegramRetryAfter as exc:
                self._wait_until = now + timedelta(seconds=exc.retry_after)
                return  # rate-limited: the rest, and this one unmarked, wait for the deadline
            except (TelegramForbiddenError, TelegramBadRequest, TelegramNotFound) as exc:
                if not isinstance(exc, TelegramNotFound) and not any(phrase in str(exc).lower() for phrase in _DEFINITE):
                    log.warning("Telegram would not give the title of chat %s: %s", chat_id, str(exc)[:120])
                    continue  # not a sure refusal: asked again on the next pass, not marked
                title = ""
            except Exception as exc:
                log.warning("could not ask Telegram for the title of chat %s (%s)", chat_id, type(exc).__name__)
                return
            else:
                title = " ".join((getattr(chat, "title", None) or "").split())[:TITLE_MAX]
            if title:
                await store.adopt_chat_title(
                    chat_id, title, KEY + chat_id, json.dumps({"title": title, "at": now.isoformat()}),
                )
            else:
                await store.kv_set(KEY + chat_id, json.dumps({"unknown": True, "at": now.isoformat()}))

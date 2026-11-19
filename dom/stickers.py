"""Owner-selected Telegram sticker sets and their emoji index."""

from __future__ import annotations

import asyncio
import logging
import re
import tomllib
from collections.abc import Callable
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from dom.home import Home

log = logging.getLogger(__name__)
MAX_SETS = 10
MAX_STICKERS = 120
MAX_EMOJI = 40
RETRY_AFTER = timedelta(hours=1)  # before a set that failed to load is asked for again
SET_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class Stickers:
    def __init__(self, home: Home, bot: Bot, now: Callable[[], datetime]):
        self.home, self.bot, self._now = home.scoped(allow_private=False), bot, now
        self._retry_at: dict[str, datetime] = {}
        self.by_emoji: dict[str, list[str]] = {}
        self._loaded: dict[str, list[tuple[str, str]]] = {}
        self._lock = asyncio.Lock()

    def _names(self) -> list[str]:
        sets = tomllib.loads(self.home.read("stickers.toml")).get("sets", [])
        if not isinstance(sets, list) or any(not isinstance(name, str) or not SET_NAME.fullmatch(name) for name in sets):
            raise ValueError("stickers.toml needs a sets list of sticker set names")
        return list(dict.fromkeys(sets))

    async def _fetch(self, name: str) -> list[tuple[str, str]] | None:
        try:
            result = await self.bot.get_sticker_set(name)
        except TelegramAPIError as error:
            log.warning("could not load sticker set %s (%s)", name, type(error).__name__)
            self._retry_at[name] = self._now() + RETRY_AFTER
            return None
        self._retry_at.pop(name, None)
        return [(sticker.emoji, sticker.file_id) for sticker in result.stickers[:MAX_STICKERS] if sticker.emoji]

    async def _refresh(self, names: list[str]) -> None:
        names = names[:MAX_SETS]
        now = self._now()
        missing = [name for name in names if name not in self._loaded and self._retry_at.get(name, now) <= now]
        for name, items in zip(missing, await asyncio.gather(*(self._fetch(name) for name in missing))):
            if items is not None:
                self._loaded[name] = items
        self._loaded = {name: self._loaded[name] for name in names if name in self._loaded}
        index: dict[str, list[str]] = {}
        for items in self._loaded.values():
            for emoji, file_id in items:
                index.setdefault(emoji, []).append(file_id)
        self.by_emoji = index

    async def refresh(self) -> None:
        """Load the listed sets. Never raises: Dom's background pass and startup go on without stickers."""
        try:
            async with self._lock:
                try:
                    names = self._names()
                except (OSError, ValueError) as error:
                    log.warning("could not read sticker sets: %s", type(error).__name__)
                    return
                await self._refresh(names)
        except Exception:
            log.exception("could not refresh stickers")

    async def change(self, action: str, name: str) -> str:
        if not SET_NAME.fullmatch(name):
            raise ValueError("Use the short sticker set name from its Telegram link.")
        async with self._lock:
            names = self._names()
            if action == "add":
                if name in names:
                    return f"Sticker set already listed: {name}"
                if len(names) >= MAX_SETS:
                    raise ValueError(f"At most {MAX_SETS} sticker sets can be listed.")
                items = await self._fetch(name)
                if items is None:
                    raise ValueError("Could not load that sticker set from Telegram.")
                self.home.write_sticker_sets([*names, name])
                self._loaded[name] = items
                result = f"Added sticker set {name}"
                names.append(name)
            else:
                if name not in names:
                    return f"Sticker set is absent: {name}"
                names.remove(name)
                self.home.write_sticker_sets(names)
                result = f"Removed sticker set {name}"
            await self._refresh(names)
            return result

    def turn_note(self) -> str:
        emoji = ", ".join(list(self.by_emoji)[:MAX_EMOJI])
        if not emoji:
            return ""
        return f'Optional action "sticker": choose one of these emoji to send after the bubbles: {emoji}'

"""Owner-selected Telegram sticker sets, their emoji index, and cached previews."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from dom.home import Home

if TYPE_CHECKING:
    from dom.media import MediaDescriber

log = logging.getLogger(__name__)
MAX_SETS = 10
MAX_STICKERS = 120
MAX_EMOJI = 40
MAX_DESCRIPTION_CHARS = 80
MAX_CONSECUTIVE_DESC_FAILURES = 3
DESCRIPTIONS_FILE = "sticker_descriptions.json"
RETRY_AFTER = timedelta(hours=1)  # before a set or description that failed is asked for again
SET_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
SHARE_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/addstickers/([^/?#\s]*)/?(?:[?#]\S*)?", re.IGNORECASE)


def set_name(value: str) -> str:
    """A sticker set's short name, given either the name or its addstickers share link."""
    text = value.strip()
    if (link := SHARE_LINK.fullmatch(text)) is not None:
        text = link.group(1)
    if not SET_NAME.fullmatch(text):
        raise ValueError("Use a sticker set's short name or its addstickers share link.")
    return text


def _clean_description(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    line = " ".join(text.split()).strip(" \"'`-•*")
    if not line:
        return None
    if len(line) > MAX_DESCRIPTION_CHARS:
        line = line[:MAX_DESCRIPTION_CHARS - 1].rstrip(" ,;:-") + "…"
    return line


@dataclass(frozen=True)
class StickerItem:
    emoji: str
    file_id: str
    key: str
    mime: str = "image/webp"
    preview_file_id: str | None = None
    file_size: int | None = None

    def media_dict(self) -> dict:
        media: dict = {
            "kind": "sticker",
            "file_id": self.file_id,
            "file_size": self.file_size,
            "mime": self.mime,
            "emoji": self.emoji,
        }
        if self.preview_file_id:
            media["preview_file_id"] = self.preview_file_id
        return media


def _sticker_item(sticker: object) -> StickerItem:
    is_animated = bool(getattr(sticker, "is_animated", False))
    is_video = bool(getattr(sticker, "is_video", False))
    thumb = getattr(sticker, "thumbnail", None)
    mime = "application/x-tgsticker" if is_animated else "video/webm" if is_video else "image/webp"
    preview_file_id = getattr(thumb, "file_id", None) if (is_animated or is_video) and thumb else None
    file_id = getattr(sticker, "file_id")
    key = getattr(sticker, "file_unique_id", None) or file_id
    return StickerItem(
        emoji=getattr(sticker, "emoji"),
        file_id=file_id,
        key=key,
        mime=mime,
        preview_file_id=preview_file_id,
        file_size=getattr(sticker, "file_size", None),
    )


class Stickers:
    def __init__(
        self,
        home: Home,
        bot: Bot,
        now: Callable[[], datetime],
        describer: Callable[[], MediaDescriber | None] | None = None,
    ):
        self.home, self.bot, self._now = home.scoped(allow_private=False), bot, now
        self._describer = describer
        self._retry_at: dict[str, datetime] = {}
        self._desc_retry_at: dict[str, datetime] = {}
        self._describer_retry_at: datetime | None = None
        self.by_emoji: dict[str, list[str]] = {}
        self._loaded: dict[str, list[StickerItem]] = {}
        self._descriptions: dict[str, str] = {}
        self._descriptions_loaded = False
        self._lock = asyncio.Lock()

    def names(self) -> list[str]:
        try:
            return self._names()
        except (OSError, ValueError):
            return []

    def _names(self) -> list[str]:
        sets = tomllib.loads(self.home.read("stickers.toml")).get("sets", [])
        if not isinstance(sets, list) or any(not isinstance(name, str) or not SET_NAME.fullmatch(name) for name in sets):
            raise ValueError("stickers.toml needs a sets list of sticker set names")
        return list(dict.fromkeys(sets))

    def _load_descriptions(self) -> None:
        self._descriptions_loaded = True
        try:
            raw = self.home.read(DESCRIPTIONS_FILE)
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            log.warning("could not read %s; starting with empty sticker descriptions", DESCRIPTIONS_FILE)
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._descriptions = {
            key: desc
            for key, value in data.items()
            if isinstance(key, str) and (desc := _clean_description(value))
        }

    def _save_descriptions(self) -> None:
        try:
            self.home.write_sticker_descriptions(self._descriptions)
        except OSError:
            log.warning("could not write %s", DESCRIPTIONS_FILE)

    async def _fetch(self, name: str) -> list[StickerItem] | None:
        try:
            result = await self.bot.get_sticker_set(name)
        except TelegramAPIError as error:
            log.warning("could not load sticker set %s (%s)", name, type(error).__name__)
            self._retry_at[name] = self._now() + RETRY_AFTER
            return None
        self._retry_at.pop(name, None)
        return [_sticker_item(sticker) for sticker in result.stickers[:MAX_STICKERS] if sticker.emoji]

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
            for item in items:
                index.setdefault(item.emoji, []).append(item.file_id)
        self.by_emoji = index

    async def _describe_items(self, items: list[StickerItem]) -> tuple[int, int]:
        describer = self._describer() if self._describer is not None else None
        if describer is None:
            return 0, 0
        if not self._descriptions_loaded:
            self._load_descriptions()
        now = self._now()
        if self._describer_retry_at is not None and self._describer_retry_at > now:
            return 0, 0

        described = 0
        failed = 0
        consecutive_failures = 0
        dirty = False
        seen: set[str] = set()
        for item in items:
            if item.key in seen or item.key in self._descriptions:
                continue
            seen.add(item.key)
            if self._desc_retry_at.get(item.key, now) > now:
                continue
            if item.mime != "image/webp" and not item.preview_file_id:
                self._desc_retry_at[item.key] = now + RETRY_AFTER
                failed += 1
                continue
            try:
                raw = await describer.describe_sticker(item.media_dict())
            except Exception as error:
                log.warning("sticker description failed for %s (%s)", item.key, type(error).__name__)
                raw = None
            desc = _clean_description(raw)
            if desc:
                self._descriptions[item.key] = desc
                self._desc_retry_at.pop(item.key, None)
                self._describer_retry_at = None
                consecutive_failures = 0
                described += 1
                dirty = True
            else:
                self._desc_retry_at[item.key] = now + RETRY_AFTER
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_DESC_FAILURES:
                    self._describer_retry_at = now + RETRY_AFTER
                    break
        if dirty:
            self._save_descriptions()
        return described, failed

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
                if not self._descriptions_loaded:
                    self._load_descriptions()
        except Exception:
            log.exception("could not refresh stickers")

    async def ensure_descriptions(self) -> None:
        """Lazily describe loaded stickers missing cached descriptions. Never raises."""
        try:
            async with self._lock:
                if not self._descriptions_loaded:
                    self._load_descriptions()
                items = [item for group in self._loaded.values() for item in group]
                if items:
                    await self._describe_items(items)
        except Exception:
            log.exception("could not describe stickers")

    async def refresh_descriptions(self) -> tuple[int, int]:
        """Clear cached descriptions for the listed sets and re-describe them."""
        async with self._lock:
            names = self._names()
            await self._refresh(names)
            if not self._descriptions_loaded:
                self._load_descriptions()
            items = [item for group in self._loaded.values() for item in group]
            removed = False
            for item in items:
                if self._descriptions.pop(item.key, None) is not None:
                    removed = True
                self._desc_retry_at.pop(item.key, None)
            self._describer_retry_at = None
            if removed:
                self._save_descriptions()
            return await self._describe_items(items)

    async def change(self, action: str, name: str) -> str:
        name = set_name(name)
        async with self._lock:
            names = self._names()
            added_items: list[StickerItem] | None = None
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
                added_items = items
                result = f"Added sticker set {name}"
                names.append(name)
            else:
                if name not in names:
                    return f"Sticker set is absent: {name}"
                names.remove(name)
                self.home.write_sticker_sets(names)
                result = f"Removed sticker set {name}"
            await self._refresh(names)
            if added_items:
                try:
                    await self._describe_items(added_items)
                except Exception:
                    log.exception("could not describe added sticker set %s", name)
            return result

    def turn_note(self) -> str:
        if not self._descriptions_loaded:
            self._load_descriptions()
        emojis = list(self.by_emoji)[:MAX_EMOJI]
        if not emojis:
            return ""
        desc_by_emoji: dict[str, str] = {}
        for items in self._loaded.values():
            for item in items:
                if item.emoji not in desc_by_emoji and (desc := self._descriptions.get(item.key)):
                    desc_by_emoji[item.emoji] = desc
        if not desc_by_emoji:
            return f'Optional action "sticker": choose one of these emoji to send after the bubbles: {", ".join(emojis)}'
        lines = [
            f"- {emoji}: {desc_by_emoji[emoji]}" if emoji in desc_by_emoji else f"- {emoji}"
            for emoji in emojis
        ]
        return 'Optional action "sticker": choose one of these emoji to send after the bubbles:\n' + "\n".join(lines)

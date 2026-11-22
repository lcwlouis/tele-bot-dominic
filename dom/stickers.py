"""Owner-selected Telegram sticker sets, their emoji index, and cached previews."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tomllib
from collections.abc import Callable, Coroutine
from contextlib import nullcontext
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, AsyncContextManager

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from dom.home import Home
from dom.media import MAX_BYTES, PermanentMediaSkip
from dom.usage import ModelCallSkipped

if TYPE_CHECKING:
    from dom.media import MediaDescriber

log = logging.getLogger(__name__)
MAX_SETS = 10
MAX_STICKERS = 120
MAX_EMOJI = 40  # emoji offered in the turn note
MAX_DESCRIPTIONS_PER_PASS = 40  # media-model calls in one background pass; the rest wait for the next pass
MAX_DESCRIPTION_CHARS = 80
MAX_CONSECUTIVE_DESC_FAILURES = 3
DESCRIPTIONS_FILE = "sticker_descriptions.json"
PERMANENT_SKIP = ""  # cached sentinel for stickers that cannot be described (oversize or no preview)
RETRY_AFTER = timedelta(hours=1)  # before a set or transient description failure is tried again
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

    def permanent_skip_reason(self) -> str | None:
        if self.mime != "image/webp" and not self.preview_file_id:
            return "animated or video sticker has no thumbnail"
        if self.file_size is not None and self.file_size > MAX_BYTES["sticker"]:
            return "sticker exceeds size limit"
        return None


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
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task] | None = None,
        request_context: Callable[[], AsyncContextManager[None]] | None = None,
    ):
        self.home, self.bot, self._now = home.scoped(allow_private=False), bot, now
        self._describer = describer
        self._spawn = spawn or asyncio.create_task
        self._request_context = request_context
        self._retry_at: dict[str, datetime] = {}
        self._desc_retry_at: dict[str, datetime] = {}
        self._describer_retry_at: datetime | None = None
        self.by_emoji: dict[str, list[str]] = {}
        self._loaded: dict[str, list[StickerItem]] = {}
        self._variants: dict[str, list[StickerItem]] = {}  # every loaded sticker under its emoji
        self._descriptions: dict[str, str] = {}  # key -> description, or "" for permanent skip
        self._descriptions_loaded = False
        self._describe_task: asyncio.Task | None = None
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
            log.warning("invalid %s; starting with empty sticker descriptions", DESCRIPTIONS_FILE)
            data = {}
        loaded: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue
            if value == PERMANENT_SKIP:
                loaded[key] = PERMANENT_SKIP
            elif (desc := _clean_description(value)):
                loaded[key] = desc
        self._descriptions = loaded

    def _save_descriptions(self) -> None:
        try:
            self.home.write_sticker_descriptions(self._descriptions)
        except OSError:
            log.warning("could not write %s", DESCRIPTIONS_FILE)

    def _rebuild_index(self) -> None:
        variants: dict[str, list[StickerItem]] = {}
        for items in self._loaded.values():
            for item in items:
                variants.setdefault(item.emoji, []).append(item)
        self.by_emoji = {emoji: [item.file_id for item in items] for emoji, items in variants.items()}
        self._variants = variants

    async def _fetch(self, name: str) -> list[StickerItem] | None:
        try:
            result = await self.bot.get_sticker_set(name)
        except TelegramAPIError as error:
            log.warning("could not load sticker set %s (%s)", name, type(error).__name__)
            return None
        return [_sticker_item(sticker) for sticker in result.stickers[:MAX_STICKERS] if sticker.emoji]

    async def _refresh(self, names: list[str]) -> None:
        names = names[:MAX_SETS]
        async with self._lock:
            now = self._now()
            missing = [name for name in names if name not in self._loaded and self._retry_at.get(name, now) <= now]
        fetched = await asyncio.gather(*(self._fetch(name) for name in missing)) if missing else []
        async with self._lock:
            now = self._now()
            for name, items in zip(missing, fetched):
                if items is not None:
                    self._retry_at.pop(name, None)
                    self._loaded[name] = items
                else:
                    self._retry_at[name] = now + RETRY_AFTER
            self._loaded = {name: self._loaded[name] for name in names if name in self._loaded}
            self._rebuild_index()
            if not self._descriptions_loaded:
                self._load_descriptions()

    def _described_candidates(self) -> list[StickerItem]:
        """The stickers of the emoji the turn note offers, each emoji's first sticker before any second one, so a
        capped pass covers every emoji before it covers more variants of one."""
        rows = list(self._variants.values())[:MAX_EMOJI]
        return [item for rank in zip_longest(*rows) for item in rank if item is not None]

    def _pending(self) -> list[StickerItem]:
        if not self._descriptions_loaded:
            self._load_descriptions()
        now = self._now()
        if self._describer_retry_at is not None and self._describer_retry_at > now:
            return []
        seen: set[str] = set()
        pending: list[StickerItem] = []
        for item in self._described_candidates():
            if item.key in seen or item.key in self._descriptions:
                continue
            seen.add(item.key)
            if self._desc_retry_at.get(item.key, now) > now:
                continue
            pending.append(item)
        return pending

    def schedule_descriptions(self) -> int:
        """Start the single background description task if needed. Returns the count of stickers still to describe."""
        describer = self._describer() if self._describer is not None else None
        if describer is None:
            return 0
        pending = self._pending()
        if not pending:
            return 0
        if self._describe_task is None or self._describe_task.done():
            self._describe_task = self._spawn(self._run_descriptions())
        return len(pending)

    async def _run_descriptions(self) -> None:
        consecutive_failures = 0
        calls = 0
        dirty = False
        try:
            while calls < MAX_DESCRIPTIONS_PER_PASS:
                describer = self._describer() if self._describer is not None else None
                if describer is None:
                    return
                async with self._lock:
                    pending = self._pending()
                    item = pending[0] if pending else None
                if item is None:
                    return

                if item.permanent_skip_reason() is not None:
                    async with self._lock:
                        self._descriptions[item.key] = PERMANENT_SKIP
                        self._desc_retry_at.pop(item.key, None)
                        dirty = True
                        self._save_descriptions()
                        dirty = False
                    continue

                calls += 1
                permanent_skip = False
                spend_capped = False
                raw: str | None = None
                try:
                    req = self._request_context() if self._request_context is not None else nullcontext()
                    async with req:
                        raw = await describer.describe_sticker(item.media_dict())
                except PermanentMediaSkip:
                    permanent_skip = True
                except ModelCallSkipped:
                    spend_capped = True
                except Exception as error:
                    log.warning("sticker description failed for %s (%s)", item.key, type(error).__name__)
                    raw = None

                if spend_capped:
                    log.info("stopping sticker description pass: spend cap reached")
                    return

                desc = _clean_description(raw)
                async with self._lock:
                    now = self._now()
                    if permanent_skip:
                        self._descriptions[item.key] = PERMANENT_SKIP
                        self._desc_retry_at.pop(item.key, None)
                        dirty = True
                        self._save_descriptions()
                        dirty = False
                    elif desc:
                        self._descriptions[item.key] = desc
                        self._desc_retry_at.pop(item.key, None)
                        self._describer_retry_at = None
                        consecutive_failures = 0
                        dirty = True
                        self._save_descriptions()
                        dirty = False
                    else:
                        self._desc_retry_at[item.key] = now + RETRY_AFTER
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_DESC_FAILURES:
                            self._describer_retry_at = now + RETRY_AFTER
                            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background sticker description pass failed")
        finally:
            if dirty:
                self._save_descriptions()

    async def refresh(self) -> None:
        """Load the listed sets and schedule background descriptions. Never raises."""
        try:
            async with self._lock:
                try:
                    names = self._names()
                except (OSError, ValueError) as error:
                    log.warning("could not read sticker sets: %s", type(error).__name__)
                    return
            await self._refresh(names)
            async with self._lock:
                self.schedule_descriptions()
        except Exception:
            log.exception("could not refresh stickers")

    async def refresh_descriptions(self) -> int:
        """Clear cached descriptions of the stickers the turn note offers and schedule a background refresh."""
        async with self._lock:
            names = self._names()
        await self._refresh(names)
        async with self._lock:
            if not self._descriptions_loaded:
                self._load_descriptions()
            removed = False
            for item in self._described_candidates():
                if self._descriptions.pop(item.key, None) is not None:
                    removed = True
                self._desc_retry_at.pop(item.key, None)
            self._describer_retry_at = None
            if removed:
                self._save_descriptions()
            return self.schedule_descriptions()

    async def change(self, action: str, name: str) -> str:
        name = set_name(name)
        if action == "add":
            async with self._lock:
                names = self._names()
                if name in names:
                    return f"Sticker set already listed: {name}"
                if len(names) >= MAX_SETS:
                    raise ValueError(f"At most {MAX_SETS} sticker sets can be listed.")
            items = await self._fetch(name)
            async with self._lock:
                if items is None:
                    self._retry_at[name] = self._now() + RETRY_AFTER
                    raise ValueError("Could not load that sticker set from Telegram.")
                names = self._names()
                if name in names:
                    return f"Sticker set already listed: {name}"
                if len(names) >= MAX_SETS:
                    raise ValueError(f"At most {MAX_SETS} sticker sets can be listed.")
                self._retry_at.pop(name, None)
                updated = [*names, name]
                self.home.write_sticker_sets(updated)
                self._loaded[name] = items
                self._loaded = {set_id: self._loaded[set_id] for set_id in updated[:MAX_SETS] if set_id in self._loaded}
                self._rebuild_index()
                pending = self.schedule_descriptions()
            if pending:
                noun = "sticker" if pending == 1 else "stickers"
                return f"Added sticker set {name} (describing {pending} {noun} in the background)"
            return f"Added sticker set {name}"

        async with self._lock:
            names = self._names()
            if name not in names:
                return f"Sticker set is absent: {name}"
            names.remove(name)
            self.home.write_sticker_sets(names)
            self._loaded = {set_id: self._loaded[set_id] for set_id in names[:MAX_SETS] if set_id in self._loaded}
            self._rebuild_index()
            return f"Removed sticker set {name}"

    def sendable(self) -> dict[str, list[str]]:  # see resolve_sticker for how a pick is looked up
        """Sticker choices: every loaded sticker of an emoji under the emoji itself, and each described one of an emoji
        with several stickers also alone under the emoji plus its number (📚2), which is how Dom picks the one whose
        description he read."""
        if not self._descriptions_loaded:
            self._load_descriptions()
        result: dict[str, list[str]] = {}
        for emoji, items in self._variants.items():
            result[emoji] = [item.file_id for item in items]
            if len(items) > 1:
                for number, item in enumerate(items, 1):
                    if self._descriptions.get(item.key):
                        result[f"{emoji}{number}"] = [item.file_id]
        return result

    def turn_note(self) -> str:
        if not self._descriptions_loaded:
            self._load_descriptions()
        emojis = list(self._variants)[:MAX_EMOJI]
        if not emojis:
            return ""
        offered, described_any, numbered = [], False, False
        for emoji in emojis:
            items = self._variants[emoji]
            described = [(n, desc) for n, item in enumerate(items, 1) if (desc := self._descriptions.get(item.key))]
            if not described:
                offered.append(emoji)
                continue
            described_any = True
            if len(items) == 1:
                offered.append(f"{emoji} ({described[0][1]})")
            else:
                numbered = True
                offered.append(f"{emoji} ({'; '.join(f'{emoji}{n} {desc}' for n, desc in described)})")
        note = (
            'Optional action "sticker": choose one of these emoji to send after the bubbles: '
            + ("; " if described_any else ", ").join(offered)
        )
        if numbered:
            note += ('. Put the emoji alone in "sticker" for any of its stickers, or the emoji with its number in "sticker" '
                     "for that exact one; never write these in a bubble")
        return note


_NUMBERED = re.compile(r"^(?P<emoji>\D+?)\s*\d+$")


def plain_emoji(text: str) -> bool:
    """Short text with no letters, digits or spaces: safe to send on its own when a sticker cannot be."""
    return 0 < len(text) <= 8 and not any(ch.isalnum() or ch.isspace() for ch in text)


def resolve_sticker(stickers: Mapping[str, Sequence[str]] | None, raw: str | None) -> tuple[Sequence[str], str] | None:
    """What Dom wrote in "sticker" -> (stickers to pick from, the plain emoji), or None when nothing matches.

    A numbered pick (📚2) that is not on offer (out of range, not described yet) falls back to any sticker of that
    emoji. The emoji returned never carries the number, so it is what gets stored and, failing everything, sent."""
    key = (raw or "").strip()
    if not key:
        return None
    table = stickers or {}
    match = _NUMBERED.match(key)
    base = match["emoji"].strip() if match else key
    if key in table:
        return table[key], base
    if base in table:
        return table[base], base
    return None


def sticker_fallback(raw: str | None) -> str | None:
    """The text to send when a sticker could not be: the bare emoji, never a number or words."""
    key = (raw or "").strip()
    match = _NUMBERED.match(key)
    base = match["emoji"].strip() if match else key
    return base if plain_emoji(base) else None

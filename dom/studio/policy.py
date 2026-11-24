"""Rolling studio quotas and the media actions allowed in each chat."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from dom.config import StudioConfig, StudioFallback
from dom.store import Store
from dom.traits import Preset

Kind = Literal["voice", "video_note", "photo", "special"]
SWITCH = {"voice": "voice", "video_note": "video_notes", "photo": "images", "special": "special"}
STUDIO_NOTE = (
    "Most replies stay text. Use these sparingly when they add something. Never speak private details aloud, "
    "describe the machinery behind these actions, or use media in a private reply. Media goes out only through "
    "these fields: writing [photo] or similar in a bubble sends nothing."
)
UNAVAILABLE_NOTE = (
    "You can't send voice notes, video notes or photos right now. Never write one out as text or promise one; "
    "if asked, say not right now, in your own words."
)
ACTION_NOTES = {
    "voice": '"voice": spoken text (up to 600 characters)',
    "video_note": '"video_note": {"text": spoken text up to 200 characters}',
    "photo": '"image": a scene up to 300 characters (a selfie anywhere, or a scene in cyberspace; leave clothing to the outfit)',
    "special": '"video_note": {"text": spoken text up to 120 characters, "special": true} '
               "for a milestone, birthday or an explicit request",
}


# Each studio switch in config.toml, its plain name, and the kinds it turns on (the profile photo uses the local
# studio's portrait endpoint, so only the studio serves it).
SWITCH_LABELS = {
    "voice": "voice notes", "video_notes": "round video notes", "images": "photos", "special": "special video notes",
    "profile_photo_follows_preset": "the profile photo",
}
SWITCH_KINDS: dict[str, tuple[Kind, ...]] = {
    "voice": ("voice",), "video_notes": ("video_note",), "images": ("photo",), "special": ("special",),
    "profile_photo_follows_preset": (),
}


def backups(config: StudioConfig, kind: Kind) -> tuple[StudioFallback, ...]:
    """The cloud backups that may serve a kind; a Gemini voice runs only on a key confirmed as paid."""
    if kind == "photo":
        return config.image_fallbacks
    if kind == "voice":
        return tuple(p for p in config.voice_fallbacks if p.provider != "gemini_tts" or p.paid)
    return config.video_fallbacks  # everyday and special video notes


def local(config: StudioConfig) -> bool:
    """The local studio is set up: switched on with an address."""
    return config.enabled and bool(config.base_url)


def servable(config: StudioConfig, kind: Kind) -> bool:
    return local(config) or bool(backups(config, kind))


def switch_servable(config: StudioConfig, switch: str, has_key: Callable[[str], bool]) -> bool:
    """Whether anything set up can serve a studio switch: the local studio, or a cloud backup for its kind whose key
    variable holds a value. `has_key` reads .env, so callers run this off the event loop."""
    return local(config) or any(
        has_key(backup.api_key_env) for kind in SWITCH_KINDS[switch] for backup in backups(config, kind))


def unserved(config: StudioConfig, has_key: Callable[[str], bool]) -> list[str]:
    """The switches that are on although nothing set up can serve them; Dom sends text in their place."""
    return [label for switch, label in SWITCH_LABELS.items()
            if getattr(config, switch) and not switch_servable(config, switch, has_key)]


def enabled(config: StudioConfig, scope: str, kind: Kind) -> bool:
    return ((kind != "special" or scope == "owner_dm") and getattr(config, SWITCH[kind])
            and servable(config, kind))


def outfits(config: StudioConfig, preset: Preset | None) -> tuple[str, ...]:
    if preset is not None and preset.outfits:
        return preset.outfits
    return (config.default_outfit,) if config.default_outfit else ()


def turn_note(remaining: dict[Kind, int], wardrobe: tuple[str, ...]) -> str:
    if not remaining:
        return ""
    lines = [STUDIO_NOTE, "At most one of these per reply."]
    for kind, count in remaining.items():
        lines.append(f"Optional action {ACTION_NOTES[kind]} ({count} left in its rolling limit).")
    if wardrobe and any(kind != "voice" for kind in remaining):
        lines.append(f'Optional "outfit" for photos or video notes: {", ".join(wardrobe)}.')
    return "\n".join(lines)


class StudioQuota:
    def __init__(self, store: Store):
        self.store = store
        self._lock = asyncio.Lock()
        self._pending: set[int] = set()

    async def _remaining(self, config: StudioConfig, scope: str, chat_id: int, now: datetime) -> dict[Kind, int]:
        if not any(enabled(config, scope, kind) for kind in SWITCH) or chat_id in self._pending:
            return {}
        tier = getattr(config.limits, scope)
        day, week = timedelta(days=1), timedelta(days=7)
        windows = {
            "voice": (tier.voice_daily, day),
            "video_note": (tier.video_weekly, week) if tier.video_daily is None else (tier.video_daily, day),
            "photo": (tier.image_weekly, week) if tier.image_daily is None else (tier.image_daily, day),
            "special": (tier.special_daily, day),
        }
        total, rows = await self.store.studio_usage(chat_id, now)
        global_left = config.limits.daily - total - len(self._pending)
        gap = timedelta(minutes=tier.gap_minutes)
        if global_left <= 0 or any(stamp > now - gap for _, stamp in rows):
            return {}
        remaining = {}
        for kind, (cap, window) in windows.items():
            count = sum(item == kind and stamp > now - window for item, stamp in rows)
            left = min(cap - count, global_left)
            if left > 0 and enabled(config, scope, kind):
                remaining[kind] = left
        return remaining

    async def remaining(self, config: StudioConfig, scope: str, chat_id: int, now: datetime) -> dict[Kind, int]:
        async with self._lock:
            return await self._remaining(config, scope, chat_id, now)

    @asynccontextmanager
    async def reserve(self, config: StudioConfig, scope: str, chat_id: int, kind: Kind, now: datetime):
        # Reservations cover the request and the Telegram write. The lock covers only the quota lookup.
        async with self._lock:
            accepted = kind in await self._remaining(config, scope, chat_id, now)
            if accepted:
                self._pending.add(chat_id)
        try:
            yield accepted
        finally:
            if accepted:
                self._pending.discard(chat_id)

"""Rolling studio quotas and the media actions allowed in each chat."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from dom.config import StudioConfig
from dom.store import Store
from dom.traits import Preset

Kind = Literal["voice", "video_note", "photo", "special"]
SWITCH = {"voice": "voice", "video_note": "video_notes", "photo": "images", "special": "special"}
STUDIO_NOTE = (
    "Most replies stay text. Use these sparingly when they add something. Never speak private details aloud, "
    "describe the machinery behind these actions, or use media in a private reply."
)
ACTION_NOTES = {
    "voice": '"voice": spoken text (up to 600 characters)',
    "video_note": '"video_note": {"text": spoken text up to 600 characters}',
    "photo": '"image": a scene up to 300 characters (a selfie anywhere, or a scene in cyberspace; leave clothing to the outfit)',
    "special": '"video_note": {"text": spoken text up to 600 characters, "special": true} '
               "for a milestone, birthday or an explicit request",
}


def enabled(config: StudioConfig, scope: str, kind: Kind) -> bool:
    return config.enabled and (kind != "special" or scope == "owner_dm") and getattr(config, SWITCH[kind])


def outfits(config: StudioConfig, preset: Preset | None) -> tuple[str, ...]:
    if preset is not None and preset.outfits:
        return preset.outfits
    return (config.default_outfit,) if config.default_outfit else ()


def turn_note(remaining: dict[Kind, int], wardrobe: tuple[str, ...]) -> str:
    if not remaining:
        return ""
    lines = [STUDIO_NOTE]
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

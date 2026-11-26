"""Rolling studio quotas and the media actions allowed in each chat."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor
from typing import TYPE_CHECKING, Literal

from dom.config import StudioConfig, StudioFallback, StudioLimits, StudioTier
from dom.store import Store
from dom.traits import Preset

if TYPE_CHECKING:
    from dom.studio.events import EventDays

Kind = Literal["voice", "video_note", "photo", "special"]
SWITCH = {"voice": "voice", "video_note": "video_notes", "photo": "images", "special": "special"}
STUDIO_NOTE = (
    "Most replies stay text. Use these sparingly when they add something. Never speak private details aloud, "
    "describe the machinery behind these actions, or use media in a private reply. These are fields of your final "
    "JSON answer, next to \"bubbles\": they are not tools, so never call a tool named image, photo, voice or "
    "video_note. Writing [photo] or similar in a bubble sends nothing."
)
UNAVAILABLE_NOTE = (
    "You can't send voice notes, video notes or photos right now. Never write one out as text or promise one; "
    "if asked, say not right now, in your own words."
)
ACTION_NOTES = {
    "voice": '"voice": spoken text (up to 600 characters)',
    "video_note": '"video_note": {"text": spoken text up to 200 characters, "scene": where you are and what is around you, '
                  'up to 200 characters; each note is filmed in a fresh scene}',
    "photo": '"image": a scene up to 300 characters (a selfie anywhere, or a scene in cyberspace; leave clothing to the outfit)',
    "special": '"video_note": {"text": spoken text up to 120 characters, "scene": as above, "special": true} '
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


def turn_note(remaining: dict[Kind, int], wardrobe: tuple[str, ...], event: str = "") -> str:
    if not remaining:
        return ""
    lines = [STUDIO_NOTE, "At most one of these per reply."]
    if event:
        lines.append(event)
    for kind, count in remaining.items():
        lines.append(f"Optional action {ACTION_NOTES[kind]} ({count} left in its rolling limit).")
    if wardrobe and any(kind != "voice" for kind in remaining):
        lines.append(f'Optional "outfit" for photos or video notes: {", ".join(wardrobe)}.')
    return "\n".join(lines)


GAP_FIELDS: dict[Kind, str] = {
    "voice": "voice_gap_minutes", "video_note": "video_gap_minutes", "photo": "image_gap_minutes",
    "special": "special_gap_minutes",
}


@dataclass(frozen=True)
class Standing:
    """Where a chat stands against its studio limits: what is left of each kind (counting the global daily total, but
    not the gaps or the switches), how much of the global total is left, when each kind's gap ends if it hasn't, and
    the part of `left` that is only there because of an event day (the item would pass the normal limits)."""

    left: dict[Kind, int]
    daily_left: int
    gaps: dict[Kind, datetime]
    normal: dict[Kind, int]  # what is left under the normal limits alone, with no event day
    used: dict[Kind, int] = field(default_factory=dict)  # this chat's items of each kind inside its rolling window

    @property
    def next_at(self) -> datetime | None:
        """When every kind is free of its gap, if any gap is running."""
        return max(self.gaps.values(), default=None)


def windows(tier: StudioTier) -> dict[Kind, tuple[int, timedelta]]:
    """Each kind's cap and rolling window; a daily video or image limit wins when set."""
    day, week = timedelta(days=1), timedelta(days=7)
    return {
        "voice": (tier.voice_daily, day),
        "video_note": (tier.video_weekly, week) if tier.video_daily is None else (tier.video_daily, day),
        "photo": (tier.image_weekly, week) if tier.image_daily is None else (tier.image_daily, day),
        "special": (tier.special_daily, day),
    }


def gap_minutes(tier: StudioTier, kind: Kind) -> int:
    own = getattr(tier, GAP_FIELDS[kind])
    return tier.gap_minutes if own is None else own


def gap_ends(tier: StudioTier, rows: list[tuple[str, datetime]], now: datetime) -> dict[Kind, datetime]:
    """When each kind's minimum gap ends, for the kinds still waiting. A kind with a gap of its own is spaced only from
    its own last item. The kinds without one share the general `gap_minutes` and are spaced from each other, so a
    config that sets no per-kind gap behaves as it always did: any media holds back the next."""
    shared = {kind for kind in GAP_FIELDS if getattr(tier, GAP_FIELDS[kind]) is None}
    ends = {}
    for kind in GAP_FIELDS:
        lane = shared if kind in shared else {kind}
        last = max((stamp for item, stamp in rows if item in lane), default=None)
        if last is not None and (end := last + timedelta(minutes=gap_minutes(tier, kind))) > now:
            ends[kind] = end
    return ends


def boosted(limit: int, boost: float) -> int:
    """A limit under an event day's multiplier: rounded down, so it never passes the owner's ceiling, and never below
    the normal limit."""
    return max(limit, floor(limit * boost + 1e-9))


def standing(
    limits: StudioLimits, scope: str, total: int, rows: list[tuple[str, datetime]], now: datetime,
    boost: float = 1.0,
) -> Standing:
    """`total` is the studio items sent across all chats in the last 24 hours (plus any in flight); `rows` is this
    chat's rolling week of deliveries. `boost` is the event-day multiplier on every limit (1 when there is none): `left`
    counts what the boosted limits allow, `normal` what the usual ones do."""
    tier = getattr(limits, scope)
    daily_normal, daily_boosted = max(limits.daily - total, 0), max(boosted(limits.daily, boost) - total, 0)
    left, normal, used = {}, {}, {}
    for kind, (cap, window) in windows(tier).items():
        count = used[kind] = sum(item == kind and stamp > now - window for item, stamp in rows)
        normal[kind] = max(min(cap - count, daily_normal), 0)
        left[kind] = max(min(boosted(cap, boost) - count, daily_boosted), 0)
    return Standing(left, daily_boosted, gap_ends(tier, rows, now), normal, used)


class StudioQuota:
    def __init__(self, store: Store, events: EventDays | None = None):
        self.store = store
        self.events = events
        self._lock = asyncio.Lock()
        self._pending: set[int] = set()

    async def _boost(self, now: datetime) -> float:
        return 1.0 if self.events is None else await self.events.multiplier(now)

    async def _remaining(self, config: StudioConfig, scope: str, chat_id: int, now: datetime) -> dict[Kind, int]:
        if not any(enabled(config, scope, kind) for kind in SWITCH) or chat_id in self._pending:
            return {}
        total, rows = await self.store.studio_usage(chat_id, now)
        now_standing = standing(config.limits, scope, total + len(self._pending), rows, now, await self._boost(now))
        result = {}
        for kind, left in now_standing.left.items():
            if left <= 0 or kind in now_standing.gaps or not enabled(config, scope, kind):
                continue
            # Past its normal limit, an item (an event day's extra) also has to fit the month's unused budget.
            if now_standing.normal[kind] <= 0 and not (self.events is not None and await self.events.may_extra(kind)):
                continue
            result[kind] = left
        return result

    def sending(self, chat_id: int) -> bool:
        """A studio item for this chat is reserved and on its way."""
        return chat_id in self._pending

    async def standing(self, config: StudioConfig, scope: str, chat_id: int, now: datetime) -> Standing:
        """Where a chat stands now, for the dashboard. It reserves nothing and takes no lock: a reply in flight still
        counts toward the global total."""
        total, rows = await self.store.studio_usage(chat_id, now)
        return standing(config.limits, scope, total + len(self._pending), rows, now, await self._boost(now))

    async def has_room_for_still(self, config: StudioConfig, scope: str, chat_id: int, now: datetime) -> bool:
        """After this chat's reserved video note, is there still a slot in the daily total for the still it is filmed
        from? Without one the note animates the portrait instead."""
        async with self._lock:
            total, rows = await self.store.studio_usage(chat_id, now)
            return standing(
                config.limits, scope, total + len(self._pending), rows, now, await self._boost(now),
            ).daily_left > 0

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

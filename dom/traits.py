"""Canonical personality sliders, the global and per-chat presets and reflection drift."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from dom.home import Home, PrivateAccessError

if TYPE_CHECKING:
    from dom.store import Store

log = logging.getLogger(__name__)
NAMES = ("sarcasm", "playfulness", "humor", "formality", "empathy", "enthusiasm", "slang", "emoji")
MAX_OVERLAY = 800
GLOBAL_PRESET_KEY = "preset:global"


def numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _name(key: object) -> str | None:
    """The canonical slider name for a front matter or form key, or None when it names no slider."""
    if not isinstance(key, str):
        return None
    name = key.removesuffix("_level")
    name = "slang" if name == "singlish" else name
    return name if name in NAMES else None


def normalise(raw: dict) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        number = numeric(value)
        if (name := _name(key)) is not None and number is not None:
            result[name] = min(0.95, max(0.05, number))
    return result


def validate_overrides(raw: dict) -> dict[str, float] | None:
    """Per-chat slider edits from the dashboard: normalise them, or None when any key or value is not a slider."""
    if not isinstance(raw, dict) or not raw:
        return None
    if any(_name(key) is None or numeric(value) is None for key, value in raw.items()):
        return None
    return normalise(raw)


def decode(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    description: str
    traits: dict[str, float]
    overlay: str


def load_preset(home: Home, name: str) -> Preset | None:
    try:
        item = home.preset(name)
    except (OSError, PrivateAccessError, ValueError):
        item = None
    if item is None:
        log.warning("unknown preset %r; ignoring it", name)
        return None
    meta, body = item
    overlay = body.strip()
    if len(overlay) > MAX_OVERLAY:
        log.warning("preset %r overlay truncated to %s characters", name, MAX_OVERLAY)
    return Preset(
        name, str(meta.get("title") or name), str(meta.get("description") or ""),
        normalise(meta.get("traits", {})), overlay[:MAX_OVERLAY],
    )


async def for_chat(home: Home, store: Store, chat_id: int) -> tuple[dict[str, float], Preset | None]:
    meta, _ = home.scoped(allow_private=False).soul()
    sliders = normalise(meta.get("traits", {}))
    drift = decode(await store.kv_get("traits:drift"))
    sliders = normalise({name: value + (numeric(drift.get(name)) or 0) for name, value in sliders.items()})
    name, overrides = await store.get_chat_personality(chat_id)
    everywhere = await global_preset_name(store)
    shared = load_preset(home, everywhere) if everywhere else None
    own = load_preset(home, name) if name else None
    for layer in (shared, own):
        if layer is not None:
            sliders.update(layer.traits)
    sliders.update(normalise(decode(overrides)))
    return sliders, own or shared


async def global_preset_name(store: Store) -> str:
    return await store.kv_get(GLOBAL_PRESET_KEY) or ""


async def effective(home: Home, store: Store, chat_id: int) -> dict[str, float]:
    sliders, _ = await for_chat(home, store, chat_id)
    return sliders

"""Canonical personality sliders, per-chat presets and reflection drift."""

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


def numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def normalise(raw: dict) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        name = key.removesuffix("_level")
        name = "slang" if name == "singlish" else name
        number = numeric(value)
        if name in NAMES and number is not None:
            result[name] = min(0.95, max(0.05, number))
    return result


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
    preset = load_preset(home, name) if name else None
    if preset is not None:
        sliders.update(preset.traits)
    sliders.update(normalise(decode(overrides)))
    return sliders, preset


async def effective(home: Home, store: Store, chat_id: int) -> dict[str, float]:
    sliders, _ = await for_chat(home, store, chat_id)
    return sliders

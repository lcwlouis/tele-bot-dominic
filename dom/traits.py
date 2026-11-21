"""Canonical personality sliders, the global and per-chat presets and reflection drift."""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import TYPE_CHECKING

from dom.home import Home, PrivateAccessError

if TYPE_CHECKING:
    from dom.store import Store

log = logging.getLogger(__name__)
NAMES = ("sarcasm", "playfulness", "humor", "formality", "empathy", "enthusiasm", "slang", "emoji")
MAX_OVERLAY = 800
GLOBAL_PRESET_KEY = "preset:global"
# Everywhere can be "auto": Dom picks his own preset each day. The picks are kept as JSON in the kv store.
AUTO = "auto"
AUTO_PICKS_KEY = "preset:auto"
ASSISTANT = "assistant"  # task-driven, never a vibe
# Task mode: in the owner's DM Dom wears the assistant preset for an important task. It is one kv entry that expires
# after a quiet stretch, kept apart from the daily pick so it never touches `switch_vibe` or the pick.
TASK_KEY = "preset:task"
TASK_QUIET = timedelta(minutes=30)
MAX_REASON = 160
# Odds for the random fallback: the base preset most often, a joke preset rarely, any other preset in between.
RANDOM_WEIGHTS = {"classic": 6.0, "fboy": 1.0}
DEFAULT_WEIGHT = 3.0


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
    picked: str = ""  # why Dom picked it himself today; empty when somebody set it


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


@dataclass(frozen=True)
class Vibe:
    """The preset used everywhere right now and who chose it: `auto` (Dom), `locked` (the owner) or `none`."""

    mode: str
    preset: Preset | None = None
    reason: str = ""
    source: str = ""  # auto only: "reflection", "random" or "switch"
    since: datetime | None = None


async def for_chat(home: Home, store: Store, chat_id: int, now: datetime | None = None) -> tuple[dict[str, float], Preset | None]:
    meta, _ = home.scoped(allow_private=False).soul()
    sliders = normalise(meta.get("traits", {}))
    drift = decode(await store.kv_get("traits:drift"))
    sliders = normalise({name: value + (numeric(drift.get(name)) or 0) for name, value in sliders.items()})
    name, overrides = await store.get_chat_personality(chat_id)
    shared = (await vibe(home, store, now)).preset
    own = load_preset(home, name) if name else None
    if not name and await task_mode(store, chat_id, now) is not None:  # a pinned preset always wins
        shared = load_preset(home, ASSISTANT) or shared
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


# ---- Dom's own daily pick (`auto`) --------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def vibe_choices(home: Home) -> list[Preset]:
    """The presets Dom may pick for himself: every installed one that loads, except the assistant preset."""
    return [
        preset for name in home.preset_names()
        if name not in (ASSISTANT, AUTO) and (preset := load_preset(home, name)) is not None
    ]


def random_choice(choices: list[Preset], rng: random.Random | None = None) -> Preset | None:
    weights = [RANDOM_WEIGHTS.get(preset.name, DEFAULT_WEIGHT) for preset in choices]
    return (rng or random).choices(choices, weights)[0] if choices else None


def clean_reason(value: object) -> str:
    return " ".join(value.split())[:MAX_REASON] if isinstance(value, str) else ""


async def _picks(store: Store) -> list[dict]:
    """Stored picks, oldest first: {"name", "reason", "source", "since"} with `since` as a datetime."""
    try:
        raw = json.loads(await store.kv_get(AUTO_PICKS_KEY) or "[]")
    except ValueError:
        return []
    picks = []
    for item in raw if isinstance(raw, list) else []:
        try:
            since = datetime.fromisoformat(item["since"])
            ok = isinstance(item["name"], str) and isinstance(item["reason"], str) and isinstance(item["source"], str)
        except (KeyError, TypeError, ValueError):
            continue
        if ok:
            picks.append({**item, "since": since if since.tzinfo else since.replace(tzinfo=timezone.utc)})
    return sorted(picks, key=lambda pick: pick["since"])


def _in_force(picks: list[dict], now: datetime) -> dict | None:
    return next((pick for pick in reversed(picks) if pick["since"] <= now), None)


async def record_pick(
    store: Store, preset: Preset, reason: str, source: str, *, since: datetime | None = None, now: datetime | None = None,
) -> None:
    """Remember a pick that takes effect at `since` (default: now). Picks older than the one in force are dropped."""
    now = now or _utc_now()
    picks = await _picks(store)
    picks.append({"name": preset.name, "reason": clean_reason(reason), "source": source, "since": since or now})
    picks.sort(key=lambda pick: pick["since"])
    keep = picks.index(_in_force(picks, now) or picks[0])
    await store.kv_set(AUTO_PICKS_KEY, json.dumps([{**pick, "since": pick["since"].isoformat()} for pick in picks[keep:]]))


async def ensure_pick(home: Home, store: Store, now: datetime | None = None) -> bool:
    """When Everywhere becomes `auto` with no pick from the last day, pick at random now. False if nothing can be picked."""
    now = now or _utc_now()
    current = _in_force(await _picks(store), now)
    if current is not None and now - current["since"] < timedelta(days=1) and load_preset(home, current["name"]):
        return True
    chosen = random_choice(vibe_choices(home))
    if chosen is None:
        return False
    await record_pick(store, chosen, "Chosen at random.", "random", now=now)
    return True


async def vibe(home: Home, store: Store, now: datetime | None = None) -> Vibe:
    name = await global_preset_name(store)
    if name != AUTO:
        return Vibe("locked" if name else "none", load_preset(home, name) if name else None)
    pick = _in_force(await _picks(store), now or _utc_now())
    if pick is None:
        return Vibe("auto")
    preset = load_preset(home, pick["name"])
    if preset is not None:
        preset = replace(preset, picked=pick["reason"] or "your own pick")
    return Vibe("auto", preset, pick["reason"], pick["source"], pick["since"])


class SwitchRefused(ValueError):
    """Why Dom cannot change his vibe right now, in words he can act on."""


async def switch_vibe(home: Home, store: Store, chat_id: int, name: object, reason: object, now: datetime) -> Preset:
    """Dom's one extra switch of the day. Only in `auto`, only in a chat without its own preset, once per pick."""
    current = await vibe(home, store, now)
    if current.mode != "auto":
        raise SwitchRefused("the owner has fixed your preset, so you cannot switch")
    if (await store.get_chat_personality(chat_id))[0]:
        raise SwitchRefused("this chat has its own preset, so switching would change nothing here")
    if current.source == "switch":
        raise SwitchRefused("you already switched today; wait for tomorrow's pick")
    reason = clean_reason(reason)
    if not reason:
        raise SwitchRefused("give a short reason")
    chosen = next((preset for preset in vibe_choices(home) if preset.name == name), None)
    if chosen is None:
        raise SwitchRefused(f"choose one of: {', '.join(preset.name for preset in vibe_choices(home))}")
    if current.preset is not None and current.preset.name == chosen.name:
        raise SwitchRefused("that is already your vibe")
    await record_pick(store, chosen, reason, "switch", now=now)
    return chosen


# ---- task mode (assistant preset for an important task, owner DM only) -------------------------

@dataclass(frozen=True)
class TaskMode:
    reason: str
    since: datetime
    until: datetime  # moves forward while the owner keeps talking


class TaskRefused(ValueError):
    """Why Dom cannot go into task mode, in words he can act on."""


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def task_mode(store: Store, chat_id: int, now: datetime | None = None) -> TaskMode | None:
    """The task mode in force right now in this chat. Only the owner's DM can be in it, whatever the stored state."""
    if store.scope_for(chat_id, "private") != "owner_dm":
        return None
    raw = decode(await store.kv_get(TASK_KEY))
    try:
        task = TaskMode(str(raw["reason"]), _aware(datetime.fromisoformat(raw["since"])), _aware(datetime.fromisoformat(raw["until"])))
    except (KeyError, TypeError, ValueError):
        return None
    return task if task.until > (now or _utc_now()) else None


async def _save_task(store: Store, task: TaskMode) -> None:
    await store.kv_set(TASK_KEY, json.dumps({"reason": task.reason, "since": task.since.isoformat(), "until": task.until.isoformat()}))


async def start_task_mode(home: Home, store: Store, chat_id: int, reason: object, now: datetime) -> TaskMode:
    """Put the owner's DM into assistant mode, or keep it there and push its end back. Never in another chat, and
    never over a preset the owner pinned on the chat."""
    if store.scope_for(chat_id, "private") != "owner_dm":
        raise TaskRefused("task mode is only for the owner's DM")
    if (await store.get_chat_personality(chat_id))[0]:
        raise TaskRefused("this chat has its own preset, so task mode would change nothing here")
    if load_preset(home, ASSISTANT) is None:
        raise TaskRefused("there is no assistant preset installed")
    reason = clean_reason(reason)
    if not reason:
        raise TaskRefused("give a short reason")
    current = await task_mode(store, chat_id, now)
    task = TaskMode(reason, current.since if current else now, now + TASK_QUIET)
    await _save_task(store, task)
    return task


async def keep_task_mode(store: Store, task: TaskMode, now: datetime) -> None:
    """The owner is still talking: the quiet period starts again."""
    await _save_task(store, replace(task, until=now + TASK_QUIET))


async def end_task_mode(store: Store, chat_id: int, now: datetime) -> bool:
    """Back to the vibe in force. False when task mode was not on."""
    if await task_mode(store, chat_id, now) is None:
        return False
    await store.kv_set(TASK_KEY, "")
    return True

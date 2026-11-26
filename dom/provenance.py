"""Where each kept photo, voice note and video note came from, and what it cost.

While a media job runs, the executor opens a `Trace`. Every studio call made for the job adds a step to it: the local
studio (no usage row, so it costs nothing) or a cloud backup (the usage row that records its cost). When the item is
sent, the trace becomes `media_json["made"]`: which steps were the item itself, its voice or its scene still, which
were a look-before-send redo, plus the outfit, preset, generation time and length.

Dollars are never stored. `describe` adds up the linked usage rows when the library is read, so a cost OpenRouter
reports minutes later (the background cost fetch) shows once it lands, and until then reads as pending.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

LOCAL_HOST = "local"
ITEM, VOICE, STILL = "item", "voice", "still"  # what a step made: the media itself, a video note's voice, its scene still


class Trace:
    def __init__(self, steps: list[dict] | None = None):
        self.steps: list[dict] = steps if steps is not None else []
        self.kept_redo: set[str] = set()  # parts whose look-before-send redo was the one kept

    def made(self, *, outfit: str | None, preset: str | None, seconds: float, duration: float | None) -> dict:
        made: dict[str, Any] = {"steps": self.steps, "seconds": round(seconds, 1)}
        for key, value in (("outfit", outfit), ("preset", preset), ("duration", duration)):
            if value is not None:
                made[key] = value
        if self.kept_redo:
            made["kept_redo"] = sorted(self.kept_redo)
        return made


_trace: ContextVar[Trace | None] = ContextVar("media_trace", default=None)
_part: ContextVar[str] = ContextVar("media_part", default=ITEM)
_redo: ContextVar[bool] = ContextVar("media_redo", default=False)


def current() -> Trace | None:
    return _trace.get()


@contextmanager
def tracing(seed: list[dict] | None = None) -> Iterator[Trace]:
    trace = Trace(list(seed) if seed else None)
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


@contextmanager
def part(name: str) -> Iterator[None]:
    token = _part.set(name)
    try:
        yield
    finally:
        _part.reset(token)


@contextmanager
def redoing() -> Iterator[None]:
    """Calls made inside are a look-before-send redo."""
    token = _redo.set(True)
    try:
        yield
    finally:
        _redo.reset(token)


def kept_redo() -> None:
    """The redo, not the first try, is what was sent."""
    if (trace := _trace.get()) is not None:
        trace.kept_redo.add(_part.get())


def note(kind: str, model: str, host: str, usage_id: int | None) -> None:
    """One studio call for the job in progress; a no-op outside a trace. `usage_id` is None for the local studio."""
    if (trace := _trace.get()) is None:
        return
    step: dict[str, Any] = {"part": _part.get(), "kind": kind, "model": model, "host": host, "usage_id": usage_id}
    if _redo.get():
        step["redo"] = True
    trace.steps.append(step)


def discard(usage_id: int) -> None:
    """A call whose usage row was deleted (the provider never accepted it) was never made."""
    if (trace := _trace.get()) is not None:
        trace.steps[:] = [step for step in trace.steps if step.get("usage_id") != usage_id]


def usage_ids(made: object) -> list[int]:
    steps = made.get("steps") if isinstance(made, dict) else None
    return [step["usage_id"] for step in steps or [] if isinstance(step, dict) and type(step.get("usage_id")) is int]


def _cost(steps: list[dict], costs: dict[int, float | None]) -> dict:
    """The money behind some steps. Local steps are free. A cloud step whose row has no cost yet (None) is pending;
    one settled at $0 is final; one whose row is gone is left out."""
    total, pending = 0.0, False
    for step in steps:
        if step.get("host") == LOCAL_HOST:
            continue
        row = step.get("usage_id")
        if row not in costs:
            continue
        pending = pending or costs[row] is None
        total += costs[row] or 0.0
    return {"cost": round(total, 6), "pending": pending}


def _source(step: dict | None) -> str | None:
    return None if step is None else "local" if step.get("host") == LOCAL_HOST else "cloud"


def describe(made: object, costs: dict[int, float | None]) -> dict | None:
    """What the library shows for one item: from its stored `made` and the current cost of its usage rows."""
    if not isinstance(made, dict) or not isinstance(made.get("steps"), list):
        return None
    steps = [step for step in made["steps"] if isinstance(step, dict)]
    redone = set(made.get("kept_redo") or ())

    def side(name: str) -> dict | None:
        mine = [step for step in steps if step.get("part") == name]
        first, redo = [s for s in mine if not s.get("redo")], [s for s in mine if s.get("redo")]
        # the last try made the thing: an earlier cloud backup that failed still cost money, so it still counts
        made_by = (redo[-1] if name in redone and redo else first[-1] if first else redo[-1] if redo else None)
        if made_by is None:
            return None
        return {
            "source": _source(made_by), "model": made_by.get("model"),
            "host": None if made_by.get("host") == LOCAL_HOST else made_by.get("host"),
            **_cost(mine, costs),
            "redo": None if not redo else {
                **_cost(redo, costs), "kept": "second" if name in redone else "first",
                "free": all(step.get("host") == LOCAL_HOST for step in redo),
            },
            "free": all(step.get("host") == LOCAL_HOST for step in mine),
        }

    item = side(ITEM)
    if item is None:
        return None
    voice, still = side(VOICE), side(STILL)
    total = _cost(steps, costs)
    return {
        **item, **total, "free": all(step.get("host") == LOCAL_HOST for step in steps),
        "outfit": made.get("outfit"), "preset": made.get("preset"), "seconds": made.get("seconds"),
        "duration": made.get("duration"), "voice": voice, "still": still,
    }


def ogg_seconds(data: bytes) -> float | None:
    """Length of an Ogg Opus voice note: the last page's granule position at 48 kHz, less the pre-skip."""
    end = data.rfind(b"OggS")
    if end < 0 or len(data) < end + 14 or not data.startswith(b"OggS"):
        return None
    granule = struct.unpack_from("<q", data, end + 6)[0]
    head = data.find(b"OpusHead")
    skip = struct.unpack_from("<H", data, head + 10)[0] if head >= 0 and len(data) >= head + 12 else 0
    return round((granule - skip) / 48000, 1) if granule > skip else None


def mp4_seconds(data: bytes) -> float | None:
    """Length of an MP4 from its movie header (`mvhd`)."""
    at = data.find(b"mvhd")
    if at < 0 or len(data) < at + 24:
        return None
    version = data[at + 4]
    if version == 1 and len(data) >= at + 36:
        scale, length = struct.unpack_from(">IQ", data, at + 24)
    else:
        scale, length = struct.unpack_from(">II", data, at + 16)
    return round(length / scale, 1) if scale and length else None


def seconds_of(kind: str, data: bytes) -> float | None:
    try:
        return ogg_seconds(data) if kind == "voice" else mp4_seconds(data) if kind != "photo" else None
    except (struct.error, ValueError):
        return None

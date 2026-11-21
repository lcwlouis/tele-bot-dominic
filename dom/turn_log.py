"""Capture outcome and model metadata for one dispatch without storing prompts or replies."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

from dom.usage import ModelCallSkipped

if TYPE_CHECKING:
    from datetime import datetime

    from dom.models import Scope
    from dom.store import Store

log = logging.getLogger(__name__)


@dataclass
class TurnTrace:
    steps: list[str] = field(default_factory=list)
    timings: list[str] = field(default_factory=list)  # "<what>@<milliseconds>", in the order the steps ran
    calls: int = 0
    model: str = ""
    fallback: bool = False
    outcome: str = ""


_current: ContextVar[TurnTrace | None] = ContextVar("turn_trace", default=None)


def step(name: str) -> None:
    if (trace := _current.get()) is not None:
        trace.steps.append(name)


@contextmanager
def timed(what: str):
    """Record how long the block took, as one more entry of the turn's trace ("model@2713"). Steps never contain
    "@", so the trace stays a list of strings and needs no migration."""
    trace = _current.get()
    start = monotonic()
    try:
        yield
    finally:
        if trace is not None:
            trace.timings.append(f"{what}@{round((monotonic() - start) * 1000)}")


def attempt(model: str, kind: str, *, fallback: bool = False) -> None:
    if (trace := _current.get()) is not None and kind in ("chat", "triage", "guard"):
        trace.calls += 1
        if kind == "chat" or not trace.model:
            trace.model = model
        trace.fallback |= fallback
        if kind != "chat":
            trace.steps.append(kind)


def outcome(text: str) -> None:
    if (trace := _current.get()) is not None:
        trace.outcome = text


@asynccontextmanager
async def capture(store: Store, at: datetime, chat_id: int, scope: Scope):
    trace = TurnTrace()
    start = monotonic()
    token = _current.set(trace)
    try:
        yield
    except ModelCallSkipped:
        trace.outcome = "Paused"
        raise
    except asyncio.CancelledError:  # a restart or shutdown mid-turn; the unread messages are picked up again after it
        trace.outcome = "Interrupted"
        raise
    except Exception:
        trace.outcome = "Turn failed"
        raise
    finally:
        _current.reset(token)
        if trace.steps or trace.calls:
            try:
                await store.record_turn(
                    at, chat_id, scope, trace.calls, [*trace.steps, *trace.timings], trace.model, trace.fallback,
                    round((monotonic() - start) * 1000), trace.outcome or "Stayed quiet",
                )
            except Exception:
                log.exception("could not save turn metadata in chat %s", chat_id)

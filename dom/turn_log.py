"""Capture outcome and model metadata for one dispatch without storing prompts or replies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

from dom.usage import ModelCallSkipped, response_usage, token_counts

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    import httpx

    from dom.models import Scope
    from dom.store import Store

log = logging.getLogger(__name__)


def milliseconds(elapsed: timedelta) -> int:
    return max(0, round(elapsed.total_seconds() * 1000))


def prompt_hash(value: object, *, digest_size: int = 8) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return hashlib.blake2s(text.encode(), digest_size=digest_size).hexdigest()


@dataclass
class ModelCall:
    kind: str
    model: str
    fallback: bool
    system_hash: str
    tools_hash: str
    user_hash: str
    user_prefix_hash: str | None
    duration_ms: int = 0
    status: int | str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    def received(self, response: httpx.Response) -> None:
        self.status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            return
        self.input_tokens, self.output_tokens, self.cached_tokens = token_counts(response_usage(payload))


@dataclass
class TurnTrace:
    steps: list[str] = field(default_factory=list)
    timings: list[str] = field(default_factory=list)  # "<what>@<milliseconds>", in the order the steps ran
    calls: int = 0
    model: str = ""
    fallback: bool = False
    outcome: str = ""
    model_calls: list[ModelCall] = field(default_factory=list)
    message_at: datetime | None = None
    now: Callable[[], datetime] | None = None
    first_bubble_ms: int | None = None
    last_bubble_ms: int | None = None

    def add_timing(self, what: str, elapsed_ms: int) -> None:
        self.timings.append(f"{what}@{elapsed_ms}")


_current: ContextVar[TurnTrace | None] = ContextVar("turn_trace", default=None)


@contextmanager
def track(trace: TurnTrace):
    token = _current.set(trace)
    try:
        yield
    finally:
        _current.reset(token)


def step(name: str) -> None:
    if (trace := _current.get()) is not None:
        trace.steps.append(name)


@contextmanager
def timed(what: str):
    """Record how long the block took in the existing what@ms trace."""
    trace = _current.get()
    start = monotonic()
    try:
        yield
    finally:
        if trace is not None:
            trace.add_timing(what, round((monotonic() - start) * 1000))


@contextmanager
def model_call(model: str, kind: str, messages: list[dict], tools: list[dict], *, fallback: bool = False):
    """Record one provider attempt, even if transport fails or the attempt is cancelled."""
    trace = _current.get()
    if trace is None or kind not in ("chat", "triage", "guard", "media", "search"):
        yield None
        return

    users = [m for m in messages if m.get("role") == "user"]
    first = users[0].get("content") if users else None
    head, marker, _ = first.partition("<chat_window") if isinstance(first, str) else ("", "", "")
    call = ModelCall(
        kind, model, fallback,
        prompt_hash([m for m in messages if m.get("role") in ("system", "developer")]),
        prompt_hash(tools),
        prompt_hash(users),
        prompt_hash(head) if marker else None,
    )
    trace.model_calls.append(call)
    trace.calls += 1
    if kind == "chat" or not trace.model:
        trace.model = model
    trace.fallback |= fallback
    if kind in ("triage", "guard"):
        trace.steps.append(kind)

    start = monotonic()
    try:
        yield call
    except BaseException as exc:
        call.status = type(exc).__name__
        raise
    finally:
        call.duration_ms = round((monotonic() - start) * 1000)
        if kind == "chat":
            trace.add_timing("fallback_model" if fallback else "model", call.duration_ms)
        elif kind in ("triage", "guard"):
            trace.add_timing(kind, call.duration_ms)
        # Media and search run inside stages that already account for their wall time.


def bubble_sent() -> None:
    trace = _current.get()
    if trace is not None and trace.message_at is not None and trace.now is not None:
        elapsed = milliseconds(trace.now() - trace.message_at)
        if trace.first_bubble_ms is None:
            trace.first_bubble_ms = elapsed
        trace.last_bubble_ms = max(trace.last_bubble_ms or 0, elapsed)


def outcome(text: str) -> None:
    if (trace := _current.get()) is not None:
        trace.outcome = text


@asynccontextmanager
async def capture(
    store: Store, at: datetime, chat_id: int, scope: Scope, *, trace: TurnTrace | None = None,
):
    if trace is None:
        trace = TurnTrace()
    start = monotonic()
    try:
        with track(trace):
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
        if trace.steps or trace.calls:
            try:
                await store.record_turn(
                    at, chat_id, scope, trace.calls, [*trace.steps, *trace.timings], trace.model, trace.fallback,
                    round((monotonic() - start) * 1000), trace.outcome or "Stayed quiet",
                    model_calls=[asdict(call) for call in trace.model_calls],
                    first_bubble_ms=trace.first_bubble_ms, last_bubble_ms=trace.last_bubble_ms,
                )
            except Exception:
                log.exception("could not save turn metadata in chat %s", chat_id)

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

    from dom.attention import Attention, Entry
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
    user_hash: str | None  # None for media and search: their user turn carries the attachment itself
    user_prefix_hash: str | None
    duration_ms: int = 0
    status: int | str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    host: str = ""
    gap_ms: int | None = None
    between_tokens: int | None = None

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
    superseded: bool = False  # the next attempt carries this trace forward
    row_id: int | None = None  # the saved turn row, so work done after the turn can be added to it

    def add_timing(self, what: str, elapsed_ms: int) -> None:
        self.timings.append(f"{what}@{elapsed_ms}")


_current: ContextVar[TurnTrace | None] = ContextVar("turn_trace", default=None)
_inside_timed: ContextVar[bool] = ContextVar("inside_timed", default=False)  # a timed() block already counts nested calls
_VISIT_STEP = "attention:"
_visit: ContextVar[tuple[Attention, int, Callable[[], datetime], TurnTrace] | None] = ContextVar(
    "attention_visit", default=None,
)


@contextmanager
def attention_visit(
    attention: Attention, entry: Entry, started: datetime, now: Callable[[], datetime], *, wait_ms: int = 0,
):
    trace = _current.get()
    if trace is None:
        yield
        return
    token = _visit.set((attention, entry.chat_id, now, trace))
    try:
        yield
    finally:
        _visit.reset(token)
        trace.steps.append(_VISIT_STEP + json.dumps({
            "chat_id": entry.chat_id, "at": started.isoformat(), "duration_ms": milliseconds(now() - started),
            "lane": entry.lane, "wait_ms": wait_ms,
            "reason": entry.reason, "behind": entry.behind, "depth": entry.depth, "capped": entry.capped,
        }))


def visits_in(trace: list[str]) -> list[dict]:
    return [json.loads(step[len(_VISIT_STEP):]) for step in trace if step.startswith(_VISIT_STEP)]


def visible_steps(trace: list[str]) -> list[str]:
    return [step for step in trace if "@" not in step and not step.startswith(_VISIT_STEP)]



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
    token = _inside_timed.set(True)
    try:
        yield
    finally:
        _inside_timed.reset(token)
        if trace is not None:
            trace.add_timing(what, round((monotonic() - start) * 1000))


@contextmanager
def model_call(
    model: str, kind: str, messages: list[dict], tools: list[dict], *, fallback: bool = False, host: str = "",
):
    """Record one provider attempt, even if transport fails or the attempt is cancelled."""
    trace = _current.get()
    if trace is None or kind not in ("chat", "triage", "guard", "media", "search", "escalation"):
        yield None
        return

    users = [m for m in messages if m.get("role") == "user"]
    first = users[0].get("content") if users else None
    head, marker, _ = first.partition("<chat_window") if isinstance(first, str) else ("", "", "")
    call = ModelCall(
        kind, model, fallback,
        prompt_hash([m for m in messages if m.get("role") in ("system", "developer")]),
        prompt_hash(tools),
        # Media and search users carry base64 attachments (up to ~13 MB): hashing them would stall the event loop.
        None if kind in ("media", "search") else prompt_hash(users),
        prompt_hash(head) if marker else None,
        host=host,
    )
    visit = _visit.get() if kind == "chat" else None
    if visit is not None and trace is visit[3]:
        attention, chat_id, now, _ = visit
        at = now()
        if not any(previous.kind == "chat" for previous in trace.model_calls):
            if (previous := attention.previous_call(chat_id)) is not None:
                previous_at, call.between_tokens = previous
                call.gap_ms = milliseconds(at - previous_at)
    else:
        visit = None
    trace.model_calls.append(call)
    trace.calls += 1
    if kind in ("chat", "escalation") or (not trace.model and kind == "triage"):
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
        if visit is not None:
            attention.record_call(chat_id, at, call.input_tokens or 0)
        if _inside_timed.get():
            pass  # e.g. a plugin tool's leak-guard call: the enclosing tool timing already covers it
        elif kind in ("chat", "escalation"):
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
        if not trace.superseded and (trace.steps or trace.calls):
            try:
                trace.row_id = await store.record_turn(
                    at, chat_id, scope, trace.calls, [*trace.steps, *trace.timings], trace.model, trace.fallback,
                    round((monotonic() - start) * 1000), trace.outcome or "Stayed quiet",
                    model_calls=[asdict(call) for call in trace.model_calls],
                    first_bubble_ms=trace.first_bubble_ms, last_bubble_ms=trace.last_bubble_ms,
                )
            except Exception:
                log.exception("could not save turn metadata in chat %s", chat_id)

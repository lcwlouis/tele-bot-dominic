"""Capture outcome and model metadata for one dispatch, and the full debug trace (dom.tracer) beside it.

The Turn log row keeps metadata only. The trace keeps everything (prompts, replies, tool results), recorded here as
plain references on the reply path and encoded, redacted and stored later by dom.tracer, off the reply path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from time import monotonic, time
from typing import TYPE_CHECKING, Any

from dom.secrets import active_redactions, redact_secrets
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
        self.raw = response  # not a field: the trace reads it after the turn; the Turn log row never stores it
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
    # The debug trace (dom.tracer): events in the order they happened, as plain dicts holding references.
    events: list[dict] = field(default_factory=list)
    dropped_events: int = 0
    sealed: bool = False  # submitted for writing: later events (from tasks that outlive it) are ignored
    started: float = field(default_factory=time)
    kind: str = "turn"  # turn | after | background
    label: str = "turn"
    chat_id: int | None = None
    scope: str = ""
    parent: TurnTrace | None = None  # for an `after` part: the turn it belongs to, whose row id is read when written
    turn_id: int | None = None  # for an `after` part started outside its turn: that turn's row
    children: list[TurnTrace] = field(default_factory=list)  # `after` parts that finished before this turn was saved
    replaced_by: TurnTrace | None = None  # a restarted attempt: the trace that absorbed it, which its children join

    def add_timing(self, what: str, elapsed_ms: int) -> None:
        self.timings.append(f"{what}@{elapsed_ms}")

    def add_event(self, kind: str, fields: dict[str, Any]) -> None:
        if self.sealed:
            return
        if len(self.events) >= MAX_EVENTS:
            self.dropped_events += 1
            return
        self.events.append({"type": kind, "at": time(), **fields})

    def absorb(self, other: TurnTrace) -> None:
        """Take another trace's events (an arrival's triage, a restarted attempt), which come before this one's."""
        self.events[:0] = other.events
        self.dropped_events += other.dropped_events
        self.started = min(self.started, other.started)
        other.replaced_by = self  # a child of it still running finds this trace when it finishes
        for child in other.children:  # work a restarted attempt spawned belongs to the turn that replaced it
            child.parent = self
        self.children.extend(other.children)
        other.children.clear()


MAX_EVENTS = 2000
TURN_KINDS = ("chat", "triage", "guard", "media", "search", "escalation")  # the calls a Turn log row counts
_sink: Callable[[TurnTrace], None] | None = None  # dom.tracer's writer, set while Dom runs


def set_sink(sink: Callable[[TurnTrace], None] | None) -> None:
    global _sink
    _sink = sink


def submit(trace: TurnTrace) -> None:
    """Hand a finished trace to the writer: a non-blocking put into a bounded queue, so it costs the reply path nothing.
    An `after` part that finishes before its turn is saved waits on that turn, so it is written with the turn's id."""
    if trace.sealed:
        return
    trace.sealed = True
    parent = trace.parent
    while parent is not None and parent.replaced_by is not None:
        parent = parent.replaced_by
    trace.parent = parent
    if parent is not None and parent.row_id is None and not parent.sealed:
        parent.children.append(trace)
        return
    _hand_over(trace)
    children, trace.children = trace.children, []
    for child in children:
        _hand_over(child)


def _hand_over(trace: TurnTrace) -> None:
    if _sink is not None and (trace.events or trace.steps or trace.model_calls):
        try:
            _sink(trace)
        except Exception:
            log.exception("could not queue a trace")


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


def current() -> TurnTrace | None:
    return _current.get()


def event(kind: str, /, **fields: Any) -> None:
    """Add one event to the current trace. Fields are kept as references and encoded after the turn."""
    if (trace := _current.get()) is not None:
        trace.add_event(kind, fields)


def _request_copy(body: dict | None) -> dict | None:
    """A shallow copy that freezes the message list: the agent loop appends to it after the call."""
    if body is None:
        return None
    copy = dict(body)
    if isinstance(copy.get("messages"), list):
        copy["messages"] = list(copy["messages"])
    return copy


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
    body: dict | None = None, url: str = "",
):
    """Record one provider attempt, even if transport fails or the attempt is cancelled. `body` is the exact request,
    which the trace keeps with the response (ModelCall.received)."""
    trace = _current.get()
    if trace is None:
        yield None
        return
    request = _request_copy(body)
    if kind not in TURN_KINDS:  # traced, but not part of the Turn log row (memory, summaries, embeddings)
        call = ModelCall(kind, model, fallback, "", "", None, None, host=host)
        start = monotonic()
        error: BaseException | None = None
        try:
            yield call
        except BaseException as exc:
            call.status, error = type(exc).__name__, exc
            raise
        finally:
            call.duration_ms = round((monotonic() - start) * 1000)
            trace.add_event("model_call", {"call": call, "request": request, "url": url, "error": _error_text(error)})
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
    error = None
    try:
        yield call
    except BaseException as exc:
        call.status, error = type(exc).__name__, exc
        raise
    finally:
        call.duration_ms = round((monotonic() - start) * 1000)
        trace.add_event("model_call", {"call": call, "request": request, "url": url, "error": _error_text(error)})
        if visit is not None:
            attention.record_call(chat_id, at, call.input_tokens or 0)
        if _inside_timed.get():
            pass  # e.g. a plugin tool's leak-guard call: the enclosing tool timing already covers it
        elif kind in ("chat", "escalation"):
            trace.add_timing("fallback_model" if fallback else "model", call.duration_ms)
        elif kind in ("triage", "guard"):
            trace.add_timing(kind, call.duration_ms)
        # Media and search run inside stages that already account for their wall time.


def _error_text(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return type(error).__name__ + (f": {error}"[:500] if str(error) else "")


def bubble_sent(kind: str = "text", *, chat_id: int | None = None, msg_id: int | None = None, text: str | None = None) -> None:
    trace = _current.get()
    if trace is not None:
        trace.add_event("delivery", {"kind": kind, "chat_id": chat_id, "msg_id": msg_id, "text": text})
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
    trace.chat_id, trace.scope = chat_id, scope
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
                steps_out = [*trace.steps, *trace.timings]
                model_out = trace.model
                outcome_out = trace.outcome or "Stayed quiet"
                calls_out = [asdict(call) for call in trace.model_calls]
                if active_redactions():
                    steps_out = [redact_secrets(s) for s in steps_out]
                    model_out = redact_secrets(model_out)
                    outcome_out = redact_secrets(outcome_out)
                    for item in calls_out:
                        for k, v in item.items():
                            if isinstance(v, str):
                                item[k] = redact_secrets(v)
                trace.row_id = await store.record_turn(
                    at, chat_id, scope, trace.calls, steps_out, model_out, trace.fallback,
                    round((monotonic() - start) * 1000), outcome_out,
                    model_calls=calls_out,
                    first_bubble_ms=trace.first_bubble_ms, last_bubble_ms=trace.last_bubble_ms,
                )
            except Exception:
                log.exception("could not save turn metadata in chat %s", chat_id)
        if not trace.superseded:
            submit(trace)  # also when nothing was recorded: work it spawned waits on it for its turn id


@contextmanager
def follow_up(label: str, chat_id: int, scope: str, *, turn_id: int | None = None):
    """Trace work spawned from a turn that may outlive it (the staying-online check, a studio job) as an `after` part
    of that turn. Outside a turn it is a `background` part of the chat."""
    parent = _current.get()
    trace = TurnTrace(
        kind="after" if parent is not None or turn_id is not None else "background", label=label, chat_id=chat_id,
        scope=scope, parent=parent, turn_id=turn_id,
    )
    try:
        with track(trace):
            yield trace
    finally:
        submit(trace)


@contextmanager
def background(label: str, chat_id: int, scope: str):
    """Trace background work that belongs to no turn (the memory pass) as its own part, found by chat and time."""
    trace = TurnTrace(kind="background", label=label, chat_id=chat_id, scope=scope)
    try:
        with track(trace):
            yield trace
    finally:
        submit(trace)

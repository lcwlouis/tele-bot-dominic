"""The debug tracer (E102): a full record of every turn, written off the reply path.

turn_log collects a turn's events as plain references while the turn runs. When the turn ends, `turn_log.submit`
puts the trace into this module's bounded queue without waiting. One writer task takes it from there, in a worker
thread: it parses the responses, strips media, redacts secrets (the instance's real secret values, then the credential
shapes in dom.secret_patterns), compresses, and stores it in `<home>/traces.db`. That file is its own SQLite database,
so trace writes never wait on, or hold up, the store's lock that the reply path uses. Retention and a size cap with
oldest-first eviction keep it bounded (`[tracer]` in config.toml). See
docs/superpowers/specs/2026-11-27-dashboard-chat-and-tracer.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import zlib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dom import turn_log
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN

log = logging.getLogger("dom.tracer")

VERSION = 1
QUEUE_SIZE = 32  # traces waiting to be written; past this a trace is dropped (and counted), never awaited
MAX_TRACE_BYTES = 8_000_000  # one trace, compressed; past it long strings are cut, then bodies are dropped
CUT_CHARS = 65_536  # a string kept in full is at most this long once a trace is over MAX_TRACE_BYTES
MEDIA_MIN_CHARS = 4096  # a base64 string this long or longer is media, kept as a placeholder
EXPORT_LIMIT = 500
REDACTED = "[redacted]"
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s_-]+")
_DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+)?(?:;[\w=.-]+)*;base64,", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER,
    chat_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size INTEGER NOT NULL,
    body BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_turn ON traces(turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_chat_at ON traces(chat_id, at);
CREATE INDEX IF NOT EXISTS idx_traces_at ON traces(at);
"""


# --- encoding -----------------------------------------------------------------------------------------------------


@dataclass
class _Cleaner:
    """One pass over a trace's values: media becomes a placeholder, secrets become [redacted], and with `cut` long
    strings are shortened. Every string is redacted before it is cut, so a cut never leaves half a secret behind."""

    secrets: tuple[str, ...]
    cut: int | None = None
    redactions: int = 0
    media: int = 0
    truncated: bool = False
    drop_bodies: bool = False

    def string(self, text: str) -> str:
        if len(text) >= MEDIA_MIN_CHARS:
            placeholder = self._media(text)
            if placeholder is not None:
                self.media += 1
                return placeholder
        for secret in self.secrets:
            if secret in text:
                self.redactions += text.count(secret)
                text = text.replace(secret, REDACTED)
        for pattern in (API_KEY, TELEGRAM_BOT_TOKEN):
            text, count = pattern.subn(REDACTED, text)
            self.redactions += count
        text, count = BEARER_TOKEN.subn(lambda match: match.group(0).replace(match.group(1), REDACTED), text)
        self.redactions += count
        if self.cut is not None and len(text) > self.cut:
            self.truncated = True
            text = f"{text[:self.cut]}… [cut {len(text) - self.cut} characters]"
        return text

    @staticmethod
    def _media(text: str) -> str | None:
        head = _DATA_URL.match(text[:200])
        if head is not None:
            payload, mime = text[head.end():], head.group(1) or "application/octet-stream"
        elif _BASE64.fullmatch(text):
            payload, mime = text, "binary"
        else:
            return None
        digest = hashlib.sha256(payload.encode()).hexdigest()[:8]
        return f"[media {mime}, {len(payload) * 3 // 4 // 1000} kB, sha256 {digest}]"

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.string(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict):
            return {self.string(str(key)): self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.clean(item) for item in value]
        if isinstance(value, BaseModel):
            return self.clean(value.model_dump(mode="json"))
        if is_dataclass(value) and not isinstance(value, type):
            return self.clean(asdict(value))
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return f"[{len(value)} bytes]"
        return self.string(str(value))


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _response(call: turn_log.ModelCall) -> tuple[Any, float | None]:
    """The provider's answer as JSON (text when it is not JSON), and the cost it reports, if any."""
    raw = getattr(call, "raw", None)
    if raw is None:
        return None, None
    try:
        payload = raw.json()
    except Exception:
        try:
            return raw.text, None
        except Exception:
            return None, None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return payload, float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None


def _event(item: dict, started: float, drop_bodies: bool) -> dict:
    kind, at = item.get("type"), item.get("at", started)
    out: dict[str, Any] = {"type": kind, "at": _iso(at), "t_ms": max(0, round((at - started) * 1000))}
    if kind == "model_call" and isinstance(item.get("call"), turn_log.ModelCall):
        call: turn_log.ModelCall = item["call"]
        response, cost = _response(call)
        out.update(asdict(call))
        out["started_at"] = _iso(at - call.duration_ms / 1000)
        out["t_ms"] = max(0, round((at - started) * 1000) - call.duration_ms)
        out.update(url=item.get("url") or "", error=item.get("error"), cost=cost)
        if drop_bodies:
            out["request"] = out["response"] = "[dropped: the trace was over its size cap]"
        else:
            out["request"], out["response"] = item.get("request"), response
        return out
    out.update({key: value for key, value in item.items() if key not in ("type", "at")})
    return out


def _parse_timing(text: str) -> dict:
    what, _, ms = text.rpartition("@")
    try:
        return {"what": what, "ms": int(ms)}
    except ValueError:
        return {"what": text, "ms": None}


def encode(trace: turn_log.TurnTrace, secrets: Iterable[str], *, cut: int | None = None, drop_bodies: bool = False) -> dict:
    """The trace as the JSON body stored, cleaned (media out, secrets redacted). Runs in a worker thread."""
    started = trace.started
    events = sorted(trace.events, key=lambda item: item.get("at", started))
    ended = max([started, *(item.get("at", started) for item in events)])
    turn_id = trace.row_id if trace.kind == "turn" else trace.turn_id or (
        trace.parent.row_id if trace.parent is not None else None
    )
    kind = trace.kind if trace.kind == "turn" or turn_id is not None else "background"
    body = {
        "version": VERSION, "turn_id": turn_id, "chat_id": str(trace.chat_id) if trace.chat_id is not None else "",
        "scope": trace.scope, "kind": kind, "label": trace.label,
        "started_at": _iso(started), "ended_at": _iso(ended), "duration_ms": round((ended - started) * 1000),
        "outcome": trace.outcome, "model": trace.model, "fallback": trace.fallback,
        "steps": turn_log.visible_steps(trace.steps), "visits": turn_log.visits_in(trace.steps),
        "timings": [_parse_timing(item) for item in trace.timings],
        "first_bubble_ms": trace.first_bubble_ms, "last_bubble_ms": trace.last_bubble_ms,
        "events": [_event(item, started, drop_bodies) for item in events],
        "dropped_events": trace.dropped_events,
    }
    cleaner = _Cleaner(tuple(sorted({s for s in secrets if s}, key=len, reverse=True)), cut=cut)
    cleaned = cleaner.clean(body)
    cleaned.update(redactions=cleaner.redactions, media_stripped=cleaner.media,
                   truncated=cleaner.truncated or drop_bodies)
    return cleaned


def pack(trace: turn_log.TurnTrace, secrets: Iterable[str]) -> tuple[dict, bytes]:
    """Encode and compress, shrinking a trace past MAX_TRACE_BYTES: cut long strings, then drop the bodies."""
    secrets = tuple(secrets)
    for options in ({}, {"cut": CUT_CHARS}, {"cut": CUT_CHARS, "drop_bodies": True}):
        body = encode(trace, secrets, **options)
        blob = zlib.compress(json.dumps(body, ensure_ascii=False).encode(), 6)
        if len(blob) <= MAX_TRACE_BYTES:
            break
    return body, blob


def unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob))


# --- storage ------------------------------------------------------------------------------------------------------


class TraceStore:
    """`<home>/traces.db`: its own connection and lock, apart from state.db's."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {VERSION}")
        self._total: int | None = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    # Called with the lock held, from the writer's thread.
    def insert(self, body: dict, blob: bytes, max_bytes: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO traces (turn_id, chat_id, scope, kind, label, at, outcome, size, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body["turn_id"], body["chat_id"], body["scope"], body["kind"], body["label"], body["started_at"],
                 body["outcome"] or "", len(blob), blob),
            )
            self._total = (self._total_size() if self._total is None else self._total + len(blob))
            self._evict(max_bytes)
            return int(cursor.lastrowid)

    def _total_size(self) -> int:
        return int(self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM traces").fetchone()[0])

    def _evict(self, max_bytes: int) -> None:
        """Delete the oldest traces until the total is under the cap."""
        if self._total is None or self._total <= max_bytes:
            return
        rows = self._conn.execute("SELECT id, size FROM traces ORDER BY at, id").fetchall()
        doomed, total = [], self._total
        for row in rows:
            if total <= max_bytes:
                break
            doomed.append(row["id"])
            total -= row["size"]
        for start in range(0, len(doomed), 500):
            chunk = doomed[start:start + 500]
            self._conn.execute(f"DELETE FROM traces WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        self._total = total
        log.info("trace store over its cap: evicted %d oldest traces", len(doomed))

    async def prune(self, before: datetime) -> int:
        cutoff = before.astimezone(timezone.utc).isoformat()

        def prune() -> int:
            count = self._conn.execute("DELETE FROM traces WHERE at < ?", (cutoff,)).rowcount
            self._total = None
            return count

        return await self._run(prune)

    async def evict(self, max_bytes: int) -> None:
        def evict() -> None:
            self._total = self._total_size()
            self._evict(max_bytes)

        await self._run(evict)

    async def delete_chat(self, chat_id: int | str) -> int:
        def delete() -> int:
            count = self._conn.execute("DELETE FROM traces WHERE chat_id = ?", (str(chat_id),)).rowcount
            self._total = None
            return count

        return await self._run(delete)

    async def turn(self, turn_id: int) -> list[dict]:
        """Every part of one turn, the turn itself first, then the work after it in order."""
        rows = await self._run(lambda: self._conn.execute(
            "SELECT body FROM traces WHERE turn_id = ? ORDER BY kind != 'turn', at, id", (turn_id,),
        ).fetchall())
        return [unpack(row["body"]) for row in rows]

    async def traced(self, turn_ids: Iterable[int]) -> set[int]:
        ids = [int(turn_id) for turn_id in turn_ids][:500]
        if not ids:
            return set()
        rows = await self._run(lambda: self._conn.execute(
            f"SELECT DISTINCT turn_id FROM traces WHERE turn_id IN ({','.join('?' * len(ids))})", ids,
        ).fetchall())
        return {row["turn_id"] for row in rows}

    async def summaries(self, chat_id: int | str, since: datetime) -> list[dict]:
        """One line per part in a chat since a time: what the dashboard's Trace view lays beside the messages."""
        rows = await self._run(lambda: self._conn.execute(
            "SELECT id, turn_id, kind, label, at, outcome, size FROM traces WHERE chat_id = ? AND at >= ? "
            "ORDER BY at, id LIMIT 500",
            (str(chat_id), since.astimezone(timezone.utc).isoformat()),
        ).fetchall())
        return [dict(row) for row in rows]

    async def part(self, trace_id: int) -> dict | None:
        row = await self._run(lambda: self._conn.execute("SELECT body FROM traces WHERE id = ?", (trace_id,)).fetchone())
        return unpack(row["body"]) if row is not None else None

    async def export(
        self, chat_id: str | None, start: datetime | None, end: datetime | None, limit: int = EXPORT_LIMIT,
    ) -> tuple[list[dict], bool]:
        clauses, params = [], []
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if start is not None:
            clauses.append("at >= ?")
            params.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            clauses.append("at < ?")
            params.append(end.astimezone(timezone.utc).isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._run(lambda: self._conn.execute(
            f"SELECT body FROM traces {where} ORDER BY at, id LIMIT ?", (*params, limit + 1),
        ).fetchall())
        return [unpack(row["body"]) for row in rows[:limit]], len(rows) > limit

    async def stats(self) -> dict:
        row = await self._run(lambda: self._conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes, MIN(at) AS oldest FROM traces",
        ).fetchone())
        return dict(row)


# --- the writer ---------------------------------------------------------------------------------------------------


@dataclass
class TraceWriter:
    """Takes finished traces from turn_log without waiting, and writes them one at a time in a worker thread."""

    store: TraceStore
    settings: Callable[[], tuple[int, int]]  # (retention days, cap in bytes), read at each write
    secrets: Callable[[], Iterable[str]]
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    dropped: int = 0
    written: int = 0
    _queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    _task: asyncio.Task | None = None

    def submit(self, trace: turn_log.TurnTrace) -> None:
        try:
            self._queue.put_nowait(trace)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("trace queue full: dropped the trace of a %s in chat %s", trace.label, trace.chat_id)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._drain())
            turn_log.set_sink(self.submit)

    async def close(self, timeout: float = 5.0) -> None:
        turn_log.set_sink(None)
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except TimeoutError:
            log.warning("closing with %d traces unwritten", self._queue.qsize())
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _drain(self) -> None:
        while True:
            trace = await self._queue.get()
            try:
                await self.write(trace)
            except Exception:
                log.exception("could not write a trace for chat %s", trace.chat_id)
            finally:
                self._queue.task_done()

    async def write(self, trace: turn_log.TurnTrace) -> int:
        _, cap = self.settings()
        secrets = tuple(self.secrets())

        def work() -> int:
            body, blob = pack(trace, secrets)
            return self.store.insert(body, blob, cap)

        trace_id = await asyncio.to_thread(work)
        self.written += 1
        return trace_id

    async def prune(self) -> None:
        days, cap = self.settings()
        await self.store.prune(self.now() - timedelta(days=days))
        await self.store.evict(cap)


def open_writer(home: Path, config: Callable[[], Any]) -> TraceWriter:
    """The instance's writer, reading `[tracer]` and the secret values from the live config at each use."""
    from dom.model_lists import secret_values

    def settings() -> tuple[int, int]:
        tracer = config().tracer
        return tracer.retention_days, tracer.max_mb * 1_000_000

    def secrets() -> Iterable[str]:
        current = config()
        values = set(secret_values(current))
        return values

    return TraceWriter(TraceStore(Path(home) / "traces.db"), settings, secrets)

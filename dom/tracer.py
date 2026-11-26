"""The debug tracer (E102): a full record of every turn, written off the reply path.

turn_log collects a turn's events as plain references while the turn runs. When the turn ends, `turn_log.submit`
puts the trace into this module's bounded queue without waiting. One writer task takes it from there, in a worker
thread: it reads the instance's secret values, parses the responses, strips media, redacts secrets, compresses, and
stores the trace in `<home>/traces.db`. Redaction covers the secret values only: every configured key variable, the
fixed credentials and every `.env` variable whose name says it holds one (never every `.env` value), in their raw,
JSON-escaped, URL and base64 forms, plus the credential shapes in dom.secret_patterns. It runs on each string leaf
before any cut and on JSON inside strings, never over the serialised text, so it can't break the JSON. traces.db is its own owner-only SQLite database, so trace writes never wait on, or hold up, the store's
lock that the reply path uses. Retention and a size cap with oldest-first eviction keep it bounded (`[tracer]` in
config.toml). Reads unpack and serialise in a worker thread too. See
docs/superpowers/specs/2026-11-27-dashboard-chat-and-tracer.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import zlib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dom import turn_log
from dom.secret_patterns import (
    API_KEY, BASIC_AUTH, BEARER_TOKEN, CREDENTIAL_FIELD, CREDENTIAL_FIELDS, SECRET_NAME, TELEGRAM_BOT_TOKEN,
)

log = logging.getLogger("dom.tracer")

VERSION = 1
QUEUE_SIZE = 32  # traces waiting to be written; past this a trace is dropped (and counted), never awaited
MAX_TRACE_BYTES = 8_000_000  # one trace, compressed; past it long strings are cut, then bodies dropped, then a stub
CUT_CHARS = 65_536  # a string kept in full is at most this long once a trace is over MAX_TRACE_BYTES
MEDIA_MIN_CHARS = 4096  # a base64 string this long or longer is media, kept as a placeholder
MIN_SECRET_CHARS = 8  # shorter secret values are redacted only as a whole token, never inside a word
EXPORT_LIMIT = 500  # parts in one range export
EXPORT_MAX_BYTES = 64_000_000  # serialised bytes in one range export; past it the export stops and says so
SUMMARY_LIMIT = 200
FILE_MODE = 0o600
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


# --- secrets ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Secrets:
    """The instance's secret values: `long` ones (MIN_SECRET_CHARS and up) are redacted wherever they appear, in every
    encoding below; `short` ones only as a whole token, never inside a word."""

    long: tuple[str, ...] = ()
    short: tuple[str, ...] = ()


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def instance_secrets(config: Any) -> Secrets:
    """The values of every key variable config.toml names (each `*_env`: `api_key_env`, `dev_token_env`, the studio's
    and the desktop's tokens) and the fixed credentials such as the bot token, plus every `.env` variable whose name says
    it holds a credential (KEY, TOKEN, SECRET, PASSWORD, AUTH, COOKIE, SESSION…), from `.env` or the environment. Never
    every `.env` value: a time zone or a port is not a secret. Reads files: call it off the event loop."""
    env = _env_file(Path(config.home) / ".env")
    names = set(config.secret_names()) | {name for name in env if SECRET_NAME.search(name)}
    values = {value for name in names if (value := env.get(name) or os.environ.get(name))}
    by_length = sorted(values, key=len, reverse=True)
    return Secrets(
        long=tuple(value for value in by_length if len(value) >= MIN_SECRET_CHARS),
        short=tuple(value for value in by_length if len(value) < MIN_SECRET_CHARS),
    )


def _base64_forms(secret: bytes) -> set[str]:
    """The secret's base64 wherever it sits in an encoded string: at each of the three byte alignments, standard and
    URL-safe, without the edge characters that also carry the neighbouring bytes."""
    forms = set()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + secret).decode().rstrip("=")
        start = (0, 2, 3)[offset]
        end = (offset + len(secret)) * 8 // 6
        core = encoded[start:end]
        if len(core) >= MIN_SECRET_CHARS:
            forms.add(core)
            forms.add(core.replace("+", "-").replace("/", "_"))
    full = base64.b64encode(secret).decode()
    forms |= {full, full.rstrip("="), base64.urlsafe_b64encode(secret).decode(), base64.urlsafe_b64encode(secret).decode().rstrip("=")}
    return forms


def secret_forms(secret: str) -> set[str]:
    """A long secret as it may appear in text: raw, JSON-escaped, every character as \\uXXXX, URL-encoded and base64."""
    forms = {
        secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1],
        "".join(f"\\u{ord(char):04x}" for char in secret),
        urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret), urllib.parse.quote(secret),
    }
    forms |= _base64_forms(secret.encode())
    return {form for form in forms if len(form) >= MIN_SECRET_CHARS}


# --- encoding -----------------------------------------------------------------------------------------------------


EVENT_META = ("type", "at", "t_ms", "started_at")  # an event's own fields, never redacted


@dataclass
class _Cleaner:
    """One pass over a trace's content, string leaves only: media becomes a placeholder, secrets become [redacted], and
    with `cut` long strings are shortened. A string that is JSON (a tool's raw arguments) is parsed and its leaves
    cleaned, recursively, so escapes can't hide a value. Keys stay as they are, but the whole value of a credential
    field (Authorization, Cookie…) is redacted. Each string is redacted before it is cut, so a cut never leaves half
    a secret behind. Serialised JSON is never rewritten, so redaction can't break its structure."""

    secrets: Secrets
    cut: int | None = None
    redactions: int = 0
    media: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        self._forms = tuple(sorted({form for secret in self.secrets.long for form in secret_forms(secret)}, key=len, reverse=True))
        self._short = [
            re.compile(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])") for secret in self.secrets.short
        ]

    def _sub(self, pattern: re.Pattern, replace: Any, text: str) -> str:
        text, count = pattern.subn(replace, text)
        self.redactions += count
        return text

    def redact(self, text: str) -> str:
        for form in self._forms:
            if form in text:
                self.redactions += text.count(form)
                text = text.replace(form, REDACTED)
        for pattern in self._short:
            text = self._sub(pattern, REDACTED, text)
        text = self._sub(CREDENTIAL_FIELD, lambda match: match.group(1) + match.group(2) + (
            f'"{REDACTED}"' if match.group(3).startswith('"') else REDACTED
        ), text)
        for pattern in (BEARER_TOKEN, BASIC_AUTH):
            text = self._sub(pattern, lambda match: match.group(0).replace(match.group(1), REDACTED), text)
        for pattern in (API_KEY, TELEGRAM_BOT_TOKEN):
            text = self._sub(pattern, REDACTED, text)
        return text

    def string(self, text: str) -> str:
        if len(text) >= MEDIA_MIN_CHARS:
            placeholder = self._media(text)
            if placeholder is not None:
                self.media += 1
                return placeholder
        stripped = text.strip()
        if stripped[:1] in ("{", "[") and len(stripped) < 2_000_000:
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return self._cut(json.dumps(self.clean(parsed), ensure_ascii=False))
        return self._cut(self.redact(text))

    def _cut(self, text: str) -> str:
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
            cleaned = {}
            for key, item in value.items():
                key = str(key)
                if key.lower() in CREDENTIAL_FIELDS and item not in (None, ""):
                    self.redactions += 1
                    cleaned[key] = REDACTED
                else:
                    cleaned[key] = self.clean(item)
            return cleaned
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

    def event(self, event: dict) -> dict:
        """An event's content cleaned; its type and times kept exactly."""
        cleaned = self.clean({key: value for key, value in event.items() if key not in EVENT_META})
        return {**{key: event[key] for key in EVENT_META if key in event}, **cleaned}


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


def turn_id_of(trace: turn_log.TurnTrace) -> int | None:
    if trace.kind == "turn":
        return trace.row_id
    return trace.turn_id or (trace.parent.row_id if trace.parent is not None else None)


def meta_of(trace: turn_log.TurnTrace) -> dict:
    """What identifies and indexes a trace (its chat, turn, kind and times), from the trace's own metadata. It is
    stored in its own columns and never passes through redaction, so forget, aliases and pruning always find it."""
    started = trace.started
    ended = max([started, *(item.get("at", started) for item in trace.events)])
    turn_id = turn_id_of(trace)
    return {
        "version": VERSION, "turn_id": turn_id, "chat_id": str(trace.chat_id) if trace.chat_id is not None else "",
        "scope": trace.scope, "kind": trace.kind if trace.kind == "turn" or turn_id is not None else "background",
        "label": trace.label, "started_at": _iso(started), "ended_at": _iso(ended),
        "duration_ms": round((ended - started) * 1000),
        "first_bubble_ms": trace.first_bubble_ms, "last_bubble_ms": trace.last_bubble_ms,
        "dropped_events": trace.dropped_events,
    }


def encode(
    trace: turn_log.TurnTrace, secrets: Secrets | Iterable[str], *, cut: int | None = None, drop_bodies: bool = False,
    stub: bool = False,
) -> dict:
    """The trace as the JSON body stored: its metadata as it is, its content cleaned (media out, secrets redacted).
    Runs in a worker thread. A `stub` keeps only each event's type and time: the last resort for a trace still over
    the cap without its bodies."""
    if not isinstance(secrets, Secrets):
        values = sorted(set(secrets), key=len, reverse=True)
        secrets = Secrets(tuple(v for v in values if len(v) >= MIN_SECRET_CHARS), tuple(v for v in values if 0 < len(v) < MIN_SECRET_CHARS))
    meta = meta_of(trace)
    started = trace.started
    events = [_event(item, started, drop_bodies) for item in sorted(trace.events, key=lambda item: item.get("at", started))]
    cleaner = _Cleaner(secrets, cut=CUT_CHARS if stub else cut)
    if stub:
        cleaned_events = [{key: event[key] for key in ("type", "at", "t_ms")} for event in events]
    else:
        cleaned_events = [cleaner.event(event) for event in events]
    content = cleaner.clean({
        "outcome": trace.outcome, "model": trace.model, "fallback": trace.fallback,
        "steps": turn_log.visible_steps(trace.steps)[:200], "visits": turn_log.visits_in(trace.steps)[:50],
        "timings": [_parse_timing(item) for item in trace.timings][:200],
    })
    return {
        **meta, **content, "events": cleaned_events,
        "redactions": cleaner.redactions, "media_stripped": cleaner.media,
        "truncated": cleaner.truncated or drop_bodies or stub,
    }


def _serialise(body: dict) -> bytes:
    return zlib.compress(json.dumps(body, ensure_ascii=False).encode(), 6)


def pack(trace: turn_log.TurnTrace, secrets: Secrets | Iterable[str]) -> tuple[dict, bytes]:
    """Encode and compress, shrinking a trace past MAX_TRACE_BYTES: cut long strings, then drop the bodies, then keep
    a stub of event types and times, then the outline alone."""
    if not isinstance(secrets, Secrets):
        secrets = tuple(secrets)
    for options in ({}, {"cut": CUT_CHARS}, {"cut": CUT_CHARS, "drop_bodies": True}, {"drop_bodies": True, "stub": True}):
        body = encode(trace, secrets, **options)
        blob = _serialise(body)
        if len(blob) <= MAX_TRACE_BYTES:
            return body, blob
    log.warning("a trace for chat %s is over the cap even as a stub; storing its outline only", trace.chat_id)
    body = {**body, "events": [], "steps": [], "timings": [], "visits": [], "truncated": True}
    return body, _serialise(body)


def unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob))


# --- storage ------------------------------------------------------------------------------------------------------


def _private(path: Path) -> None:
    """traces.db and its WAL sidecars are the owner's alone (they hold whole conversations)."""
    for item in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(item, FILE_MODE)
        except FileNotFoundError:
            pass


class TraceStore:
    """`<home>/traces.db`: its own connection and lock, apart from state.db's. Every read unpacks in a worker thread."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        # Created owner-only before SQLite opens it: SQLite gives the WAL and shm files the database file's mode.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE))
        _private(path)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {VERSION}")
        _private(path)
        self._total: int | None = None
        self._forgotten: dict[str, float] = {}  # chat -> when it was forgotten: older traces of it are never stored

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    def refused(self, chat_id: str, started: float) -> bool:
        forgotten = self._forgotten.get(chat_id)
        return forgotten is not None and started <= forgotten

    # Called from the writer's thread.
    def insert(self, body: dict, blob: bytes, max_bytes: int, *, started: float | None = None) -> int | None:
        with self._lock:
            if started is not None and self.refused(body["chat_id"], started):
                return None  # its chat was forgotten while this trace waited or was being encoded
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

    async def delete_chats(self, chat_ids: Iterable[int | str], *, forgotten_at: float | None = None) -> int:
        """Delete every trace of these chats (a chat and its earlier ids), and with `forgotten_at` refuse any trace of
        theirs that started before it, so one still queued or being encoded is never stored afterwards."""
        keys = sorted({str(chat_id) for chat_id in chat_ids})

        def delete() -> int:
            if forgotten_at is not None:
                for key in keys:
                    self._forgotten[key] = max(self._forgotten.get(key, 0.0), forgotten_at)
            count = self._conn.execute(
                f"DELETE FROM traces WHERE chat_id IN ({','.join('?' * len(keys))})", keys,
            ).rowcount if keys else 0
            self._total = None
            return count

        return await self._run(delete)

    async def delete_chat(self, chat_id: int | str) -> int:
        return await self.delete_chats([chat_id])

    async def turn_json(self, turn_id: int) -> bytes | None:
        """Every part of one turn as `{"turn_id", "parts"}` JSON, the turn first, then the work after it in order."""
        def read() -> bytes | None:
            rows = self._conn.execute(
                "SELECT body FROM traces WHERE turn_id = ? ORDER BY kind != 'turn', at, id", (turn_id,),
            ).fetchall()
            if not rows:
                return None
            return _join([zlib.decompress(row["body"]) for row in rows], {"turn_id": turn_id})

        return await self._run(read)

    async def part_json(self, trace_id: int) -> bytes | None:
        def read() -> bytes | None:
            row = self._conn.execute("SELECT body FROM traces WHERE id = ?", (trace_id,)).fetchone()
            return _join([zlib.decompress(row["body"])], {}) if row is not None else None

        return await self._run(read)

    async def turn(self, turn_id: int) -> list[dict]:
        data = await self.turn_json(turn_id)
        return await asyncio.to_thread(lambda: json.loads(data)["parts"]) if data is not None else []

    async def traced(self, turn_ids: Iterable[int]) -> set[int]:
        ids = [int(turn_id) for turn_id in turn_ids][:500]
        if not ids:
            return set()
        rows = await self._run(lambda: self._conn.execute(
            f"SELECT DISTINCT turn_id FROM traces WHERE turn_id IN ({','.join('?' * len(ids))})", ids,
        ).fetchall())
        return {row["turn_id"] for row in rows}

    async def summaries(self, chat_id: int | str, since: datetime | None = None, *, after_id: int | None = None) -> list[dict]:
        """One line per part in a chat, from a time or after a trace id (the Talk page's own cursor), oldest first."""
        if after_id is not None:
            sql, args = "chat_id = ? AND id > ?", (str(chat_id), after_id)
        else:
            stamp = (since or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            sql, args = "chat_id = ? AND at >= ?", (str(chat_id), stamp)
        rows = await self._run(lambda: self._conn.execute(
            f"SELECT id, turn_id, kind, label, at, outcome, size FROM traces WHERE {sql} ORDER BY id LIMIT ?",
            (*args, SUMMARY_LIMIT),
        ).fetchall())
        return [dict(row) for row in rows]

    async def export_json(
        self, chat_id: str | None, start: datetime | None, end: datetime | None, extra: dict,
        limit: int = EXPORT_LIMIT, max_bytes: int = EXPORT_MAX_BYTES,
    ) -> bytes:
        """The parts in a range, oldest first, as JSON built in a worker thread: at most `limit` parts and `max_bytes`
        of them, with `truncated` when more matched."""
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

        def read() -> bytes:
            parts, total, truncated = [], 0, False
            for index, row in enumerate(self._conn.execute(
                f"SELECT body FROM traces {where} ORDER BY at, id LIMIT ?", (*params, limit + 1),
            )):
                part = zlib.decompress(row["body"])
                if index >= limit or total + len(part) > max_bytes:
                    truncated = True
                    break
                parts.append(part)
                total += len(part)
            return _join(parts, {**extra, "truncated": truncated})

        return await self._run(read)

    async def stats(self) -> dict:
        row = await self._run(lambda: self._conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes, MIN(at) AS oldest FROM traces",
        ).fetchone())
        return dict(row)


def _join(parts: list[bytes], head: dict) -> bytes:
    """`{...head, "parts": [...]}` from stored JSON bodies, without parsing them again."""
    prefix = json.dumps(head, ensure_ascii=False)[:-1]
    joined = b",".join(parts)
    return (prefix + (", " if head else "") + '"parts": [').encode() + joined + b"]}"


# --- the writer ---------------------------------------------------------------------------------------------------


@dataclass
class TraceWriter:
    """Takes finished traces from turn_log without waiting, and writes them one at a time in a worker thread."""

    store: TraceStore
    settings: Callable[[], tuple[int, int]]  # (retention days, cap in bytes), read at each write
    secrets: Callable[[], Iterable[str]]  # may read files: called in the worker thread, never on the event loop
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

    async def write(self, trace: turn_log.TurnTrace) -> int | None:
        _, cap = self.settings()
        chat = str(trace.chat_id) if trace.chat_id is not None else ""
        if self.store.refused(chat, trace.started):
            return None

        def work() -> int | None:
            body, blob = pack(trace, self.secrets())
            return self.store.insert(body, blob, cap, started=trace.started)

        trace_id = await asyncio.to_thread(work)
        if trace_id is not None:
            self.written += 1
        return trace_id

    async def forget(self, chat_ids: Iterable[int | str]) -> int:
        """Delete every trace of these chats, drop their queued ones, and refuse any of theirs still in flight."""
        keys = {str(chat_id) for chat_id in chat_ids}
        kept: list[turn_log.TurnTrace] = []
        while not self._queue.empty():
            trace = self._queue.get_nowait()
            self._queue.task_done()
            if str(trace.chat_id) not in keys:
                kept.append(trace)
        for trace in kept:
            self._queue.put_nowait(trace)
        return await self.store.delete_chats(keys, forgotten_at=time.time())

    async def prune(self) -> None:
        days, cap = self.settings()
        await self.store.prune(self.now() - timedelta(days=days))
        await self.store.evict(cap)


PENDING_FORGET = "traces.forget-pending.json"  # chats whose traces must go, saved when deleting them failed


def save_pending_forget(home: Path, chat_ids: Iterable[int | str]) -> None:
    """Remember chats whose traces could not be deleted, so the next start deletes them. Written atomically."""
    path = Path(home) / PENDING_FORGET
    try:
        pending = set(json.loads(path.read_text()))
    except (FileNotFoundError, ValueError):
        pending = set()
    pending |= {str(chat_id) for chat_id in chat_ids}
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        json.dump(sorted(pending), handle)
    os.replace(temp, path)


async def forget_stored(home: Path, chat_ids: Iterable[int | str]) -> int:
    """Delete a chat's traces when the writer is not running (the tracer failed to open): open traces.db directly."""
    path = Path(home) / "traces.db"
    if not path.exists():
        return 0
    store = await asyncio.to_thread(TraceStore, path)
    try:
        return await store.delete_chats(chat_ids)
    finally:
        await asyncio.to_thread(store.close)


async def apply_pending_forget(writer: TraceWriter, home: Path) -> int:
    """Delete the traces a failed forget left behind; called when the tracer opens."""
    path = Path(home) / PENDING_FORGET
    try:
        chat_ids = json.loads(await asyncio.to_thread(path.read_text))
    except FileNotFoundError:
        return 0
    deleted = await writer.forget(chat_ids)
    path.unlink(missing_ok=True)
    log.info("deleted %d traces of %d forgotten chats that an earlier forget left behind", deleted, len(chat_ids))
    return deleted


def open_writer(home: Path, config: Callable[[], Any]) -> TraceWriter:
    """The instance's writer, reading `[tracer]` and the secret values from the live config at each use."""

    def settings() -> tuple[int, int]:
        tracer = config().tracer
        return tracer.retention_days, tracer.max_mb * 1_000_000

    return TraceWriter(TraceStore(Path(home) / "traces.db"), settings, lambda: instance_secrets(config()))

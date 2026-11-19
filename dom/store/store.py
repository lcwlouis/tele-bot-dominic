"""
Dom store: SQLite (WAL + FTS5) as the queryable index, append-only
``<home>/transcripts/<chat_id>.jsonl`` as the audit log, and
``<home>/memory/people/<uid>.md`` as per-person notes.

Salvaged from the V2 hybrid store. The owner is a constructor argument, never a constant.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
from xml.sax.saxutils import escape, quoteattr

from dom.models import (
    VISIBILITY_OWNER_PRIVATE,
    VISIBILITY_PUBLIC,
    PersonIdentity,
    Scope,
    StoredMessage,
)

T = TypeVar("T")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# (target PRAGMA user_version, file). 0001-0003 are the V2 migrations, unchanged so existing
# databases keep working; each file sets its own version.
MIGRATIONS: list[tuple[int, str]] = [
    (2, "0001_init.sql"),
    (3, "0002_offline_edit_tracking.sql"),
    (4, "0003_chat_auth_and_outbox.sql"),
    (5, "0004_dom_core.sql"),
    (6, "0005_memory_cursor.sql"),
    (7, "0006_usage.sql"),
    (8, "0007_phase3.sql"),
]
TARGET_VERSION = MIGRATIONS[-1][0]

_PRESENCE_COLUMNS = ("is_sleeping", "sleep_until", "is_offline", "offline_until", "online_until")
_CHAT_AUTH_STATUSES = {"approved", "pending_approval", "ignored", "left", "blocked"}
_CHAT_AUTH_COLUMNS = "chat_id, authorization_status, title, invited_by_handle, approval_prompt_sent"
# Visibility labels that mark owner-only rows ("admin_private" comes from V2 databases).
_PRIVATE_SCOPES = (VISIBILITY_OWNER_PRIVATE, "admin_private")
_PRIVATE_SCOPES_SQL = "(" + ", ".join(f"'{s}'" for s in _PRIVATE_SCOPES) + ")"
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
)
_MESSAGE_COLUMNS = (
    "id, chat_id, msg_id, reply_to_msg_id, sender_user_id, sender_handle, sender_name, "
    "is_bot, role, visibility_scope, text, media_json, is_edited, original_seen_before_edit, "
    "previous_seen_text, is_deleted, created_at, updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_blank(sql: str) -> bool:
    return all(not ln.strip() or ln.lstrip().startswith("--") for ln in sql.splitlines())


def _split_statements(sql: str) -> Iterator[str]:
    buf = ""
    for line in sql.splitlines(keepends=True):
        if not buf and _is_blank(line):
            continue  # leading comments/blank lines are not part of the next statement
        buf += line
        if sqlite3.complete_statement(buf):
            if not _is_blank(buf):
                yield buf.strip()
            buf = ""
    if not _is_blank(buf):
        raise ValueError(f"Incomplete SQL statement in migration: {buf.strip()[:80]!r}")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1].lower() == column.lower() for r in conn.execute(f"PRAGMA table_info({table})"))


def migrate_database(db_path: Path) -> int:
    """Bring ``db_path`` up to ``TARGET_VERSION``; returns the final ``user_version``.

    Before the first pending migration, a consistent snapshot is written to
    ``<db>.v{current_version}.bak`` (never overwriting an existing snapshot).
    Each migration runs in one transaction, so a failure leaves the DB at the previous version.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        pending = [(v, f) for v, f in MIGRATIONS if v > current]
        if not pending:
            return current

        if db_path.stat().st_size > 0 and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            bak_path = db_path.with_name(f"{db_path.name}.v{current}.bak")
            if not bak_path.exists():
                tmp_path = bak_path.with_name(f"{bak_path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    dest = sqlite3.connect(tmp_path)
                    try:
                        conn.backup(dest)
                    finally:
                        dest.close()
                    try:
                        bak_path.hardlink_to(tmp_path)
                    except FileExistsError:
                        pass
                finally:
                    tmp_path.unlink(missing_ok=True)

        for version, filename in pending:
            sql = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                    for stmt in _split_statements(sql):
                        m = _ADD_COLUMN_RE.match(stmt)
                        if m and _column_exists(conn, m.group(1), m.group(2)):
                            continue
                        conn.execute(stmt)
                    applied = conn.execute("PRAGMA user_version").fetchone()[0]
                    if applied != version:
                        raise RuntimeError(f"{filename} left user_version={applied}, expected {version}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _read_media_json(value: str | None) -> dict[str, Any]:
    try:
        media = json.loads(value) if value else None
    except ValueError:
        return {}
    return media if isinstance(media, dict) else {}


def format_chat_window_xml(chat_id: str, messages: list[StoredMessage]) -> str:
    """Render messages as ``<chat_window>`` XML. ``previous_seen_text`` is shown only when
    Dom actually saw the original before it was edited."""
    out = [f"<chat_window chat_id={quoteattr(str(chat_id))}>"]
    for m in messages:
        if m.role == "event":
            out.append(f"  <event time={quoteattr(m.created_at)}>{escape(m.text)}</event>")
            continue
        attrs = [f"id={quoteattr(str(m.msg_id if m.msg_id is not None else ''))}"]
        if m.sender_handle:
            attrs.append(f"from={quoteattr(m.sender_handle)}")
        if m.sender_name:
            attrs.append(f"name={quoteattr(m.sender_name)}")
        attrs.append(f"role={quoteattr(m.role)}")
        if m.reply_to_msg_id is not None:
            attrs.append(f"reply_to={quoteattr(str(m.reply_to_msg_id))}")
        attrs.append(f"time={quoteattr(m.created_at)}")
        if m.is_edited:
            attrs.append('edited="true"')
        body = escape(m.text)
        media = _read_media_json(m.media_json)
        kind = media.get("kind") or media.get("type")
        if isinstance(kind, str) and kind:
            emoji, description = media.get("emoji"), media.get("description")
            emoji_attr = f" emoji={quoteattr(emoji)}" if isinstance(emoji, str) and emoji else ""
            description = escape(description) if isinstance(description, str) else ""
            body += f"<media kind={quoteattr(kind)}{emoji_attr}>{description}</media>"
        if m.is_edited and m.original_seen_before_edit and m.previous_seen_text is not None:
            body += f"<previous_seen_text>{escape(m.previous_seen_text)}</previous_seen_text>"
        out.append(f"  <msg {' '.join(attrs)}>{body}</msg>")
    out.append("</chat_window>")
    return "\n".join(out)


def _normalize_handle(handle: str | None) -> str | None:
    handle = (handle or "").strip()
    if not handle:
        return None
    return handle if handle.startswith("@") else f"@{handle}"


def _row_to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        chat_id=row["chat_id"],
        msg_id=row["msg_id"],
        reply_to_msg_id=row["reply_to_msg_id"],
        sender_user_id=row["sender_user_id"],
        sender_handle=row["sender_handle"] or "",
        sender_name=row["sender_name"] or "",
        is_bot=bool(row["is_bot"]),
        role=row["role"],
        visibility_scope=row["visibility_scope"],
        text=row["text"],
        media_json=row["media_json"],
        is_edited=bool(row["is_edited"]),
        original_seen_before_edit=bool(row["original_seen_before_edit"]),
        previous_seen_text=row["previous_seen_text"],
        is_deleted=bool(row["is_deleted"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fts_phrase_query(query: str) -> str:
    """Quote each token so user text can never be parsed as FTS5 operators or column filters."""
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in query.split())


class Store:
    def __init__(self, home: Path | str, owner_id: int | None) -> None:
        self.home = Path(home)
        self.owner_id = owner_id
        self.db_path = self.home / "state.db"
        migrate_database(self.db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if owner_id is not None:
            self._conn.execute(
                "UPDATE chats SET authorization_status = 'approved' "
                "WHERE chat_id = ? AND authorization_status = 'pending_approval'",
                (str(owner_id),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- scopes -----------------------------------------------------------

    def _is_owner_chat(self, chat_id: int | str) -> bool:
        """The owner's DM: a private (positive) chat whose id is the owner's user id."""
        chat_id = str(chat_id)
        return self.owner_id is not None and chat_id == str(self.owner_id) and not chat_id.startswith("-")

    def scope_for(self, chat_id: int | str, chat_type: str) -> Scope:
        """What a chat may see. The chat decides, never the sender: the owner inside a group is `group`."""
        if chat_type != "private":
            return "group"
        return "owner_dm" if self._is_owner_chat(chat_id) else "friend_dm"

    def visibility_for(self, chat_id: int | str) -> str:
        """The messages.visibility_scope value for rows recorded in this chat."""
        return VISIBILITY_OWNER_PRIVATE if self._is_owner_chat(chat_id) else f"chat:{chat_id}"

    async def set_owner(self, owner_id: int) -> None:
        """Make `owner_id` the owner of this running store; their DM is approved from now on."""
        await self._run(self._set_owner, owner_id)

    def _set_owner(self, owner_id: int) -> None:
        with self._tx() as conn:
            self.owner_id = owner_id
            self._ensure_chat(conn, str(owner_id))
            conn.execute(
                "UPDATE chats SET authorization_status = 'approved', updated_at = ? WHERE chat_id = ?",
                (_now(), str(owner_id)),
            )

    # ---- plumbing ---------------------------------------------------------

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def locked() -> T:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _append_transcript(self, chat_id: str, event_type: str, **fields: Any) -> None:
        path = self.home / "transcripts" / f"{chat_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "event_type": event_type,
            "event_id": uuid.uuid4().hex[:8],
            "chat_id": chat_id,
            "timestamp": _now(),
            **fields,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _ensure_chat(self, conn: sqlite3.Connection, chat_id: str, scope: str | None = None) -> None:
        conn.execute(
            "INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, "group" if chat_id.startswith("-") else "private",
             scope or self.visibility_for(chat_id),
             "approved" if self._is_owner_chat(chat_id) else "pending_approval", _now()),
        )

    def _get_message(self, chat_id: str, msg_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? AND msg_id = ? AND is_deleted = 0 ORDER BY id DESC LIMIT 1",
            (chat_id, msg_id),
        ).fetchone()

    # ---- chat authorization and pending turns ----------------------------

    async def is_chat_approved(self, chat_id: int | str) -> bool:
        """Read-only: unlike get_chat_auth_status, never creates a row for an unknown chat."""
        return await self._run(self._is_chat_approved, str(chat_id))

    def _is_chat_approved(self, chat_id: str) -> bool:
        row = self._conn.execute("SELECT authorization_status FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return self._is_owner_chat(chat_id) if row is None else row["authorization_status"] == "approved"

    async def get_chat_auth_status(self, chat_id: str) -> dict[str, Any]:
        return await self._run(self._get_chat_auth_status, str(chat_id))

    def _get_chat_auth_status(self, chat_id: str) -> dict[str, Any]:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            return self._read_chat_auth(conn, chat_id)

    def _read_chat_auth(self, conn: sqlite3.Connection, chat_id: str) -> dict[str, Any]:
        row = dict(conn.execute(
            f"SELECT {_CHAT_AUTH_COLUMNS} FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone())
        row["approval_prompt_sent"] = bool(row["approval_prompt_sent"])
        return row

    async def set_chat_auth_status(
        self,
        chat_id: str,
        status: str,
        *,
        title: str | None = None,
        invited_by_handle: str | None = None,
        approval_prompt_sent: bool | None = None,
    ) -> dict[str, Any]:
        if status not in _CHAT_AUTH_STATUSES:
            raise ValueError(f"Unknown chat authorization status: {status!r}")
        return await self._run(
            self._set_chat_auth_status, str(chat_id), status, title, invited_by_handle, approval_prompt_sent
        )

    def _set_chat_auth_status(
        self, chat_id: str, status: str, title: str | None, invited_by_handle: str | None,
        approval_prompt_sent: bool | None,
    ) -> dict[str, Any]:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            conn.execute(
                """
                UPDATE chats SET authorization_status = ?, title = COALESCE(?, title),
                    invited_by_handle = COALESCE(?, invited_by_handle),
                    approval_prompt_sent = COALESCE(?, approval_prompt_sent), updated_at = ?
                WHERE chat_id = ?
                """,
                (status, title, invited_by_handle, approval_prompt_sent, _now(), chat_id),
            )
            return self._read_chat_auth(conn, chat_id)

    async def set_chat_title(self, chat_id: int | str, title: str) -> None:
        """Keep a known chat's title current (titles change, and imported chats arrive without one).
        Never creates a row."""
        if title:
            await self._run(self._set_chat_title, str(chat_id), title)

    def _set_chat_title(self, chat_id: str, title: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE chats SET title = ? WHERE chat_id = ? AND title IS NOT ?", (title, chat_id, title)
            )

    async def note_pending_chat(self, chat_id: str, title: str, invited_by: str) -> None:
        """Remember who asked and what the chat is called. Never touches the authorization status."""
        await self._run(self._note_pending_chat, str(chat_id), title, invited_by)

    def _note_pending_chat(self, chat_id: str, title: str, invited_by: str) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            conn.execute(
                "UPDATE chats SET title = ?, invited_by_handle = ?, updated_at = ? "
                "WHERE chat_id = ? AND authorization_status = 'pending_approval'",
                (title, invited_by, _now(), chat_id),
            )

    async def list_pending_chats(self) -> list[dict[str, Any]]:
        return await self._run(self._list_pending_chats)

    def _list_pending_chats(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT chat_id, title, invited_by_handle FROM chats "
            "WHERE authorization_status = 'pending_approval' ORDER BY chat_id"
        )]

    async def release_approval_prompt(self, chat_id: int | str) -> None:
        """Allow the owner prompt to be sent again after a definite delivery failure."""
        await self._run(self._release_approval_prompt, str(chat_id))

    def _release_approval_prompt(self, chat_id: str) -> None:
        self._conn.execute(
            "UPDATE chats SET approval_prompt_sent = 0 WHERE chat_id = ? AND authorization_status = 'pending_approval'",
            (chat_id,),
        )

    async def resolve_pending_chat(self, chat_id: str, status: str) -> dict[str, Any] | None:
        """Decide a chat only if it is still pending_approval. None means it was already decided."""
        if status not in _CHAT_AUTH_STATUSES:
            raise ValueError(f"Unknown chat authorization status: {status!r}")
        return await self._run(self._resolve_pending_chat, str(chat_id), status)

    def _resolve_pending_chat(self, chat_id: str, status: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE chats SET authorization_status = ?, updated_at = ? "
                "WHERE chat_id = ? AND authorization_status = 'pending_approval'",
                (status, _now(), chat_id),
            )
            return self._read_chat_auth(conn, chat_id) if cur.rowcount == 1 else None

    async def claim_approval_prompt(self, chat_id: int | str) -> bool:
        """Atomically mark the owner prompt as sent. True for exactly one caller per pending chat."""
        return await self._run(self._claim_approval_prompt, str(chat_id))

    def _claim_approval_prompt(self, chat_id: str) -> bool:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            cur = conn.execute(
                "UPDATE chats SET approval_prompt_sent = 1, updated_at = ? WHERE chat_id = ? "
                "AND approval_prompt_sent = 0 AND authorization_status = 'pending_approval'",
                (_now(), chat_id),
            )
            return cur.rowcount == 1

    async def list_chats(self, status: str | None = None) -> list[dict[str, Any]]:
        return await self._run(self._list_chats, status)

    def _list_chats(self, status: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT chat_id, authorization_status, title, preset, is_sleeping, is_offline, last_read_msg_id, updated_at "
            "FROM chats"
        )
        params = ()
        if status is not None:
            sql += " WHERE authorization_status = ?"
            params = (status,)
        chats = [dict(row) for row in self._conn.execute(sql + " ORDER BY chat_id", params)]
        for chat in chats:
            chat["is_sleeping"] = bool(chat["is_sleeping"])
            chat["is_offline"] = bool(chat["is_offline"])
        return chats

    async def get_chat_personality(self, chat_id: int | str) -> tuple[str, str]:
        return await self._run(self._get_chat_personality, str(chat_id))

    def _get_chat_personality(self, chat_id: str) -> tuple[str, str]:
        row = self._conn.execute("SELECT preset, traits_json FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return (row["preset"], row["traits_json"]) if row is not None else ("", "{}")

    async def set_chat_preset(self, chat_id: int | str, preset: str) -> bool:
        return await self._run(self._set_chat_preset, str(chat_id), preset)

    def _set_chat_preset(self, chat_id: str, preset: str) -> bool:
        return self._conn.execute(
            "UPDATE chats SET preset = ?, updated_at = ? WHERE chat_id = ? AND authorization_status = 'approved'",
            (preset, _now(), chat_id),
        ).rowcount == 1

    async def list_approved_chats(self) -> list[dict[str, Any]]:
        """Read-only (never creates a row): every approved chat with its presence columns, when it last
        saw a message and, for private chats, the other person's name. The owner's DM always comes first."""
        return await self._run(self._list_approved_chats)

    def _list_approved_chats(self) -> list[dict[str, Any]]:
        columns = ", ".join(f"c.{name}" for name in _PRESENCE_COLUMNS)
        chats = [dict(row) for row in self._conn.execute(
            f"SELECT c.chat_id, c.title, {columns}, "
            "COALESCE((SELECT MAX(created_at) FROM messages m WHERE m.chat_id = c.chat_id), c.updated_at) "
            "AS last_activity, "
            "(SELECT sender_name FROM messages m WHERE m.chat_id = c.chat_id AND m.is_bot = 0 "
            "AND m.sender_name != '' ORDER BY m.id DESC LIMIT 1) AS peer_name "
            "FROM chats c WHERE c.authorization_status = 'approved'"
        )]
        chats.sort(key=lambda chat: chat["last_activity"] or "", reverse=True)
        chats.sort(key=lambda chat: not self._is_owner_chat(chat["chat_id"]))
        if self.owner_id is not None and not any(self._is_owner_chat(c["chat_id"]) for c in chats):
            chats.insert(0, {  # approved by definition, but they have not written yet
                "chat_id": str(self.owner_id), "title": "", "is_sleeping": 0, "sleep_until": None,
                "is_offline": 0, "offline_until": None, "online_until": None, "last_activity": None, "peer_name": None,
            })
        return chats

    async def get_presence(self, chat_id: int | str) -> dict[str, Any]:
        return await self._run(self._get_presence, str(chat_id))

    def _get_presence(self, chat_id: str) -> dict[str, Any]:
        self._ensure_chat(self._conn, chat_id)
        columns = ", ".join(_PRESENCE_COLUMNS)
        return dict(self._conn.execute(f"SELECT {columns} FROM chats WHERE chat_id = ?", (chat_id,)).fetchone())

    async def set_presence(self, chat_id: int | str, **cols: Any) -> None:
        if cols.keys() - set(_PRESENCE_COLUMNS):
            raise ValueError("unknown presence column")
        if cols:
            await self._run(self._set_presence, str(chat_id), cols)

    def _set_presence(self, chat_id: str, cols: dict[str, Any]) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            assignments = ", ".join(f"{col} = ?" for col in cols)
            conn.execute(
                f"UPDATE chats SET {assignments}, updated_at = ? WHERE chat_id = ?",
                (*cols.values(), _now(), chat_id),
            )

    async def enqueue_pending_turn(self, chat_id: str, msg_id: int | None) -> None:
        await self._run(self._enqueue_pending_turn, str(chat_id), msg_id)

    def _enqueue_pending_turn(self, chat_id: str, msg_id: int | None) -> None:
        self._conn.execute(
            """
            INSERT INTO pending_turns (chat_id, first_queued_msg_id, last_queued_msg_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                first_queued_msg_id = COALESCE(
                    MIN(pending_turns.first_queued_msg_id, excluded.first_queued_msg_id),
                    pending_turns.first_queued_msg_id, excluded.first_queued_msg_id
                ),
                last_queued_msg_id = COALESCE(
                    MAX(pending_turns.last_queued_msg_id, excluded.last_queued_msg_id),
                    pending_turns.last_queued_msg_id, excluded.last_queued_msg_id
                ),
                updated_at = excluded.updated_at
            """,
            (chat_id, msg_id, msg_id, _now()),
        )

    async def complete_pending_turn(self, chat_id: str) -> None:
        await self._run(self._complete_pending_turn, str(chat_id))

    def _complete_pending_turn(self, chat_id: str) -> None:
        self._conn.execute("DELETE FROM pending_turns WHERE chat_id = ?", (chat_id,))

    async def record_turn_failure(self, chat_id: str, error: str) -> int:
        """Record a failed attempt. A missing or already completed turn returns zero."""
        return await self._run(self._record_turn_failure, str(chat_id), error)

    def _record_turn_failure(self, chat_id: str, error: str) -> int:
        row = self._conn.execute(
            "UPDATE pending_turns SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ? "
            "WHERE chat_id = ? RETURNING attempt_count",
            (error, _now(), chat_id),
        ).fetchone()
        return row[0] if row is not None else 0

    async def list_pending_turns(self, max_attempts: int = 5) -> list[dict[str, Any]]:
        return await self._run(self._list_pending_turns, max_attempts)

    def _list_pending_turns(self, max_attempts: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM pending_turns WHERE attempt_count < ? ORDER BY updated_at, chat_id",
                (max_attempts,),
            )
        ]

    async def get_bot_depth(self, chat_id: int | str) -> int:
        return await self._run(self._get_bot_depth, str(chat_id))

    def _get_bot_depth(self, chat_id: str) -> int:
        row = self._conn.execute("SELECT bot_depth FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["bot_depth"] if row is not None else 0

    async def advance_bot_depth(self, chat_id: int | str, row_id: int, previous_depth: int) -> bool:
        return await self._run(self._advance_bot_depth, str(chat_id), row_id, previous_depth)

    def _advance_bot_depth(self, chat_id: str, row_id: int, previous_depth: int) -> bool:
        # A human may have reset the depth during triage, even if the numeric value was zero.
        cur = self._conn.execute(
            "UPDATE chats SET bot_depth = bot_depth + 1 WHERE chat_id = ? "
            "AND authorization_status = 'approved' AND bot_depth = ? "
            "AND NOT EXISTS (SELECT 1 FROM messages WHERE chat_id = ? AND id > ? AND role = 'user' AND is_bot = 0)",
            (chat_id, previous_depth, chat_id, row_id),
        )
        return cur.rowcount == 1

    async def has_recent_bot_duplicate(self, msg: StoredMessage, since: datetime) -> bool:
        return await self._run(self._has_recent_bot_duplicate, msg, since.isoformat())

    def _has_recent_bot_duplicate(self, msg: StoredMessage, since: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM messages JOIN chats USING (chat_id) "
            "WHERE messages.chat_id = ? AND messages.id < ? AND sender_user_id = ? AND text = ? AND role = 'user' "
            "AND is_bot = 1 AND is_deleted = 0 AND authorization_status = 'approved' "
            "AND julianday(messages.created_at) > julianday(?) AND julianday(messages.created_at) <= julianday(?) LIMIT 1",
            (msg.chat_id, msg.id, msg.sender_user_id, msg.text, since, msg.created_at),
        ).fetchone() is not None

    # ---- messages ---------------------------------------------------------

    async def record_new_message(self, msg: StoredMessage) -> int:
        return await self._run(self._record_new_message, msg)

    def _record_new_message(self, msg: StoredMessage) -> int:
        with self._tx() as conn:
            if msg.msg_id is not None:
                existing = conn.execute(
                    "SELECT id FROM messages WHERE chat_id = ? AND msg_id = ?", (msg.chat_id, msg.msg_id)
                ).fetchone()
                if existing is not None:
                    return existing["id"]
            self._ensure_chat(conn, msg.chat_id, msg.visibility_scope)
            row_id = self._insert_message(conn, msg)
            if msg.role == "user" and not msg.is_bot:
                conn.execute("UPDATE chats SET bot_depth = 0 WHERE chat_id = ?", (msg.chat_id,))
        self._append_transcript(
            msg.chat_id,
            "message_created",
            visibility_scope=msg.visibility_scope,
            sender_name=msg.sender_name,
            sender_handle=msg.sender_handle,
            sender_user_id=msg.sender_user_id,
            msg_id=msg.msg_id,
            reply_to_msg_id=msg.reply_to_msg_id,
            role=msg.role,
            is_bot=msg.is_bot,
            text=msg.text,
            media_json=msg.media_json,
            message_created_at=msg.created_at,
        )
        return row_id

    def _insert_message(self, conn: sqlite3.Connection, msg: StoredMessage) -> int:
        cur = conn.execute(
            """
            INSERT INTO messages (
                chat_id, msg_id, reply_to_msg_id, sender_user_id, sender_handle, sender_name,
                is_bot, role, visibility_scope, text, media_json, is_edited,
                original_seen_before_edit, previous_seen_text, is_deleted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                msg.chat_id, msg.msg_id, msg.reply_to_msg_id, msg.sender_user_id,
                msg.sender_handle, msg.sender_name, int(msg.is_bot), msg.role,
                msg.visibility_scope, msg.text, msg.media_json, int(msg.is_edited),
                int(msg.original_seen_before_edit), msg.previous_seen_text,
                msg.created_at, msg.updated_at,
            ),
        )
        return int(cur.lastrowid)

    async def record_owner_event(self, text: str) -> None:
        """A one-line note in the owner's DM history (a command they ran, a button they tapped). It is owner-private,
        never unread and has no msg_id."""
        if self.owner_id is None:
            raise ValueError("there is no owner to record an event for")
        await self.record_new_message(StoredMessage(
            chat_id=str(self.owner_id), role="event", visibility_scope=VISIBILITY_OWNER_PRIVATE,
            text=text, created_at=_now(),
        ))

    async def has_message(self, chat_id: int | str, msg_id: int) -> bool:
        return await self._run(self._has_message, str(chat_id), msg_id)

    def _has_message(self, chat_id: str, msg_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id),
        ).fetchone() is not None

    async def set_media_description(self, chat_id: int | str, msg_id: int, description: str | None) -> None:
        await self._run(self._set_media_description, str(chat_id), msg_id, description)

    def _set_media_description(self, chat_id: str, msg_id: int, description: str | None) -> None:
        with self._tx() as conn:
            row = self._get_message(chat_id, msg_id)
            media = _read_media_json(row["media_json"]) if row is not None else {}
            if not media or ("description" in media and media["description"] == description):
                return
            media["description"] = description
            conn.execute(
                "UPDATE messages SET media_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(media, ensure_ascii=False), _now(), row["id"]),
            )
        self._append_transcript(
            chat_id, "media_described", msg_id=msg_id,
            description=description, visibility_scope=row["visibility_scope"],
        )

    async def record_message_edit(
        self,
        chat_id: str,
        msg_id: int,
        new_text: str,
        dom_was_online_and_read: bool | None = None,
    ) -> StoredMessage | None:
        return await self._run(self._record_message_edit, str(chat_id), msg_id, new_text, dom_was_online_and_read)

    def _record_message_edit(
        self, chat_id: str, msg_id: int, new_text: str, dom_was_online_and_read: bool | None
    ) -> StoredMessage | None:
        with self._tx() as conn:
            row = self._get_message(chat_id, msg_id)
            if row is None:
                return None
            if row["text"] == new_text:  # e.g. reaction/entity-only edit events: nothing changed
                return _row_to_message(row)

            chat = conn.execute(
                "SELECT is_offline, is_sleeping, last_read_msg_id FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            dom_saw_original = dom_was_online_and_read is True
            if chat is not None:
                dom_saw_original = (
                    dom_saw_original
                    or (
                        dom_was_online_and_read is None and not chat["is_offline"] and not chat["is_sleeping"]
                    )
                    or (chat["last_read_msg_id"] is not None and msg_id <= chat["last_read_msg_id"])
                )

            seen_before = bool(row["original_seen_before_edit"])
            previous = row["previous_seen_text"]
            if dom_saw_original:
                seen_before, previous = True, row["text"]
            # else: Dom hasn't read it. Keep any earlier "seen" record (he did read an older
            # version); a first-time offline edit stays seen=0 / previous=None.

            conn.execute(
                """
                UPDATE messages
                SET text = ?, is_edited = 1, original_seen_before_edit = ?, previous_seen_text = ?, updated_at = ?
                WHERE id = ? AND is_deleted = 0
                """,
                (new_text, int(seen_before), previous, _now(), row["id"]),
            )
            updated = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?", (row["id"],)
            ).fetchone()
        self._append_transcript(
            chat_id,
            "message_edited",
            msg_id=msg_id,
            old_text=row["text"],
            new_text=new_text,
            original_seen_before_edit=seen_before,
        )
        return _row_to_message(updated)

    async def record_message_delete(self, chat_id: str, msg_ids: list[int]) -> int:
        return await self._run(self._record_message_delete, str(chat_id), list(msg_ids))

    def _record_message_delete(self, chat_id: str, msg_ids: list[int]) -> int:
        if not msg_ids:
            return 0
        marks = ",".join("?" * len(msg_ids))
        with self._tx() as conn:
            hit = [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT msg_id FROM messages WHERE chat_id = ? AND is_deleted = 0 AND msg_id IN ({marks})",
                    (chat_id, *msg_ids),
                )
            ]
            if not hit:
                return 0
            now = _now()
            hit_marks = ",".join("?" * len(hit))
            cur = conn.execute(
                f"UPDATE messages SET is_deleted = 1, deleted_at = ?, updated_at = ? "
                f"WHERE chat_id = ? AND is_deleted = 0 AND msg_id IN ({hit_marks})",
                (now, now, chat_id, *hit),
            )
            count = cur.rowcount
        self._append_transcript(chat_id, "message_deleted", msg_ids=sorted(hit), deleted_at=now)
        return count

    async def mark_chat_read(self, chat_id: str, up_to_msg_id: int) -> None:
        await self._run(self._mark_chat_read, str(chat_id), up_to_msg_id)

    def _mark_chat_read(self, chat_id: str, up_to_msg_id: int) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            conn.execute(
                "UPDATE chats SET last_read_msg_id = MAX(COALESCE(last_read_msg_id, 0), ?), updated_at = ? "
                "WHERE chat_id = ?",
                (up_to_msg_id, _now(), chat_id),
            )

    # ---- reading ----------------------------------------------------------

    async def get_unread(self, chat_id: int | str) -> tuple[int, StoredMessage | None]:
        return await self._run(self._get_unread, str(chat_id))

    def _get_unread(self, chat_id: str) -> tuple[int, StoredMessage | None]:
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        row = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS}, COUNT(*) OVER () AS unread_count FROM messages "
            "WHERE chat_id = ? AND is_deleted = 0 AND role = 'user' "
            "AND msg_id > COALESCE((SELECT last_read_msg_id FROM chats WHERE chat_id = ?), 0)"
            f"{hide} ORDER BY msg_id DESC LIMIT 1",
            (chat_id, chat_id),
        ).fetchone()
        return (row["unread_count"], _row_to_message(row)) if row is not None else (0, None)

    async def has_unread_human(self, chat_id: int | str) -> bool:
        return await self._run(self._has_unread_human, str(chat_id))

    def _has_unread_human(self, chat_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND is_deleted = 0 AND role = 'user' AND is_bot = 0 "
            "AND msg_id > COALESCE((SELECT last_read_msg_id FROM chats WHERE chat_id = ?), 0) LIMIT 1",
            (chat_id, chat_id),
        ).fetchone() is not None

    async def get_unread_media_to_describe(self, chat_id: int | str, limit: int) -> list[tuple[int, dict]]:
        """(msg_id, media) of undescribed attachments from people among the last `limit` unread messages."""
        return await self._run(self._get_unread_media_to_describe, str(chat_id), limit)

    def _get_unread_media_to_describe(self, chat_id: str, limit: int) -> list[tuple[int, dict]]:
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        rows = self._conn.execute(
            "SELECT msg_id, media_json, is_bot FROM messages "
            "WHERE chat_id = ? AND is_deleted = 0 AND role = 'user' "
            "AND msg_id > COALESCE((SELECT last_read_msg_id FROM chats WHERE chat_id = ?), 0)"
            f"{hide} ORDER BY msg_id DESC LIMIT ?",
            (chat_id, chat_id, limit),
        ).fetchall()
        found = []
        for row in reversed(rows):
            media = _read_media_json(row["media_json"])
            if media and not row["is_bot"] and media.get("description") is None:
                found.append((row["msg_id"], media))
        return found

    async def get_recent_chat_window(
        self, chat_id: int | str, limit: int = 35, *, through_id: int | None = None,
    ) -> list[StoredMessage]:
        return await self._run(self._get_recent_chat_window, str(chat_id), limit, through_id)

    def _get_recent_chat_window(self, chat_id: str, limit: int, through_id: int | None) -> list[StoredMessage]:
        # Rows labelled private (e.g. from a legacy import) never enter another chat's window.
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND is_deleted = 0{hide} "
            "AND (? IS NULL OR role != 'user' OR id <= ?) ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
            (chat_id, through_id, through_id, max(0, limit)),
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    format_chat_window_xml = staticmethod(format_chat_window_xml)

    async def hourly_message_counts(
        self, days: int = 14, tz: tzinfo = timezone.utc, *, now: datetime | None = None,
    ) -> list[int]:
        """Count approved chats' incoming human messages by local hour, reading timestamps only."""
        end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = end - timedelta(days=days)
        return await self._run(self._hourly_message_counts, start.isoformat(), end.isoformat(), tz)

    def _hourly_message_counts(self, start: str, end: str, tz: tzinfo) -> list[int]:
        rows = self._conn.execute(
            "SELECT m.created_at FROM messages m JOIN chats c ON c.chat_id = m.chat_id "
            "WHERE c.authorization_status = 'approved' AND m.role = 'user' "
            "AND m.is_bot = 0 AND m.is_deleted = 0 "
            "AND julianday(m.created_at) >= julianday(?) AND julianday(m.created_at) <= julianday(?)",
            (start, end),
        )
        counts = [0] * 24
        for (at,) in rows:
            time = datetime.fromisoformat(at)
            if time.tzinfo is None:
                time = time.replace(tzinfo=timezone.utc)
            counts[time.astimezone(tz).hour] += 1
        return counts

    async def kv_get(self, key: str) -> str | None:
        return await self._run(self._kv_get, key)

    def _kv_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._run(
            self._conn.execute,
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )

    # ---- usage ------------------------------------------------------------

    async def record_usage(
        self, at: str, kind: str, model: str, host: str, chat_id: int | None,
        input_tokens: int, output_tokens: int, cost_usd: float, fallback: int,
    ) -> None:
        await self._run(
            self._conn.execute,
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, kind, model, host, chat_id, input_tokens, output_tokens, cost_usd, fallback),
        )

    async def usage_totals(self, start: str, end: str) -> tuple[int, float]:
        return await self._run(self._usage_totals, start, end)

    def _usage_totals(self, start: str, end: str) -> tuple[int, float]:
        count, cost = self._conn.execute(
            "SELECT COUNT(*), TOTAL(cost_usd) FROM usage WHERE at >= ? AND at < ?", (start, end),
        ).fetchone()
        return count, cost

    async def claim_spend_cap_notice(self, day: str) -> bool:
        return await self._run(self._claim_spend_cap_notice, day)

    def _claim_spend_cap_notice(self, day: str) -> bool:
        return self._conn.execute("INSERT OR IGNORE INTO spend_cap_notices VALUES (?)", (day,)).rowcount == 1

    # ---- memory pass --------------------------------------------------------

    def _memory_filter(self, chat_id: str) -> str:
        """Rows the memory pass may read: messages past its cursor, never a private row in another chat."""
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        return (
            "chat_id = ? AND is_deleted = 0 AND role != 'event' AND "
            f"msg_id > (SELECT memory_cursor_msg_id FROM chats WHERE chat_id = ?){hide}"
        )

    async def get_memory_cursor(self, chat_id: int | str) -> int:
        return await self._run(self._get_memory_cursor, str(chat_id))

    def _get_memory_cursor(self, chat_id: str) -> int:
        row = self._conn.execute("SELECT memory_cursor_msg_id FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row is not None else 0

    async def advance_memory_cursor(self, chat_id: int | str, msg_id: int) -> None:
        """Mark messages up to `msg_id` as read by the memory pass. Never moves backwards."""
        await self._run(self._advance_memory_cursor, str(chat_id), msg_id)

    def _advance_memory_cursor(self, chat_id: str, msg_id: int) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            conn.execute(
                "UPDATE chats SET memory_cursor_msg_id = MAX(memory_cursor_msg_id, ?) WHERE chat_id = ?",
                (msg_id, chat_id),
            )

    async def count_new_for_memory(self, chat_id: int | str) -> int:
        """Messages (not events) the memory pass has not read yet."""
        return await self._run(self._count_new_for_memory, str(chat_id))

    def _count_new_for_memory(self, chat_id: str) -> int:
        return self._conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {self._memory_filter(chat_id)}", (chat_id, chat_id)
        ).fetchone()[0]

    async def get_memory_batch(self, chat_id: int | str, limit: int) -> list[StoredMessage]:
        """The newest `limit` messages past the memory cursor, oldest first. In the owner's DM the events
        between them come along, so the pass sees what the owner did as well as what they said."""
        return await self._run(self._get_memory_batch, str(chat_id), limit)

    def _get_memory_batch(self, chat_id: str, limit: int) -> list[StoredMessage]:
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {self._memory_filter(chat_id)} "
            "ORDER BY msg_id DESC LIMIT ?",
            (chat_id, chat_id, max(0, limit)),
        ).fetchall()[::-1]
        if rows and self._is_owner_chat(chat_id):
            rows += self._conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND role = 'event' AND id > ? AND id < ? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, rows[0]["id"], max(r["id"] for r in rows), max(0, limit)),
            ).fetchall()
            rows.sort(key=lambda r: r["id"])
        return [_row_to_message(r) for r in rows]

    async def search_chat_history(
        self, query: str, requester_chat_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        return await self._run(self._search_chat_history, query, str(requester_chat_id), limit)

    def _search_chat_history(self, query: str, requester_chat_id: str, limit: int) -> list[dict[str, Any]]:
        match = _fts_phrase_query(query)
        if not match:
            return []
        sql = (
            "SELECT m.id, m.chat_id, m.msg_id, m.sender_handle, m.sender_name, m.role, "
            "m.visibility_scope, m.text, m.created_at "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ? AND m.is_deleted = 0"
        )
        params: list[Any] = [match]
        if not self._is_owner_chat(requester_chat_id):  # the owner's DM searches every chat
            sql += f" AND m.chat_id = ? AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
            params.append(requester_chat_id)
        sql += " ORDER BY messages_fts.rank LIMIT ?"
        params.append(max(1, min(limit, 100)))
        return [dict(r) for r in self._conn.execute(sql, params)]

    # ---- identity ---------------------------------------------------------

    async def resolve_person(
        self, telegram_user_id: int, handle: str | None, display_name: str
    ) -> PersonIdentity:
        return await self._run(self._resolve_person, telegram_user_id, handle, display_name)

    def _resolve_person(self, uid: int, handle: str | None, display_name: str) -> PersonIdentity:
        handle = _normalize_handle(handle)
        profile_path = f"memory/people/{uid}.md"
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM people_identities WHERE telegram_user_id = ?", (uid,)).fetchone()
            if row is None and handle:
                # Legacy profile created from a handle only: bind it to the immutable id on first sight
                # and move its notes to the uid-keyed file.
                row = conn.execute(
                    "SELECT * FROM people_identities WHERE telegram_user_id IS NULL "
                    "AND lower(current_handle) = lower(?) LIMIT 1",
                    (handle,),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE people_identities SET telegram_user_id = ? WHERE person_key = ?",
                        (uid, row["person_key"]),
                    )
                    old, new = self.home / row["profile_path"], self.home / profile_path
                    if old.is_file() and not new.exists():
                        new.parent.mkdir(parents=True, exist_ok=True)
                        old.rename(new)

            if row is None:
                return self._create_person(conn, uid, handle, display_name)

            known = json.loads(row["known_handles_json"] or "[]")
            current = row["current_handle"] or ""
            if handle and handle != current:
                current = handle
            if handle and handle not in known:
                known.append(handle)
            name = display_name or row["display_name"] or ""
            conn.execute(
                "UPDATE people_identities SET current_handle = ?, known_handles_json = ?, display_name = ?, "
                "profile_path = ?, updated_at = ? WHERE person_key = ?",
                (current, json.dumps(known), name, profile_path, _now(), row["person_key"]),
            )
            row = conn.execute(
                "SELECT * FROM people_identities WHERE person_key = ?", (row["person_key"],)
            ).fetchone()
        return self._row_to_person(row)

    def _create_person(
        self, conn: sqlite3.Connection, uid: int, handle: str | None, display_name: str
    ) -> PersonIdentity:
        person_key = f"uid_{uid}"
        rel_path = f"memory/people/{uid}.md"
        known = [handle] if handle else []
        name = display_name or handle or str(uid)
        path = self.home / rel_path
        if not path.exists():  # the notes file belongs to Dom: never overwrite it
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {name}" + (f" ({handle})" if handle else "") + "\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO people_identities (person_key, telegram_user_id, current_handle, known_handles_json, "
            "display_name, preferred_name, profile_path, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (person_key, uid, handle or "", json.dumps(known), name, name, rel_path, _now()),
        )
        return PersonIdentity(
            person_key=person_key,
            telegram_user_id=uid,
            current_handle=handle or "",
            known_handles=known,
            display_name=name,
            preferred_name=name,
            profile_path=rel_path,
        )

    @staticmethod
    def _row_to_person(row: sqlite3.Row) -> PersonIdentity:
        return PersonIdentity(
            person_key=row["person_key"],
            telegram_user_id=row["telegram_user_id"],
            current_handle=row["current_handle"] or "",
            known_handles=json.loads(row["known_handles_json"] or "[]"),
            display_name=row["display_name"] or "",
            preferred_name=row["preferred_name"] or "",
            profile_path=row["profile_path"],
        )

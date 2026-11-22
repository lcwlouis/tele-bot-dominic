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
    (9, "0008_proposals_reminders.sql"),
    (10, "0009_turn_log.sql"),
    (11, "0010_desktop_tasks.sql"),
    (12, "0011_plugins.sql"),
    (13, "0012_t3_audit.sql"),
    (14, "0013_lan_address_proposals.sql"),
    (15, "0014_desktop_containers.sql"),
    (16, "0015_self_work.sql"),
    (19, "0018_plugin_storage.sql"),
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


def _display_time(stamp: str, tz: tzinfo | None) -> str:
    """Message times in the owner's local time, matching the turn note's "It is …" line.
    Mixing UTC stamps with a local "now" made the model misjudge how long ago things happened."""
    if tz is None:
        return stamp
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def format_chat_window_xml(chat_id: str, messages: list[StoredMessage], tz: tzinfo | None = None) -> str:
    """Render messages as ``<chat_window>`` XML. ``previous_seen_text`` is shown only when
    Dom actually saw the original before it was edited. With ``tz``, times are local."""
    out = [f"<chat_window chat_id={quoteattr(str(chat_id))}>"]
    for m in messages:
        if m.role == "event":
            out.append(f"  <event time={quoteattr(_display_time(m.created_at, tz))}>{escape(m.text)}</event>")
            continue
        attrs = [f"id={quoteattr(str(m.msg_id if m.msg_id is not None else ''))}"]
        if m.sender_handle:
            attrs.append(f"from={quoteattr(m.sender_handle)}")
        if m.sender_name:
            attrs.append(f"name={quoteattr(m.sender_name)}")
        attrs.append(f"role={quoteattr(m.role)}")
        if m.reply_to_msg_id is not None:
            attrs.append(f"reply_to={quoteattr(str(m.reply_to_msg_id))}")
        attrs.append(f"time={quoteattr(_display_time(m.created_at, tz))}")
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
            "SELECT c.chat_id, c.title, c.invited_by_handle, (SELECT m.text FROM messages m WHERE m.chat_id = c.chat_id "
            "AND m.role = 'user' AND m.text IS NOT NULL AND m.text != '' ORDER BY m.id LIMIT 1) AS first_text FROM chats c "
            "WHERE c.authorization_status = 'pending_approval' ORDER BY c.chat_id"
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

    async def set_chat_traits(self, chat_id: int | str, traits: dict[str, float]) -> bool:
        return await self._run(self._set_chat_traits, str(chat_id), traits)

    def _set_chat_traits(self, chat_id: str, traits: dict[str, float]) -> bool:
        return self._conn.execute(
            "UPDATE chats SET traits_json = ?, updated_at = ? WHERE chat_id = ? AND authorization_status = 'approved'",
            (json.dumps(traits), _now(), chat_id),
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

    async def set_presence(
        self, chat_id: int | str, *, observed_at: datetime | None = None, start_session: bool = False, **cols: Any,
    ) -> None:
        if cols.keys() - set(_PRESENCE_COLUMNS):
            raise ValueError("unknown presence column")
        if cols:
            await self._run(self._set_presence, str(chat_id), cols, observed_at, start_session)

    def _set_presence(
        self, chat_id: str, cols: dict[str, Any], observed_at: datetime | None, start_session: bool,
    ) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            if observed_at is not None and "online_until" in cols:
                self._record_online_session(conn, chat_id, observed_at, cols["online_until"], start_session)
            assignments = ", ".join(f"{col} = ?" for col in cols)
            conn.execute(
                f"UPDATE chats SET {assignments}, updated_at = ? WHERE chat_id = ?",
                (*cols.values(), _now(), chat_id),
            )

    @staticmethod
    def _record_online_session(
        conn: sqlite3.Connection, chat_id: str, observed_at: datetime, deadline: str | None, start_session: bool,
    ) -> None:
        at = observed_at.astimezone(timezone.utc).isoformat()
        ends = datetime.fromisoformat(deadline).astimezone(timezone.utc).isoformat() if deadline else None
        previous = conn.execute("SELECT online_until FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        try:
            previous = datetime.fromisoformat(previous) if previous else None
        except ValueError:
            previous = None  # some older stores have no usable presence deadline
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            previous = previous.astimezone(timezone.utc).isoformat()
            if ends is not None and not start_session:
                new_end = ends
            else:
                new_end = min(previous, at)
            conn.execute(
                "UPDATE online_sessions SET ends_at = MAX(started_at, ?) WHERE id = "
                "(SELECT id FROM online_sessions WHERE chat_id = ? AND ends_at = ? ORDER BY id DESC LIMIT 1)",
                (new_end, chat_id, previous),
            )
        if start_session and ends is not None and ends > at:
            conn.execute(
                "INSERT INTO online_sessions (chat_id, started_at, ends_at) VALUES (?, ?, ?)",
                (chat_id, at, ends),
            )

    async def online_sessions(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        rows = await self._run(lambda: self._conn.execute(
            "SELECT started_at, ends_at FROM online_sessions WHERE ends_at > ? AND started_at < ? ORDER BY started_at",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall())
        return [(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])) for row in rows]

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

    async def record_t3(
        self, chat_id: int, tool: str, machine: str, target: str, summary: str, outcome: str, error: str,
    ) -> int:
        def insert() -> int:
            cursor = self._conn.execute(
                """INSERT INTO t3_audit (at, chat_id, tool, machine, target, summary, outcome, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now(), str(chat_id), tool, machine[:200], target[:200], summary, outcome, error[:200]),
            )
            return int(cursor.lastrowid)

        return await self._run(insert)

    async def finish_t3(self, audit_id: int, outcome: str, error: str) -> None:
        await self._run(self._conn.execute, "UPDATE t3_audit SET outcome = ?, error = ? WHERE id = ?",
                        (outcome, error[:200], audit_id))

    async def t3_history(self, chat_id: int) -> list[dict[str, Any]]:
        def select() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM t3_audit WHERE chat_id = ? ORDER BY id DESC LIMIT 15", (str(chat_id),),
            )
            return [dict(row) for row in rows]

        return await self._run(select)

    async def prune_t3(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        await self._run(self._conn.execute, "DELETE FROM t3_audit WHERE at < ?", (cutoff,))

    async def record_owner_event(self, text: str) -> None:
        """A one-line note in the owner's DM history (a command they ran, a button they tapped). It is owner-private,
        never unread and has no msg_id."""
        if self.owner_id is None:
            raise ValueError("there is no owner to record an event for")
        await self.record_new_message(StoredMessage(
            chat_id=str(self.owner_id), role="event", visibility_scope=VISIBILITY_OWNER_PRIVATE,
            text=text, created_at=_now(),
        ))

    async def record_chat_event(self, chat_id: int | str, text: str) -> None:
        """A one-line note for Dom in one chat's history (no msg_id, never unread). Only the main model sees it."""
        await self.record_new_message(StoredMessage(
            chat_id=str(chat_id), role="event", visibility_scope=self.visibility_for(chat_id), text=text, created_at=_now(),
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

    async def latest_chat_message(self, chat_id: int | str) -> tuple[int, str] | None:
        """Latest visible Telegram message id and timestamp, excluding owner events."""
        return await self._run(self._latest_chat_message, str(chat_id))

    def _latest_chat_message(self, chat_id: str) -> tuple[int, str] | None:
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        row = self._conn.execute(
            "SELECT msg_id, created_at FROM messages WHERE chat_id = ? AND msg_id IS NOT NULL "
            f"AND is_deleted = 0 AND role IN ('user', 'assistant'){hide} ORDER BY msg_id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return (row["msg_id"], row["created_at"]) if row is not None else None

    async def get_recent_chat_window(
        self, chat_id: int | str, limit: int = 35, *, through_id: int | None = None,
    ) -> list[StoredMessage]:
        return await self._run(self._get_recent_chat_window, str(chat_id), limit, through_id)

    def _window_where(self, chat_id: str, through_id: int | None) -> tuple[str, tuple]:
        # Rows labelled private (e.g. from a legacy import) never enter another chat's window.
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        return (
            f"chat_id = ? AND is_deleted = 0{hide} AND (? IS NULL OR role != 'user' OR id <= ?)",
            (chat_id, through_id, through_id),
        )

    def _get_recent_chat_window(self, chat_id: str, limit: int, through_id: int | None) -> list[StoredMessage]:
        where, args = self._window_where(chat_id, through_id)
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
            (*args, max(0, limit)),
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    async def get_window_from(
        self, chat_id: int | str, start: tuple[int, str] | None, limit: int, *, through_id: int | None = None,
    ) -> list[StoredMessage]:
        """The messages of a chat from `start` (a message's row id and created_at, which stay valid even if that
        row is later deleted or edited) up to the newest, oldest first; at most the newest `limit` of them."""
        return await self._run(self._get_window_from, str(chat_id), start, limit, through_id)

    def _get_window_from(
        self, chat_id: str, start: tuple[int, str] | None, limit: int, through_id: int | None,
    ) -> list[StoredMessage]:
        where, args = self._window_where(chat_id, through_id)
        if start is not None:
            where += " AND (julianday(created_at), id) >= (julianday(?), ?)"
            args += (start[1], start[0])
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
            (*args, max(0, limit)),
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    format_chat_window_xml = staticmethod(format_chat_window_xml)

    async def reflection_messages(self, start: datetime, end: datetime, *, private: bool) -> str:
        return await self._run(self._reflection_messages, start.isoformat(), end.isoformat(), private)

    def _reflection_messages(self, start: str, end: str, private: bool) -> str:
        owner = str(self.owner_id)
        bounds = (
            "m.is_deleted = 0 AND julianday(m.created_at) > julianday(?) AND julianday(m.created_at) <= julianday(?)"
        )
        if private:
            if self.owner_id is None or self._conn.execute(
                f"SELECT 1 FROM messages m WHERE {bounds} AND m.chat_id = ? "
                "AND m.role IN ('user', 'assistant') LIMIT 1",
                (start, end, owner),
            ).fetchone() is None:
                return ""
            scope = "m.chat_id = ? AND m.role IN ('user', 'assistant', 'event')"
        else:
            scope = (
                "CAST(m.chat_id AS INTEGER) < 0 AND c.authorization_status = 'approved' "
                "AND m.role IN ('user', 'assistant') "
                f"AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
            )
        rows = self._conn.execute(
            "SELECT m.role, m.text, COALESCE(NULLIF(c.title, ''), m.chat_id) "
            f"FROM messages m JOIN chats c ON c.chat_id = m.chat_id WHERE {bounds} AND {scope} "
            "ORDER BY julianday(m.created_at) DESC, m.id DESC",
            (start, end, owner) if private else (start, end),
        )
        parts = []
        remaining = 12000
        for role, text, title in rows:
            header = f"<message chat={quoteattr(title[:256])} role={quoteattr(role)}>"
            footer = "</message>"
            room = remaining - len(header) - len(footer)
            if room <= 0:
                break
            body = escape(text[:room])[:room]
            body = re.sub(r"&[^;]*$", "", body)  # keep any final XML entity whole
            parts.append(header + body + footer)
            remaining -= len(parts[-1]) + 1  # newline between messages
        return "\n".join(parts)

    async def hourly_message_counts(
        self, days: int = 14, tz: tzinfo = timezone.utc, *, now: datetime | None = None,
    ) -> list[int]:
        """Count approved chats' active (sender, local date, local hour) buckets by local hour, reading timestamps only.

        A burst of messages is one bucket, and with fewer than two distinct senders every count is zero."""
        end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = end - timedelta(days=days)
        return await self._run(self._hourly_message_counts, start.isoformat(), end.isoformat(), tz)

    def _hourly_message_counts(self, start: str, end: str, tz: tzinfo) -> list[int]:
        rows = self._conn.execute(
            "SELECT m.created_at, COALESCE(m.sender_user_id, m.chat_id) FROM messages m "
            "JOIN chats c ON c.chat_id = m.chat_id "
            "WHERE c.authorization_status = 'approved' AND m.role = 'user' "
            "AND m.is_bot = 0 AND m.is_deleted = 0 "
            "AND julianday(m.created_at) >= julianday(?) AND julianday(m.created_at) <= julianday(?)",
            (start, end),
        )
        buckets, senders = set(), set()
        for at, sender in rows:
            time = datetime.fromisoformat(at)
            if time.tzinfo is None:
                time = time.replace(tzinfo=timezone.utc)
            local = time.astimezone(tz)
            buckets.add((sender, local.date(), local.hour))
            senders.add(sender)
        counts = [0] * 24
        if len(senders) >= 2:  # one person alone cannot say when everyone sleeps
            for _, _, hour in buckets:
                counts[hour] += 1
        return counts

    async def kv_get(self, key: str) -> str | None:
        return await self._run(self._kv_get, key)

    def _kv_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.kv_set_many({key: value})

    async def kv_set_many(self, values: dict[str, str]) -> None:
        await self._run(self._kv_set_many, values)

    def _kv_set_many(self, values: dict[str, str]) -> None:
        with self._tx() as conn:
            self._write_kv(conn, values)

    def _write_kv(self, conn: sqlite3.Connection, values: dict[str, str]) -> None:
        now = _now()
        conn.executemany(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [(key, value, now) for key, value in values.items()],
        )

    # ---- turn log ---------------------------------------------------------

    async def record_turn(
        self, at: datetime, chat_id: int, scope: Scope, calls: int, trace: list[str], model: str, fallback: bool,
        duration_ms: int, outcome: str,
    ) -> None:
        await self._run(
            self._conn.execute,
            "INSERT INTO turns (at, chat_id, scope, calls, trace, model, fallback, duration_ms, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at.astimezone(timezone.utc).isoformat(), str(chat_id), scope, calls, json.dumps(trace), model,
             int(fallback), duration_ms, outcome),
        )

    async def turn_counts(self, start: datetime, end: datetime) -> tuple[int, int]:
        """Count turns and replies in the half-open interval [start, end)."""
        bounds = (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())

        def count() -> tuple[int, int]:
            row = self._conn.execute(
                "SELECT COUNT(*), COUNT(CASE WHEN outcome GLOB 'Replied*' THEN 1 END) "
                "FROM turns WHERE at >= ? AND at < ?",
                bounds,
            ).fetchone()
            return row[0], row[1]

        return await self._run(count)

    async def recent_turns(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM turns WHERE (? IS NULL OR chat_id = ?) ORDER BY at DESC, id DESC LIMIT 200",
                (chat_id, chat_id),
            )
        ])

    async def recent_turn_errors(self) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT at, scope, model, outcome FROM turns WHERE outcome LIKE 'Turn failed%' OR outcome LIKE 'Error%' "
                "ORDER BY at DESC, id DESC LIMIT 20"
            )
        ])

    async def prune_turn_history(self, before: datetime) -> None:
        cutoff = before.astimezone(timezone.utc).isoformat()

        def prune() -> None:
            with self._tx() as conn:
                conn.execute("DELETE FROM turns WHERE at < ?", (cutoff,))
                conn.execute("DELETE FROM online_sessions WHERE ends_at < ?", (cutoff,))

        await self._run(prune)

    # ---- desktop tasks -----------------------------------------------------

    async def enqueue_desktop_task(self, chat_id: int, brief: str, container: str = "default") -> int | None:
        """Queue a task on a container; None if that container already has an active task."""
        def enqueue() -> int | None:
            if chat_id != self.owner_id:
                return None
            row = self._conn.execute(
                "INSERT INTO desktop_tasks (chat_id, brief, status, created_at, container) VALUES (?, ?, 'queued', ?, ?) "
                "ON CONFLICT DO NOTHING RETURNING id", (chat_id, brief, _now(), container),
            ).fetchone()
            return row["id"] if row else None

        return await self._run(enqueue)

    async def desktop_tasks(
        self, chat_id: int, *, active: bool = False, container: str | None = None,
    ) -> list[dict[str, Any]]:
        def select() -> list[dict[str, Any]]:
            if chat_id != self.owner_id:
                return []
            condition = "AND status IN ('queued', 'running', 'waiting_owner')" if active else ""
            if container is not None:
                condition += " AND container = ?"
            return [dict(row) for row in self._conn.execute(
                f"SELECT * FROM desktop_tasks WHERE chat_id = ? {condition} ORDER BY id DESC LIMIT 10",
                (chat_id, *([container] if container is not None else [])),
            )]

        return await self._run(select)

    async def update_desktop_task(
        self, task_id: int, chat_id: int, expected: tuple[str, ...], status: str, *,
        steps: int | None = None, result: str | None = None, error: str | None = None,
        expected_steps: int | None = None,
    ) -> dict[str, Any] | None:
        def update() -> dict[str, Any] | None:
            if chat_id != self.owner_id:
                return None
            now = _now()
            placeholders = ", ".join("?" for _ in expected)
            row = self._conn.execute(
                "UPDATE desktop_tasks SET status = ?, steps = coalesce(?, steps), result = ?, error = ?, "
                "started_at = CASE WHEN ? = 'running' THEN coalesce(started_at, ?) ELSE started_at END, "
                "finished_at = CASE WHEN ? IN ('done', 'failed', 'cancelled') THEN ? ELSE finished_at END "
                f"WHERE id = ? AND chat_id = ? AND status IN ({placeholders}) "
                "AND (? IS NULL OR steps = ?) RETURNING *",
                (status, steps, result, error, status, now, status, now, task_id, chat_id, *expected,
                 expected_steps, expected_steps),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(update)

    # ---- self-directed tasks (W13) ----------------------------------------

    async def add_self_tasks(self, tasks: list[dict[str, Any]], announced_at: datetime, not_before: datetime) -> list[dict[str, Any]]:
        """Record a new plan. Tasks of an earlier plan that never started expire: one plan is live at a time."""
        def add() -> list[dict[str, Any]]:
            with self._tx() as conn:
                conn.execute(
                    "UPDATE self_tasks SET status = 'expired', finished_at = ? WHERE status = 'planned'", (_now(),),
                )
                return [dict(conn.execute(
                    "INSERT INTO self_tasks (goal, why, tools_json, sites_json, minutes, steps_budget, announced_at, not_before) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                    (task["goal"], task["why"], json.dumps(task["tools"]), json.dumps(task["sites"]), task["minutes"],
                     task["steps"], announced_at.isoformat(), not_before.isoformat()),
                ).fetchone()) for task in tasks]

        return await self._run(add)

    async def schedule_self_tasks(self, task_ids: list[int], not_before: datetime) -> None:
        """Set the start time of announced tasks that are still planned."""
        placeholders = ", ".join("?" for _ in task_ids)
        await self._run(lambda: self._conn.execute(
            f"UPDATE self_tasks SET not_before = ? WHERE status = 'planned' AND id IN ({placeholders})",
            (not_before.isoformat(), *task_ids),
        ))

    async def self_tasks(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run(lambda: [dict(row) for row in self._conn.execute(
            "SELECT * FROM self_tasks WHERE (? IS NULL OR status = ?) ORDER BY id DESC LIMIT ?", (status, status, limit),
        )])

    async def update_self_task(
        self, task_id: int, expected: tuple[str, ...], status: str, *, steps: int | None = None,
        result: str | None = None, error: str | None = None, files: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Move a task from one of `expected` to `status`; None if it was not in one of them."""
        def update() -> dict[str, Any] | None:
            now = _now()
            placeholders = ", ".join("?" for _ in expected)
            row = self._conn.execute(
                "UPDATE self_tasks SET status = ?, steps = coalesce(?, steps), result = coalesce(?, result), "
                "error = coalesce(?, error), files_json = coalesce(?, files_json), "
                "started_at = CASE WHEN ? = 'running' THEN coalesce(started_at, ?) ELSE started_at END, "
                "finished_at = CASE WHEN ? NOT IN ('planned', 'running') THEN ? ELSE finished_at END "
                f"WHERE id = ? AND status IN ({placeholders}) RETURNING *",
                (status, steps, result, error, None if files is None else json.dumps(files), status, now, status, now,
                 task_id, *expected),
            ).fetchone()
            return dict(row) if row else None

        return await self._run(update)

    async def expire_self_tasks(self, announced_before: datetime) -> None:
        await self._run(lambda: self._conn.execute(
            "UPDATE self_tasks SET status = 'expired', finished_at = ? WHERE status = 'planned' AND announced_at < ?",
            (_now(), announced_before.isoformat()),
        ))

    # ---- plugins ---------------------------------------------------------

    async def stage_plugins(self, manifests: list[tuple[str, str, str]]) -> None:
        def stage() -> None:
            with self._tx() as conn:
                # Older versions ignored the storage manifest key. Require a new review if those
                # exact bytes previously received approval without this capability.
                for name, digest, manifest in manifests:
                    if json.loads(manifest).get("storage") and conn.execute(
                        "UPDATE plugins SET status = 'pending', manifest_json = ?, test_run_json = NULL, "
                        "updated_at = ? WHERE name = ? AND hash = ? AND status != 'disabled' "
                        "AND json_type(manifest_json, '$.storage') IS NULL",
                        (manifest, _now(), name, digest),
                    ).rowcount:
                        conn.execute("DELETE FROM kv WHERE key = ?", (f"plugin-reviewed:{name}:{digest}",))
                conn.executemany(
                    "INSERT INTO plugins (name, status, hash, manifest_json, updated_at) "
                    "VALUES (?, 'pending', ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                    "status = CASE WHEN plugins.status IN ('approved', 'disabled') THEN 'pending' ELSE plugins.status END, "
                    "hash = excluded.hash, manifest_json = excluded.manifest_json, updated_at = excluded.updated_at, "
                    "test_run_json = NULL "
                    "WHERE plugins.hash != excluded.hash",
                    [(name, digest, manifest, _now()) for name, digest, manifest in manifests],
                )
                conn.executemany(
                    "UPDATE proposals SET status = 'expired', decided_at = ? WHERE kind = 'plugin' "
                    "AND status = 'pending' AND json_extract(body_json, '$.name') = ? "
                    "AND json_extract(body_json, '$.hash') != ?",
                    [(_now(), name, digest) for name, digest, _ in manifests],
                )

        await self._run(stage)

    async def list_plugins(self) -> list[dict[str, Any]]:
        return await self._run(lambda: [dict(row) for row in self._conn.execute("SELECT * FROM plugins ORDER BY name")])

    async def set_plugin_test_run(self, name: str, digest: str, result: dict) -> None:
        await self._run(lambda: self._conn.execute(
            "UPDATE plugins SET test_run_json = ? WHERE name = ? AND hash = ?",
            (json.dumps(result), name, digest),
        ))

    def read_plugin_storage(self, name: str, digest: str) -> str | None:
        """Read state for a pinned approved version, from the sandbox worker thread."""
        with self._lock:
            row = self._conn.execute(
                "SELECT plugin_storage.value_json FROM plugins LEFT JOIN plugin_storage USING (name) "
                "WHERE name = ? AND hash = ? AND approved_hash = ? AND status = 'approved'",
                (name, digest, digest),
            ).fetchone()
            if row is None:
                raise ValueError("Plugin is no longer approved.")
            return row[0]

    def write_plugin_storage(self, name: str, digest: str, value_json: str) -> None:
        """Commit state only if this exact version is still approved."""
        with self._lock:
            updated = self._conn.execute(
                "INSERT INTO plugin_storage (name, value_json) SELECT name, ? FROM plugins "
                "WHERE name = ? AND hash = ? AND approved_hash = ? AND status = 'approved' "
                "ON CONFLICT(name) DO UPDATE SET value_json = excluded.value_json",
                (value_json, name, digest, digest),
            )
            if updated.rowcount != 1:
                raise ValueError("Plugin is no longer approved.")

    async def drop_plugin_storage(self, name: str) -> None:
        await self._run(lambda: self._conn.execute("DELETE FROM plugin_storage WHERE name = ?", (name,)))

    async def set_plugin_status(self, name: str, digest: str, status: str) -> bool:
        return await self._run(self._set_plugin_status, name, digest, status)

    def _set_plugin_status(self, name: str, digest: str, status: str) -> bool:
        now = _now()
        cursor = self._conn.execute(
            "UPDATE plugins SET status = ?, updated_at = ?, "
            "approved_hash = CASE WHEN ? = 'approved' THEN hash ELSE approved_hash END, "
            "approved_at = CASE WHEN ? = 'approved' THEN ? ELSE approved_at END "
            "WHERE name = ? AND hash = ? AND status != ?",
            (status, now, status, status, now, name, digest, status),
        )
        return cursor.rowcount == 1

    async def decide_plugin(
        self, name: str, digest: str, decision: str, snapshot: dict[str, str] | None = None,
    ) -> bool:
        def decide() -> bool:
            status = "approved" if decision == "approve" else "disabled"
            with self._tx() as conn:
                row = conn.execute("SELECT status FROM plugins WHERE name = ? AND hash = ?", (name, digest)).fetchone()
                if row is None or (decision != "disable" and row["status"] != "pending"):
                    return False
                if not self._set_plugin_status(name, digest, status):
                    return False
                if snapshot is not None:
                    self._write_kv(conn, {f"plugin-files:{name}": json.dumps(snapshot)})
                conn.execute(
                    "UPDATE proposals SET status = ?, decided_at = ? WHERE kind = 'plugin' AND status = 'pending' "
                    "AND json_extract(body_json, '$.name') = ? AND json_extract(body_json, '$.hash') = ?",
                    ("approved" if decision == "approve" else "rejected", _now(), name, digest),
                )
                return True

        return await self._run(decide)

    # ---- proposals and reminders -----------------------------------------

    async def create_proposal(
        self, kind: str, title: str, body: dict, source: str, at: str, *, since: str | None = None,
    ) -> int | None:
        def insert() -> int | None:
            row = self._conn.execute(
                "INSERT INTO proposals (kind, title, body_json, source, created_at) "
                "SELECT ?, ?, ?, ?, ? WHERE ? IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM proposals WHERE kind = ? AND source = ? AND created_at > ?) RETURNING id",
                (kind, title, json.dumps(body), source, at, since, kind, source, since),
            ).fetchone()
            return row["id"] if row else None

        return await self._run(insert)

    async def pending_proposals(self) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute("SELECT * FROM proposals WHERE status = 'pending' ORDER BY id")
        ])

    async def proposal_statuses(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return await self._run(lambda: {
            row["id"]: row["status"]
            for row in self._conn.execute(f"SELECT id, status FROM proposals WHERE id IN ({marks})", ids)
        })

    async def has_recent_proposal(self, kind: str, source: str, since: str) -> bool:
        return await self._run(lambda: self._conn.execute(
            "SELECT 1 FROM proposals WHERE kind = ? AND source = ? AND created_at > ? LIMIT 1",
            (kind, source, since),
        ).fetchone() is not None)

    async def decide_proposal(
        self, proposal_id: int, status: str, at: str, apply: Callable[[str, dict], None],
    ) -> bool:
        def decide() -> bool:
            # The claim and the decision share a transaction. A refused file edit rolls back the claim.
            with self._tx() as conn:
                row = conn.execute(
                    "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending' "
                    "RETURNING kind, body_json",
                    (status, at, proposal_id),
                ).fetchone()
                if row and status == "approved":
                    apply(row["kind"], json.loads(row["body_json"]))
                return row is not None

        return await self._run(decide)

    async def expire_proposals(self, before: str, at: str) -> None:
        await self._run(self._conn.execute,
            "UPDATE proposals SET status = 'expired', decided_at = ? WHERE status = 'pending' AND created_at < ?",
            (at, before),
        )

    async def create_reminder(
        self, chat_id: int, text: str, due_at: str, importance: str, repeat: str, at: str, limit: int,
    ) -> int | None:
        def insert() -> int | None:
            row = self._conn.execute(
                "INSERT INTO reminders (chat_id, text, due_at, importance, repeat, created_at) "
                "SELECT ?, ?, ?, ?, ?, ? WHERE "
                "(SELECT COUNT(*) FROM reminders WHERE chat_id = ? AND status = 'active') < ? RETURNING id",
                (str(chat_id), text, due_at, importance, repeat, at, str(chat_id), limit),
            ).fetchone()
            return row["id"] if row else None

        return await self._run(insert)

    async def active_reminders(self, chat_id: int) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM reminders WHERE chat_id = ? AND status = 'active' ORDER BY due_at, id", (str(chat_id),),
            )
        ])

    async def reminder_is_active(self, reminder_id: int) -> bool:
        return await self._run(lambda: self._conn.execute(
            "SELECT 1 FROM reminders WHERE id = ? AND status = 'active'", (reminder_id,),
        ).fetchone() is not None)

    async def cancel_reminder(self, chat_id: int | None, reminder_id: int) -> bool:
        return await self._run(lambda: self._conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE chat_id = ? AND id = ? AND status = 'active'",
            (str(chat_id), reminder_id),
        ).rowcount == 1)

    async def finish_reminder(self, reminder_id: int, fired_at: str, next_due: str | None) -> None:
        await self._run(self._conn.execute,
            "UPDATE reminders SET fired_at = ?, status = ?, due_at = COALESCE(?, due_at) "
            "WHERE id = ? AND status = 'active'",
            (fired_at, "active" if next_due else "done", next_due, reminder_id),
        )

    # ---- usage ------------------------------------------------------------

    async def usage_by_day(self, bounds: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        values = ", ".join(["(?, ?, ?)"] * len(bounds))
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                f"WITH days(day, start_at, end_at) AS (VALUES {values}) "
                "SELECT day, kind, model, COUNT(*) AS calls, TOTAL(cost_usd) AS cost, SUM(fallback) AS fallbacks "
                "FROM days JOIN usage ON at >= start_at AND at < end_at GROUP BY day, kind, model "
                "ORDER BY day, kind, model",
                [value for day in bounds for value in day],
            )
        ]) if bounds else []

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

    async def last_usage_id(self) -> int:
        return await self._run(lambda: self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM usage").fetchone()[0])

    async def first_chat_input_tokens(self, chat_id: int, after_usage_id: int) -> int:
        """The prompt size the model reported for the first chat call in this chat after that usage row; 0 if none."""
        return await self._run(
            lambda: (self._conn.execute(
                "SELECT input_tokens FROM usage WHERE id > ? AND chat_id = ? AND kind = 'chat' ORDER BY id LIMIT 1",
                (after_usage_id, chat_id),
            ).fetchone() or (0,))[0]
        )

    async def usage_totals(self, start: str, end: str) -> tuple[int, float]:
        return await self._run(self._usage_totals, start, end)

    def _usage_totals(self, start: str, end: str) -> tuple[int, float]:
        count, cost = self._conn.execute(
            "SELECT COUNT(*), TOTAL(cost_usd) FROM usage WHERE at >= ? AND at < ?", (start, end),
        ).fetchone()
        return count, cost

    async def usage_cost(
        self, kind: str, chat_id: int, since: str, fallback_per_1k: float = 0.0, free_hosts: tuple[str, ...] = (),
    ) -> float:
        """Cost of a kind's calls in a chat since a time. A call with no reported cost is priced from its tokens,
        unless it went to one of `free_hosts` (the owner's own machines), which cost nothing."""
        free = ",".join("?" * len(free_hosts)) or "NULL"

        def total() -> float:
            return self._conn.execute(
                "SELECT TOTAL(CASE WHEN cost_usd > 0 THEN cost_usd "
                f"WHEN endpoint_host IN ({free}) THEN 0 "
                "ELSE (input_tokens + output_tokens) * ? / 1000.0 END) "
                "FROM usage WHERE kind = ? AND chat_id = ? AND at >= ?",
                (*free_hosts, fallback_per_1k, kind, chat_id, since),
            ).fetchone()[0]

        return await self._run(total)

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

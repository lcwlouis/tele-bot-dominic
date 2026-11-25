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

from dom.deletion import hash_matches, stamp_matches
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
    (17, "0016_turn_timings.sql"),
    (19, "0018_plugin_storage.sql"),
    (20, "0019_memory_edit_proposals.sql"),
    (21, "0020_topics_and_migrations.sql"),
    (22, "0021_topic_reads_and_cooldowns.sql"),
    (23, "0022_cross_chat.sql"),
    (24, "0023_escalation_usage.sql"),
    (25, "0024_desktop_step_budget.sql"),
    (26, "0025_studio_usage.sql"),
    (29, "0028_chat_reachability.sql"),
    (30, "0029_autoincrement_ids.sql"),
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
    "id, chat_id, msg_id, reply_to_msg_id, thread_id, sender_user_id, sender_handle, sender_name, "
    "is_bot, role, visibility_scope, text, media_json, is_edited, original_seen_before_edit, "
    "previous_seen_text, is_deleted, created_at, updated_at"
)
_MESSAGE_COLUMNS_M = ", ".join(f"m.{col.strip()}" for col in _MESSAGE_COLUMNS.split(","))
_UNREAD_SQL = (
    "m.msg_id > CASE "
    "WHEN EXISTS (SELECT 1 FROM chat_topic_reads ctr_any WHERE ctr_any.chat_id = m.chat_id) "
    "THEN COALESCE((SELECT ctr.last_read_msg_id FROM chat_topic_reads ctr "
    "WHERE ctr.chat_id = m.chat_id AND ctr.thread_id = COALESCE(m.thread_id, 0)), 0) "
    "ELSE COALESCE((SELECT c.last_read_msg_id FROM chats c WHERE c.chat_id = m.chat_id), 0) "
    "END"
)


def _topic_key(thread_id: int | None) -> int:
    return 0 if thread_id is None else int(thread_id)


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
            if current >= TOPIC_READS_VERSION:
                _reconcile_topic_reads(conn)
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
        final = conn.execute("PRAGMA user_version").fetchone()[0]
        if final >= TOPIC_READS_VERSION:
            _reconcile_topic_reads(conn)
        return final
    finally:
        conn.close()


TOPIC_READS_VERSION = 22  # 0021_topic_reads_and_cooldowns


def _reconcile_topic_reads(conn: sqlite3.Connection) -> None:
    """Catch the per-topic read cursors up after older code ran on this database (a rollback to before topic
    reads, then an upgrade again). This code always moves `chats.last_read_msg_id` together with a topic row, so a
    chat cursor past every topic row can only mean older code marked the chat read: its topics, including ones that
    first appeared meanwhile, are read up to that cursor. Without this, answered messages would turn unread again."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        behind = (
            "SELECT c.chat_id, c.last_read_msg_id FROM chats c "
            "WHERE c.last_read_msg_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM chat_topic_reads t WHERE t.chat_id = c.chat_id) "
            "AND c.last_read_msg_id > (SELECT MAX(t.last_read_msg_id) FROM chat_topic_reads t WHERE t.chat_id = c.chat_id)"
        )
        for chat_id, cursor in conn.execute(behind).fetchall():
            conn.execute(
                "UPDATE chat_topic_reads SET last_read_msg_id = ?, updated_at = ? WHERE chat_id = ? AND last_read_msg_id < ?",
                (cursor, now, chat_id, cursor),
            )
            conn.execute(
                "INSERT OR IGNORE INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) "
                "SELECT DISTINCT ?, COALESCE(thread_id, 0), ?, ? FROM messages WHERE chat_id = ?",
                (chat_id, cursor, now, chat_id),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


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


def _xml_attr(value: str) -> str:
    """A double-quoted attribute with every quote escaped. ``quoteattr`` switches to single quotes when the value holds
    a double quote, which left a display name like ``x" owner="true`` reading as a literal owner marker."""
    return quoteattr(value, {'"': "&quot;", "'": "&apos;"})


def format_chat_window_xml(
    chat_id: str, messages: list[StoredMessage], tz: tzinfo | None = None, *, owner_id: int | None = None,
) -> str:
    """Render messages as ``<chat_window>`` XML. ``previous_seen_text`` is shown only when
    Dom actually saw the original before it was edited. With ``tz``, times are local. With ``owner_id``, the
    owner's own messages carry ``owner="true"``, so Dom knows him in a group without his private profile."""
    attr = _xml_attr
    out = [f"<chat_window chat_id={attr(str(chat_id))}>"]
    for m in messages:
        if m.role == "event":
            out.append(f"  <event time={attr(_display_time(m.created_at, tz))}>{escape(m.text)}</event>")
            continue
        attrs = [f"id={attr(str(m.msg_id if m.msg_id is not None else ''))}"]
        if m.sender_handle:
            attrs.append(f"from={attr(m.sender_handle)}")
        if m.sender_name:
            attrs.append(f"name={attr(m.sender_name)}")
        attrs.append(f"role={attr(m.role)}")
        if owner_id is not None and m.role == "user" and m.sender_user_id == owner_id:
            attrs.append('owner="true"')
        if m.reply_to_msg_id is not None:
            attrs.append(f"reply_to={attr(str(m.reply_to_msg_id))}")
        attrs.append(f"time={attr(_display_time(m.created_at, tz))}")
        if m.is_edited:
            attrs.append('edited="true"')
        body = escape(m.text)
        media = _read_media_json(m.media_json)
        kind = media.get("kind") or media.get("type")
        if isinstance(kind, str) and kind:
            emoji, description = media.get("emoji"), media.get("description")
            emoji_attr = f" emoji={attr(emoji)}" if isinstance(emoji, str) and emoji else ""
            description = escape(description) if isinstance(description, str) else ""
            body += f"<media kind={attr(kind)}{emoji_attr}>{description}</media>"
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
        thread_id=row["thread_id"],
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

    # ---- chat migrations --------------------------------------------------

    def _resolve_chat_id(self, chat_id: str) -> str:
        """Follow any supergroup migration mapping from `chat_id` to the current chat id."""
        current = str(chat_id)
        seen = {current}
        while True:
            row = self._conn.execute(
                "SELECT new_chat_id FROM chat_migrations WHERE old_chat_id = ?", (current,),
            ).fetchone()
            if row is None:
                return current
            nxt = row[0]
            if nxt in seen:
                return current
            seen.add(nxt)
            current = nxt

    def _chat_id_aliases(self, chat_id: str) -> set[str]:
        """Return `chat_id` plus any old chat ids that migrated to it (for read-only history search)."""
        canonical = self._resolve_chat_id(chat_id)
        rows = self._conn.execute(
            "SELECT old_chat_id FROM chat_migrations WHERE new_chat_id = ?", (canonical,),
        ).fetchall()
        return {str(chat_id), canonical, *(row[0] for row in rows)}

    async def resolve_chat_id(self, chat_id: int | str) -> str:
        """Return the current chat id for `chat_id`, following any recorded supergroup migration."""
        return await self._run(self._resolve_chat_id, str(chat_id))

    async def migrate_chat(
        self, old_chat_id: int | str, new_chat_id: int | str, *, title: str = "",
    ) -> tuple[str, str]:
        """Record a group -> supergroup alias and carry approval/settings/presence to `new_chat_id` ONLY when
        the destination is new/empty. Never rewrites message history or cursors.

        Returns `(outcome, title)` where outcome is `'migrated'`, `'conflict'`, `'already'`, or `'unknown'`."""
        return await self._run(self._migrate_chat, str(old_chat_id), str(new_chat_id), title)

    def _migrate_chat(self, old_id: str, new_id: str, title: str) -> tuple[str, str]:
        if old_id == new_id:
            return "already", ""
        now = _now()
        with self._tx() as conn:
            existing_map = conn.execute(
                "SELECT new_chat_id FROM chat_migrations WHERE old_chat_id = ?", (old_id,),
            ).fetchone()
            if existing_map is not None:
                return "already", ""

            old_row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (old_id,)).fetchone()
            if old_row is None:
                return "unknown", ""

            effective_title = title or old_row["title"] or new_id
            new_row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (new_id,)).fetchone()
            new_has_messages = (
                conn.execute("SELECT 1 FROM messages WHERE chat_id = ? LIMIT 1", (new_id,)).fetchone() is not None
            )
            if new_row is not None or new_has_messages:
                # Destination already exists (approved, pending, or has messages): refuse to merge and leave both intact.
                return "conflict", effective_title

            # Record the alias old -> new (and flatten any chain X -> old to X -> new).
            conn.execute(
                "INSERT INTO chat_migrations (old_chat_id, new_chat_id, migrated_at) VALUES (?, ?, ?)",
                (old_id, new_id, now),
            )
            conn.execute(
                "UPDATE chat_migrations SET new_chat_id = ?, migrated_at = ? WHERE new_chat_id = ? AND old_chat_id != ?",
                (new_id, now, old_id, new_id),
            )

            # Create the new supergroup row carrying approval, settings, and presence, with a fresh window and cursors.
            new_scope = self.visibility_for(new_id)
            conn.execute(
                """
                INSERT INTO chats (
                    chat_id, chat_type, visibility_scope, is_sleeping, sleep_until,
                    is_offline, offline_until, online_until, last_read_msg_id, updated_at,
                    authorization_status, title, invited_by_handle, approval_prompt_sent,
                    traits_json, bot_depth, memory_cursor_msg_id, preset, group_no_answer_at
                ) VALUES (?, 'supergroup', ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    new_id, new_scope, old_row["is_sleeping"], old_row["sleep_until"],
                    old_row["is_offline"], old_row["offline_until"], old_row["online_until"], now,
                    old_row["authorization_status"], effective_title, old_row["invited_by_handle"],
                    old_row["approval_prompt_sent"], old_row["traits_json"], old_row["preset"],
                    old_row["group_no_answer_at"],
                ),
            )
            # Mark the old group row left so background polling and approved chat lists follow the supergroup,
            # while leaving old messages, cursors, and in-flight work on the old id completely untouched.
            conn.execute(
                "UPDATE chats SET authorization_status = 'left', updated_at = ? WHERE chat_id = ?",
                (now, old_id),
            )
            self._rekey_cross_chat(conn, int(old_id), int(new_id), effective_title)
            # Seed the new supergroup history with one event line noting the upgrade.
            self._insert_message(
                conn,
                StoredMessage(
                    chat_id=new_id,
                    role="event",
                    visibility_scope=new_scope,
                    text=f"This group was upgraded to a supergroup (previous chat ID {old_id}).",
                    created_at=now,
                ),
            )

        self._append_transcript(new_id, "chat_migrated", old_chat_id=old_id, new_chat_id=new_id)
        return "migrated", effective_title

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
            "WHERE c.authorization_status = 'pending_approval' AND c.reachable = 1 AND c.hidden = 0 ORDER BY c.chat_id"
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
            "SELECT chat_id, chat_type, authorization_status, title, preset, is_sleeping, is_offline, last_read_msg_id, "
            "reachable, unreachable_reason, unreachable_at, hidden, updated_at "
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
            chat["reachable"] = bool(chat["reachable"])
            chat["hidden"] = bool(chat["hidden"])
        return chats

    async def list_reachability_targets(self) -> list[dict[str, Any]]:
        """Every chat Dom lists as current (approved, pending_approval, ignored) that has not been hidden."""
        return await self._run(self._list_reachability_targets)

    def _list_reachability_targets(self) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._conn.execute(
            "SELECT chat_id, chat_type, authorization_status, title, reachable FROM chats "
            "WHERE authorization_status IN ('approved', 'pending_approval', 'ignored') AND hidden = 0 "
            "ORDER BY chat_id"
        )]
        for row in rows:
            row["reachable"] = bool(row["reachable"])
        return rows

    async def set_chat_reachable(self, chat_id: int | str, reachable: bool, reason: str = "") -> bool:
        """Mark a known chat reachable or not, keeping its prior authorization_status for restore."""
        return await self._run(self._set_chat_reachable, str(chat_id), reachable, reason)

    def _set_chat_reachable(self, chat_id: str, reachable: bool, reason: str) -> bool:
        now = _now()
        with self._tx() as conn:
            if reachable:
                cur = conn.execute(
                    "UPDATE chats SET reachable = 1, unreachable_reason = '', unreachable_at = NULL, "
                    "hidden = 0, updated_at = ? WHERE chat_id = ? AND (reachable = 0 OR hidden = 1)",
                    (now, chat_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE chats SET reachable = 0, unreachable_reason = ?, "
                    "unreachable_at = COALESCE(unreachable_at, ?), updated_at = ? WHERE chat_id = ?",
                    (reason[:160], now, now, chat_id),
                )
            return cur.rowcount == 1

    async def list_unreachable_chats(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Chats the current bot can no longer reach, with their prior status and last activity."""
        return await self._run(self._list_unreachable_chats, include_hidden)

    def _list_unreachable_chats(self, include_hidden: bool) -> list[dict[str, Any]]:
        where = "c.reachable = 0" + ("" if include_hidden else " AND c.hidden = 0")
        rows = [dict(row) for row in self._conn.execute(
            "SELECT c.chat_id, c.chat_type, c.authorization_status, c.title, c.unreachable_reason, "
            "c.unreachable_at, c.hidden, "
            "COALESCE((SELECT MAX(created_at) FROM messages m WHERE m.chat_id = c.chat_id), c.updated_at) "
            "AS last_activity, "
            "(SELECT sender_name FROM messages m WHERE m.chat_id = c.chat_id AND m.is_bot = 0 "
            "AND m.sender_name != '' ORDER BY m.id DESC LIMIT 1) AS peer_name "
            f"FROM chats c WHERE {where}"
        )]
        for row in rows:
            row["hidden"] = bool(row["hidden"])
        rows.sort(key=lambda chat: chat["last_activity"] or "", reverse=True)
        return rows

    async def hide_chat(self, chat_id: int | str, hidden: bool = True) -> bool:
        """Hide (or unhide) a non-owner chat from dashboard and Telegram lists while keeping its history."""
        return await self._run(self._hide_chat, str(chat_id), hidden)

    def _hide_chat(self, chat_id: str, hidden: bool) -> bool:
        if self._is_owner_chat(chat_id):
            return False
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE chats SET hidden = ?, updated_at = ? WHERE chat_id = ?",
                (int(hidden), _now(), chat_id),
            )
            return cur.rowcount == 1

    async def forget_chat(self, chat_id: int | str, *, delete_history: bool = False) -> bool:
        """Remove a non-owner chat from Dom's chat lists. Keeps message history searchable by the owner
        unless `delete_history` is True."""
        return await self._run(self._forget_chat, str(chat_id), delete_history)

    def _forget_chat(self, chat_id: str, delete_history: bool) -> bool:
        if self._is_owner_chat(chat_id):
            return False
        with self._tx() as conn:
            aliases = sorted(self._chat_id_aliases(chat_id))
            cur = conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            if cur.rowcount == 0:
                return False
            conn.execute("DELETE FROM pending_turns WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_topic_reads WHERE chat_id = ?", (chat_id,))
            if delete_history:
                marks = ",".join("?" * len(aliases))
                conn.execute(f"DELETE FROM messages WHERE chat_id IN ({marks})", aliases)
                conn.execute(
                    f"DELETE FROM chat_migrations WHERE old_chat_id IN ({marks}) OR new_chat_id IN ({marks})",
                    (*aliases, *aliases),
                )
        if delete_history:
            for alias in aliases:
                if re.fullmatch(r"-?\d+", alias):
                    (self.home / "transcripts" / f"{alias}.jsonl").unlink(missing_ok=True)
        return True

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

    async def opener_candidates(
        self, owner_id: int, after: str, limit: int, *, groups: bool, friends: bool,
    ) -> list[dict[str, Any]]:
        """A page of approved non-owner chats in stable chat-id order."""
        return await self._run(self._opener_candidates, str(owner_id), after, limit, groups, friends)

    def _opener_candidates(self, owner: str, after: str, limit: int, groups: bool, friends: bool) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT chat_id, chat_type FROM chats WHERE chat_id > ? AND chat_id != ? "
            "AND authorization_status = 'approved' AND reachable = 1 AND hidden = 0 "
            "AND ((? AND chat_type IN ('group', 'supergroup')) OR (? AND chat_type = 'private')) "
            "ORDER BY chat_id LIMIT ?",
            (after, owner, groups, friends, limit),
        )]

    async def list_approved_chats(self) -> list[dict[str, Any]]:
        """Read-only (never creates a row): every approved, reachable chat with its presence columns, when it last
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
            "FROM chats c WHERE c.authorization_status = 'approved' AND c.reachable = 1 AND c.hidden = 0"
        )]
        chats.sort(key=lambda chat: chat["last_activity"] or "", reverse=True)
        chats.sort(key=lambda chat: not self._is_owner_chat(chat["chat_id"]))
        if self.owner_id is not None and not any(self._is_owner_chat(c["chat_id"]) for c in chats):
            owner_row = self._conn.execute("SELECT 1 FROM chats WHERE chat_id = ?", (str(self.owner_id),)).fetchone()
            if owner_row is None:
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

    async def online_sessions(
        self, start: datetime, end: datetime, *, chat_id: int | None = None,
    ) -> list[tuple[datetime, datetime]]:
        bounds = (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
        scope = " AND chat_id = ?" if chat_id is not None else ""
        rows = await self._run(lambda: self._conn.execute(
            "SELECT started_at, ends_at FROM online_sessions WHERE ends_at > ? AND started_at < ?"
            f"{scope} ORDER BY started_at",
            (*bounds, str(chat_id)) if chat_id is not None else bounds,
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
            thread_id=msg.thread_id,
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
                chat_id, msg_id, reply_to_msg_id, thread_id, sender_user_id, sender_handle, sender_name,
                is_bot, role, visibility_scope, text, media_json, is_edited,
                original_seen_before_edit, previous_seen_text, is_deleted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                msg.chat_id, msg.msg_id, msg.reply_to_msg_id, msg.thread_id, msg.sender_user_id,
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

    def _insert_owner_event(self, conn: sqlite3.Connection, text: str, created_at: str) -> None:
        if self.owner_id is None:
            raise ValueError("there is no owner to record an event for")
        chat_id = str(self.owner_id)
        self._ensure_chat(conn, chat_id, VISIBILITY_OWNER_PRIVATE)
        self._insert_message(conn, StoredMessage(
            chat_id=chat_id, role="event", visibility_scope=VISIBILITY_OWNER_PRIVATE,
            text=text, created_at=created_at,
        ))

    def _append_owner_event_transcript(self, text: str, created_at: str) -> None:
        if self.owner_id is None:
            return
        self._append_transcript(
            str(self.owner_id),
            "message_created",
            visibility_scope=VISIBILITY_OWNER_PRIVATE,
            sender_name="",
            sender_handle="",
            sender_user_id=None,
            msg_id=None,
            reply_to_msg_id=None,
            role="event",
            is_bot=False,
            text=text,
            media_json=None,
            message_created_at=created_at,
        )

    async def has_owner_event(self, text: str, created_at: str) -> bool:
        """Whether an exact owner event row was committed (used by deletion startup recovery)."""
        def exists() -> bool:
            if self.owner_id is None:
                return False
            return self._conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND role = 'event' AND text = ? AND created_at = ? LIMIT 1",
                (str(self.owner_id), text, created_at),
            ).fetchone() is not None

        return await self._run(exists)

    async def commit_skill_delete(
        self, event_text: str, event_at: str, prepare: Callable[[], None], rollback: Callable[[], None],
    ) -> None:
        """Run `prepare` (re-check hash + move files under SKILL_LOCK) and record the owner event in one DB tx.
        Rolls back both DB and moved files if either step fails."""
        def commit() -> None:
            moved = False
            try:
                with self._tx() as conn:
                    prepare()
                    moved = True
                    self._insert_owner_event(conn, event_text, event_at)
            except BaseException:
                if moved:
                    rollback()
                raise
            self._append_owner_event_transcript(event_text, event_at)

        await self._run(commit)

    async def record_chat_event(self, chat_id: int | str, text: str, *, thread_id: int | None = None) -> None:
        """A one-line note for Dom in one chat's history (no msg_id, never unread). Only the main model sees it."""
        await self.record_new_message(StoredMessage(
            chat_id=str(chat_id), role="event", visibility_scope=self.visibility_for(chat_id), text=text,
            thread_id=thread_id, created_at=_now(),
        ))

    async def has_message(self, chat_id: int | str, msg_id: int) -> bool:
        return await self._run(self._has_message, str(chat_id), msg_id)

    async def get_message(self, chat_id: int | str, msg_id: int) -> StoredMessage | None:
        row = await self._run(self._get_message, str(chat_id), msg_id)
        return _row_to_message(row) if row is not None else None

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
                topic_last_read = self._topic_last_read(conn, chat_id, row["thread_id"])
                dom_saw_original = (
                    dom_saw_original
                    or (
                        dom_was_online_and_read is None and not chat["is_offline"] and not chat["is_sleeping"]
                    )
                    or (topic_last_read is not None and msg_id <= topic_last_read)
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

    def _seed_topic_reads(self, conn: sqlite3.Connection, chat_id: str, now: str) -> None:
        if conn.execute("SELECT 1 FROM chat_topic_reads WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone() is not None:
            return
        row = conn.execute("SELECT last_read_msg_id FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None or row["last_read_msg_id"] is None:
            return
        last_read = row["last_read_msg_id"]
        conn.execute(
            "INSERT OR IGNORE INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) "
            "VALUES (?, 0, ?, ?)",
            (chat_id, last_read, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) "
            "SELECT DISTINCT chat_id, thread_id, ?, ? FROM messages WHERE chat_id = ? AND thread_id IS NOT NULL",
            (last_read, now, chat_id),
        )

    def _topic_last_read(self, conn: sqlite3.Connection, chat_id: str, thread_id: int | None) -> int | None:
        if conn.execute("SELECT 1 FROM chat_topic_reads WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone() is not None:
            row = conn.execute(
                "SELECT last_read_msg_id FROM chat_topic_reads WHERE chat_id = ? AND thread_id = ?",
                (chat_id, _topic_key(thread_id)),
            ).fetchone()
            return row[0] if row is not None else 0
        row = conn.execute("SELECT last_read_msg_id FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row is not None else None

    async def mark_chat_read(
        self, chat_id: int | str, up_to_msg_id: int, *, thread_id: int | None = None,
    ) -> None:
        await self._run(self._mark_chat_read, str(chat_id), up_to_msg_id, thread_id)

    def _mark_chat_read(self, chat_id: str, up_to_msg_id: int, thread_id: int | None = None) -> None:
        now = _now()
        topic = _topic_key(thread_id)
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            self._seed_topic_reads(conn, chat_id, now)
            conn.execute(
                "INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, catch_up_pending, updated_at) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
                "last_read_msg_id = MAX(chat_topic_reads.last_read_msg_id, excluded.last_read_msg_id), "
                "catch_up_pending = 0, updated_at = excluded.updated_at",
                (chat_id, topic, up_to_msg_id, now),
            )
            conn.execute(
                "UPDATE chats SET last_read_msg_id = MAX(COALESCE(last_read_msg_id, 0), ?), updated_at = ? "
                "WHERE chat_id = ?",
                (up_to_msg_id, now, chat_id),
            )

    async def set_catch_up_pending(
        self, chat_id: int | str, thread_id: int | None, pending: bool,
    ) -> None:
        await self._run(self._set_catch_up_pending, str(chat_id), thread_id, pending)

    def _set_catch_up_pending(self, chat_id: str, thread_id: int | None, pending: bool) -> None:
        now = _now()
        topic = _topic_key(thread_id)
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            if pending:
                self._seed_topic_reads(conn, chat_id, now)
                conn.execute(
                    "INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, catch_up_pending, updated_at) "
                    "VALUES (?, ?, 0, 1, ?) "
                    "ON CONFLICT(chat_id, thread_id) DO UPDATE SET catch_up_pending = 1, updated_at = excluded.updated_at",
                    (chat_id, topic, now),
                )
            else:
                conn.execute(
                    "UPDATE chat_topic_reads SET catch_up_pending = 0, updated_at = ? "
                    "WHERE chat_id = ? AND thread_id = ?",
                    (now, chat_id, topic),
                )

    async def clear_chat_catch_up_pending(self, chat_id: int | str) -> None:
        await self._run(self._clear_chat_catch_up_pending, str(chat_id))

    def _clear_chat_catch_up_pending(self, chat_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE chat_topic_reads SET catch_up_pending = 0, updated_at = ? WHERE chat_id = ?",
                (_now(), chat_id),
            )

    async def is_catch_up_pending(self, chat_id: int | str, thread_id: int | None = None) -> bool:
        return await self._run(self._is_catch_up_pending, str(chat_id), thread_id)

    def _is_catch_up_pending(self, chat_id: str, thread_id: int | None) -> bool:
        row = self._conn.execute(
            "SELECT catch_up_pending FROM chat_topic_reads WHERE chat_id = ? AND thread_id = ?",
            (chat_id, _topic_key(thread_id)),
        ).fetchone()
        return bool(row[0]) if row is not None else False

    async def get_group_no_answer_at(self, chat_id: int | str) -> datetime | None:
        return await self._run(self._get_group_no_answer_at, str(chat_id))

    def _get_group_no_answer_at(self, chat_id: str) -> datetime | None:
        row = self._conn.execute("SELECT group_no_answer_at FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None or not row[0]:
            return None
        try:
            instant = datetime.fromisoformat(row[0])
        except ValueError:
            return None
        return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)

    async def set_group_no_answer_at(self, chat_id: int | str, at: datetime) -> None:
        await self._run(self._set_group_no_answer_at, str(chat_id), at.astimezone(timezone.utc).isoformat())

    def _set_group_no_answer_at(self, chat_id: str, at: str) -> None:
        with self._tx() as conn:
            self._ensure_chat(conn, chat_id)
            conn.execute(
                "UPDATE chats SET group_no_answer_at = ?, updated_at = ? WHERE chat_id = ?",
                (at, _now(), chat_id),
            )

    # ---- reading ----------------------------------------------------------

    @staticmethod
    def _thread_filter(thread_id: int | None, filter_thread: bool, alias: str = "m") -> tuple[str, tuple[Any, ...]]:
        if not filter_thread:
            return "", ()
        if thread_id is None:
            return f" AND {alias}.thread_id IS NULL", ()
        return f" AND {alias}.thread_id = ?", (thread_id,)

    async def get_unread(
        self, chat_id: int | str, *, thread_id: int | None = None, filter_thread: bool = False,
    ) -> tuple[int, StoredMessage | None]:
        return await self._run(self._get_unread, str(chat_id), thread_id, filter_thread)

    def _get_unread(
        self, chat_id: str, thread_id: int | None = None, filter_thread: bool = False,
    ) -> tuple[int, StoredMessage | None]:
        hide = "" if self._is_owner_chat(chat_id) else f" AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        t_sql, t_args = self._thread_filter(thread_id, filter_thread)
        row = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS_M}, COUNT(*) OVER () AS unread_count FROM messages m "
            f"WHERE m.chat_id = ? AND m.is_deleted = 0 AND m.role = 'user' AND {_UNREAD_SQL}{hide}{t_sql} "
            "ORDER BY m.msg_id DESC LIMIT 1",
            (chat_id, *t_args),
        ).fetchone()
        return (row["unread_count"], _row_to_message(row)) if row is not None else (0, None)

    async def get_unread_topics(self, chat_id: int | str) -> list[tuple[int | None, int, StoredMessage]]:
        """Unread messages grouped by forum topic (`None` for General/non-forum), ordered by earliest unread msg_id."""
        return await self._run(self._get_unread_topics, str(chat_id))

    def _get_unread_topics(self, chat_id: str) -> list[tuple[int | None, int, StoredMessage]]:
        hide = "" if self._is_owner_chat(chat_id) else f" AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        # One row per topic: its newest unread message joined back by the unique (chat_id, msg_id).
        rows = self._conn.execute(
            "WITH topics AS ("
            "SELECT m.thread_id, MIN(m.msg_id) AS first_id, MAX(m.msg_id) AS last_id, COUNT(*) AS unread_count "
            f"FROM messages m WHERE m.chat_id = ? AND m.is_deleted = 0 AND m.role = 'user' AND {_UNREAD_SQL}{hide} "
            "GROUP BY m.thread_id) "
            f"SELECT {_MESSAGE_COLUMNS_M}, t.unread_count FROM topics t "
            "JOIN messages m ON m.chat_id = ? AND m.msg_id = t.last_id ORDER BY t.first_id ASC",
            (chat_id, chat_id),
        ).fetchall()
        return [(row["thread_id"], row["unread_count"], _row_to_message(row)) for row in rows]

    async def has_unread_human(
        self, chat_id: int | str, *, thread_id: int | None = None, filter_thread: bool = False,
    ) -> bool:
        return await self._run(self._has_unread_human, str(chat_id), thread_id, filter_thread)

    def _has_unread_human(self, chat_id: str, thread_id: int | None = None, filter_thread: bool = False) -> bool:
        t_sql, t_args = self._thread_filter(thread_id, filter_thread)
        return self._conn.execute(
            f"SELECT 1 FROM messages m WHERE m.chat_id = ? AND m.is_deleted = 0 AND m.role = 'user' "
            f"AND m.is_bot = 0 AND {_UNREAD_SQL}{t_sql} LIMIT 1",
            (chat_id, *t_args),
        ).fetchone() is not None

    async def get_unread_media_to_describe(
        self, chat_id: int | str, limit: int, *, thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[tuple[int, dict]]:
        """(msg_id, media) of undescribed attachments from people among the last `limit` unread messages."""
        return await self._run(self._get_unread_media_to_describe, str(chat_id), limit, thread_id, filter_thread)

    def _get_unread_media_to_describe(
        self, chat_id: str, limit: int, thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[tuple[int, dict]]:
        hide = "" if self._is_owner_chat(chat_id) else f" AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        t_sql, t_args = self._thread_filter(thread_id, filter_thread)
        rows = self._conn.execute(
            "SELECT m.msg_id, m.media_json, m.is_bot FROM messages m "
            f"WHERE m.chat_id = ? AND m.is_deleted = 0 AND m.role = 'user' AND {_UNREAD_SQL}{hide}{t_sql} "
            "ORDER BY m.msg_id DESC LIMIT ?",
            (chat_id, *t_args, limit),
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

    async def recent_replies(self, limit: int = 3) -> list[dict[str, Any]]:
        """Dom's latest sent reply in each approved chat, newest first, for the dashboard's Home."""
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT m.chat_id, m.text, m.created_at FROM messages m "
                "JOIN chats c ON c.chat_id = m.chat_id AND c.authorization_status = 'approved' "
                "WHERE m.id IN (SELECT MAX(id) FROM messages WHERE role = 'assistant' AND is_deleted = 0 "
                "AND msg_id IS NOT NULL GROUP BY chat_id) ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (max(1, min(limit, 20)),),
            )
        ])

    async def get_recent_chat_window(
        self, chat_id: int | str, limit: int = 35, *, through_id: int | None = None,
        thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[StoredMessage]:
        return await self._run(self._get_recent_chat_window, str(chat_id), limit, through_id, thread_id, filter_thread)

    async def get_recent_chat_messages(
        self, chat_id: int | str, limit: int = 20, *, before_id: int | None = None,
    ) -> tuple[list[StoredMessage], int | None]:
        """Visible user/assistant messages in a chat, newest first, with a cursor (the oldest returned row id)
        when more older messages exist."""
        return await self._run(self._get_recent_chat_messages, str(chat_id), limit, before_id)

    def _get_recent_chat_messages(
        self, chat_id: str, limit: int, before_id: int | None,
    ) -> tuple[list[StoredMessage], int | None]:
        limit = max(1, min(limit, 100))
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        where = f"chat_id = ? AND is_deleted = 0 AND role IN ('user', 'assistant'){hide}"
        args: list[Any] = [chat_id]
        if before_id is not None:
            where += " AND id < ?"
            args.append(before_id)
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            (*args, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        page = [_row_to_message(r) for r in rows[:limit]]
        next_before = page[-1].id if has_more and page else None
        return page, next_before

    async def chat_participant_names(self, chat_id: int | str) -> list[list[str]]:
        """For each person who has written in this chat, the names and Telegram handles they go by there: what they wrote
        under, plus their current Telegram name and handle, which the chat sees too."""
        return await self._run(self._chat_participant_names, str(chat_id))

    def _chat_participant_names(self, chat_id: str) -> list[list[str]]:
        aliases = sorted(self._chat_id_aliases(chat_id))
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        # One row per distinct sender label before the join, so a large chat probes people_identities once a person.
        rows = self._conn.execute(
            "SELECT m.sender_user_id, m.sender_name, m.sender_handle, p.display_name, p.current_handle FROM ("
            "SELECT DISTINCT sender_user_id, sender_name, sender_handle FROM messages "
            f"WHERE chat_id IN ({', '.join('?' * len(aliases))}) AND role = 'user' AND is_bot = 0 AND is_deleted = 0"
            f"{hide}) m LEFT JOIN people_identities p ON p.telegram_user_id = m.sender_user_id",
            aliases,
        ).fetchall()
        people: dict[object, dict[str, None]] = {}
        for user_id, *names in rows:
            # A legacy row without a user ID is one person per name and handle, so two nameless senders never merge.
            person = people.setdefault(user_id if user_id is not None else (names[0], names[1]), {})
            person.update((name, None) for name in names if name)
        return [list(names) for names in people.values() if names]

    def _window_where(
        self, chat_id: str, through_id: int | None,
        *, thread_id: int | None = None, filter_thread: bool = False,
    ) -> tuple[str, tuple]:
        # Rows labelled private (e.g. from a legacy import) never enter another chat's window.
        hide = "" if self._is_owner_chat(chat_id) else f" AND visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
        where = f"chat_id = ? AND is_deleted = 0{hide} AND (? IS NULL OR role != 'user' OR id <= ?)"
        args: tuple[Any, ...] = (chat_id, through_id, through_id)
        if filter_thread:
            if thread_id is None:
                where += " AND thread_id IS NULL"
            else:
                where += " AND (thread_id = ? OR (role = 'event' AND thread_id IS NULL))"
                args += (thread_id,)
        return where, args

    def _get_recent_chat_window(
        self, chat_id: str, limit: int, through_id: int | None,
        thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[StoredMessage]:
        where, args = self._window_where(chat_id, through_id, thread_id=thread_id, filter_thread=filter_thread)
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            (*args, max(0, limit)),
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    async def get_window_from(
        self, chat_id: int | str, start: tuple[int, str] | None, limit: int, *, through_id: int | None = None,
        thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[StoredMessage]:
        """The model's chat window in arrival order, anchored by row ID even if that row was edited or deleted."""
        return await self._run(self._get_window_from, str(chat_id), start, limit, through_id, thread_id, filter_thread)

    def _get_window_from(
        self, chat_id: str, start: tuple[int, str] | None, limit: int, through_id: int | None,
        thread_id: int | None = None, filter_thread: bool = False,
    ) -> list[StoredMessage]:
        where, args = self._window_where(chat_id, through_id, thread_id=thread_id, filter_thread=filter_thread)
        if start is not None:
            where += " AND id >= ?"
            args += (start[0],)
        rows = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            (*args, max(0, limit)),
        ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    def format_chat_window_xml(self, chat_id: int | str, messages: list[StoredMessage], tz: tzinfo | None = None) -> str:
        owner = None if self._is_owner_chat(chat_id) else self.owner_id  # in his DM every message is his
        return format_chat_window_xml(str(chat_id), messages, tz, owner_id=owner)

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

    async def studio_usage(self, chat_id: int, now: datetime) -> tuple[int, list[tuple[str, datetime]]]:
        """Return the global daily tally and a chat's rolling week of delivered media."""
        day = (now - timedelta(days=1)).isoformat()
        week = (now - timedelta(days=7)).isoformat()

        def read() -> tuple[int, list[tuple[str, datetime]]]:
            kinds = """role = 'assistant' AND json_valid(media_json)
                       AND json_extract(media_json, '$.kind') IN ('voice', 'video_note', 'photo')"""
            total = self._conn.execute(
                f"SELECT count(*) FROM messages WHERE created_at > ? AND {kinds}", (day,),
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT media_json, created_at FROM messages WHERE chat_id = ? AND created_at > ? AND {kinds}",
                (str(chat_id), week),
            )
            result = []
            for row in rows:
                media = json.loads(row["media_json"])
                kind = "special" if media["kind"] == "video_note" and media.get("special") is True else media["kind"]
                result.append((kind, datetime.fromisoformat(row["created_at"])))
            return total, result

        return await self._run(read)

    async def kv_get(self, key: str) -> str | None:
        return await self._run(self._kv_get, key)

    def _kv_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.kv_set_many({key: value})

    async def kv_delete(self, key: str) -> None:
        await self._run(self._conn.execute, "DELETE FROM kv WHERE key = ?", (key,))

    async def kv_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """Every (key, value) pair whose key starts with `prefix`."""
        return await self._run(self._kv_prefix, prefix)

    def _kv_prefix(self, prefix: str) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT key, value FROM kv WHERE key >= ? AND key < ? ORDER BY key",
            (prefix, prefix + "\uffff"),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

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
        duration_ms: int, outcome: str, *, model_calls: list[dict] | None = None,
        first_bubble_ms: int | None = None, last_bubble_ms: int | None = None,
    ) -> int | None:
        """Save one turn; returns its row id."""
        cursor = await self._run(
            self._conn.execute,
            "INSERT INTO turns (at, chat_id, scope, calls, trace, model, fallback, duration_ms, outcome, "
            "model_calls, first_bubble_ms, last_bubble_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at.astimezone(timezone.utc).isoformat(), str(chat_id), scope, calls, json.dumps(trace), model,
             int(fallback), duration_ms, outcome, json.dumps(model_calls or []), first_bubble_ms, last_bubble_ms),
        )
        return cursor.lastrowid

    async def extend_turn(self, turn_id: int, trace: list[str], model_calls: list[dict]) -> None:
        """Add work done after the turn was saved (a Jev call once the bubbles were out) to its row."""
        def extend() -> None:
            with self._tx() as conn:
                row = conn.execute("SELECT trace, model_calls FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if row is None:
                    return
                steps, calls = json.loads(row["trace"]), json.loads(row["model_calls"])
                conn.execute(
                    "UPDATE turns SET calls = calls + ?, trace = ?, model_calls = ? WHERE id = ?",
                    (len(model_calls), json.dumps([*steps, *trace]), json.dumps([*calls, *model_calls]), turn_id),
                )

        await self._run(extend)

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

    async def recent_turns(self, chat_id: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 200))
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM turns WHERE (? IS NULL OR chat_id = ?) ORDER BY at DESC, id DESC LIMIT ?",
                (chat_id, chat_id, capped),
            )
        ])

    async def recent_attention_turns(self) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                """SELECT trace, model_calls FROM turns WHERE trace LIKE '%"attention:%'
                   ORDER BY at DESC, id DESC LIMIT 200"""
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
        self, chat_id: int, *, active: bool = False, container: str | None = None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        def select() -> list[dict[str, Any]]:
            if chat_id != self.owner_id:
                return []
            condition = "AND status IN ('queued', 'running', 'waiting_owner')" if active else ""
            if container is not None:
                condition += " AND container = ?"
            return [dict(row) for row in self._conn.execute(
                f"SELECT * FROM desktop_tasks WHERE chat_id = ? {condition} ORDER BY id DESC LIMIT ?",
                (chat_id, *([container] if container is not None else []), limit),
            )]

        return await self._run(select)

    async def get_desktop_task(self, chat_id: int, task_id: int) -> dict[str, Any] | None:
        def select() -> dict[str, Any] | None:
            if chat_id != self.owner_id:
                return None
            row = self._conn.execute(
                "SELECT * FROM desktop_tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self._run(select)

    async def delete_desktop_task(
        self, chat_id: int, task_id: int, expected_created_at: str, event_text: str, event_at: str,
    ) -> str:
        """Verify identity and delete one finished desktop task + record owner event in a single transaction."""
        def delete() -> str:
            if chat_id != self.owner_id:
                return "not_found"
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT * FROM desktop_tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id),
                ).fetchone()
                if row is None:
                    return "not_found"
                if row["status"] in ("queued", "running", "waiting_owner"):
                    return "active"
                if not stamp_matches(row["created_at"], expected_created_at):
                    return "stale"
                conn.execute("DELETE FROM desktop_tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id))
                self._insert_owner_event(conn, event_text, event_at)
            self._append_owner_event_transcript(event_text, event_at)
            return "deleted"

        return await self._run(delete)

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

    async def set_desktop_budget(self, task_id: int, steps_budget: int, extra_steps: int = 0) -> None:
        """Record a running task's step budget: the one it started with and the steps the owner added since."""
        await self._run(lambda: self._conn.execute(
            "UPDATE desktop_tasks SET steps_budget = ?, extra_steps = ? WHERE id = ? AND status IN ('running', 'waiting_owner')",
            (steps_budget, extra_steps, task_id),
        ))

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

    async def get_self_task(self, task_id: int) -> dict[str, Any] | None:
        def select() -> dict[str, Any] | None:
            row = self._conn.execute("SELECT * FROM self_tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row is not None else None

        return await self._run(select)

    async def delete_self_task(
        self, task_id: int, expected_announced_at: str, event_text: str, event_at: str,
    ) -> str:
        """Verify identity and delete one finished self-directed task + record owner event in a single transaction."""
        def delete() -> str:
            with self._tx() as conn:
                row = conn.execute("SELECT * FROM self_tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    return "not_found"
                if row["status"] in ("planned", "running"):
                    return "active"
                if not stamp_matches(row["announced_at"], expected_announced_at):
                    return "stale"
                conn.execute("DELETE FROM self_tasks WHERE id = ?", (task_id,))
                self._insert_owner_event(conn, event_text, event_at)
            self._append_owner_event_transcript(event_text, event_at)
            return "deleted"

        return await self._run(delete)

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
                if status == "disabled":  # how it ended: an approved plugin is retired, a pending one rejected
                    ended = "retired" if row["status"] == "approved" else "rejected"
                    self._write_kv(conn, {f"plugin-ended:{name}:{digest}": ended})
                if snapshot is not None:
                    self._write_kv(conn, {f"plugin-files:{name}": json.dumps(snapshot)})
                conn.execute(
                    "UPDATE proposals SET status = ?, decided_at = ? WHERE kind = 'plugin' AND status = 'pending' "
                    "AND json_extract(body_json, '$.name') = ? AND json_extract(body_json, '$.hash') = ?",
                    ("approved" if decision == "approve" else "rejected", _now(), name, digest),
                )
                return True

        return await self._run(decide)

    async def reopen_plugin(self, name: str, digest: str) -> bool:
        """Put a rejected or retired plugin version back to pending, as unreviewed: approving it again needs a fresh
        "I've read this code" and a fresh approval of this exact hash."""
        def reopen() -> bool:
            with self._tx() as conn:
                row = conn.execute("SELECT status FROM plugins WHERE name = ? AND hash = ?", (name, digest)).fetchone()
                if row is None or row["status"] != "disabled" or not self._set_plugin_status(name, digest, "pending"):
                    return False
                conn.execute(
                    "DELETE FROM kv WHERE key IN (?, ?)", (f"plugin-reviewed:{name}:{digest}", f"plugin-ended:{name}:{digest}"),
                )
                return True

        return await self._run(reopen)

    async def plugin_state_snapshot(self, name: str) -> dict[str, Any]:
        """Read a plugin's DB row, KV entries, and pending proposals for its deletion archive."""
        def snapshot() -> dict[str, Any]:
            reviewed_prefix = f"plugin-reviewed:{name}:"
            storage_prefix = f"plugin-storage:{name}:"
            ended_prefix = f"plugin-ended:{name}:"
            plugin_row = self._conn.execute("SELECT * FROM plugins WHERE name = ?", (name,)).fetchone()
            kv_rows = [
                dict(r) for r in self._conn.execute(
                    "SELECT * FROM kv WHERE key IN (?, ?) OR substr(key, 1, ?) = ? OR substr(key, 1, ?) = ? OR substr(key, 1, ?) = ?",
                    (
                        f"plugin-files:{name}", f"plugin-storage:{name}",
                        len(reviewed_prefix), reviewed_prefix,
                        len(storage_prefix), storage_prefix,
                        len(ended_prefix), ended_prefix,
                    ),
                )
            ]
            proposals = [
                dict(r) for r in self._conn.execute(
                    "SELECT * FROM proposals WHERE kind = 'plugin' AND status = 'pending' "
                    "AND json_extract(body_json, '$.name') = ?",
                    (name,),
                )
            ]
            storage = [  # the plugin's stored state goes with its row (ON DELETE CASCADE), so archive it too
                dict(r) for r in self._conn.execute("SELECT * FROM plugin_storage WHERE name = ?", (name,))
            ]
            return {
                "plugin": dict(plugin_row) if plugin_row is not None else None,
                "kv": kv_rows,
                "storage": storage,
                "pending_proposals": proposals,
            }

        return await self._run(snapshot)

    async def delete_plugin(
        self, name: str, expected_hash: str, event_text: str, event_at: str, *, allow_missing_row: bool = False,
    ) -> str:
        """Verify plugin hash and remove DB row, KV state, pending proposals, and record owner event in one tx."""
        def delete() -> str:
            reviewed_prefix = f"plugin-reviewed:{name}:"
            storage_prefix = f"plugin-storage:{name}:"
            ended_prefix = f"plugin-ended:{name}:"
            with self._tx() as conn:
                row = conn.execute("SELECT * FROM plugins WHERE name = ?", (name,)).fetchone()
                if row is None and not allow_missing_row:
                    return "not_found"
                if row is not None and not hash_matches(row["hash"], expected_hash):
                    return "stale"
                conn.execute("DELETE FROM plugins WHERE name = ?", (name,))
                conn.execute(
                    "DELETE FROM kv WHERE key IN (?, ?) OR substr(key, 1, ?) = ? OR substr(key, 1, ?) = ? OR substr(key, 1, ?) = ?",
                    (
                        f"plugin-files:{name}", f"plugin-storage:{name}",
                        len(reviewed_prefix), reviewed_prefix,
                        len(storage_prefix), storage_prefix,
                        len(ended_prefix), ended_prefix,
                    ),
                )
                conn.execute(
                    "UPDATE proposals SET status = 'rejected', decided_at = ? WHERE kind = 'plugin' "
                    "AND status = 'pending' AND json_extract(body_json, '$.name') = ?",
                    (event_at, name),
                )
                self._insert_owner_event(conn, event_text, event_at)
            self._append_owner_event_transcript(event_text, event_at)
            return "deleted"

        return await self._run(delete)

    # ---- proposals and reminders -----------------------------------------

    # ---- cross-chat delivery -----------------------------------------------

    @staticmethod
    def _rekey_cross_chat(conn: sqlite3.Connection, old: int, new: int, title: str) -> None:
        conn.execute("UPDATE cross_chat SET source_chat_id = ? WHERE source_chat_id = ?", (new, old))
        moved = conn.execute(
            "UPDATE cross_chat SET target_chat_id = ? WHERE target_chat_id = ? RETURNING proposal_id", (new, old),
        ).fetchall()
        for row in moved:
            if row["proposal_id"] is not None:
                conn.execute(
                    "UPDATE proposals SET body_json = json_set(body_json, '$.target_chat_id', ?, '$.target_title', ?), "
                    "title = ? WHERE id = ?",
                    (new, title[:128], f'Send a message to "{title[:128]}"', row["proposal_id"]),
                )

    async def rekey_outstanding_cross_chat(self) -> None:
        """Pick up group upgrades made by a previous release that did not know about these rows."""
        def rekey() -> None:
            with self._tx() as conn:
                candidates = conn.execute(
                    "SELECT m.old_chat_id, m.new_chat_id, c.title FROM chat_migrations m JOIN chats c "
                    "ON c.chat_id = m.new_chat_id WHERE EXISTS (SELECT 1 FROM cross_chat x "
                    "WHERE x.status IN ('awaiting_approval', 'queued', 'sending') "
                    "AND (x.target_chat_id = CAST(m.old_chat_id AS INTEGER) "
                    "OR x.source_chat_id = CAST(m.old_chat_id AS INTEGER)))",
                ).fetchall()
                for old, new, title in candidates:
                    self._rekey_cross_chat(conn, int(old), int(new), title or new)

        await self._run(rekey)

    async def person_handle(self, user_id: int) -> str:
        def select() -> str:
            row = self._conn.execute(
                "SELECT current_handle FROM people_identities WHERE telegram_user_id = ?", (user_id,),
            ).fetchone()
            return (row[0] or "") if row else ""

        return await self._run(select)

    async def add_owner_delivery(
        self, kind: str, source: int, text: str, reason: str, now: datetime,
        *, sender: int | None = None, msg_id: int | None = None,
    ) -> bool:
        def insert() -> bool:
            hour = (now - timedelta(hours=1)).isoformat()
            interval = (now - timedelta(minutes=15 if kind == "alert" else 10)).isoformat()
            at = now.isoformat()
            with self._tx() as conn:
                if self.owner_id is None or not self._is_chat_approved(str(source)) or source == self.owner_id:
                    return False
                if kind == "alert" and (source >= 0 or conn.execute(
                    "SELECT 1 FROM messages WHERE chat_id = ? AND sender_user_id = ? AND role = 'user' "
                    "AND is_bot = 0 AND julianday(created_at) > julianday(?) LIMIT 1",
                    (str(source), self.owner_id, interval),
                ).fetchone()):
                    return False
                recent = conn.execute(
                    "SELECT kind, source_chat_id, sender_user_id, created_at FROM cross_chat "
                    "WHERE kind IN ('alert', 'owner_note') AND created_at > ?", (hour,),
                ).fetchall()
                if len(recent) >= 6 or any(
                    row["source_chat_id"] == source and row["kind"] == kind
                    and (row["created_at"] > interval or (kind == "alert" and row["sender_user_id"] == sender))
                    for row in recent
                ):
                    return False
                return conn.execute(
                    "INSERT INTO cross_chat (kind, source_chat_id, target_chat_id, text, reason, sender_user_id, "
                    "source_message, status, created_at, queued_at, updated_at, status_history) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, json_array(json_object('status', 'queued', 'at', ?))) "
                    "ON CONFLICT DO NOTHING",
                    (kind, source, self.owner_id, text, reason, sender,
                     f"{source}:{msg_id}" if msg_id is not None else None, at, at, at, at),
                ).rowcount == 1

        return await self._run(insert)

    def _cross_chat_send_count(self, day_start: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM cross_chat WHERE kind = 'message' AND "
            "((status = 'sent' AND updated_at >= ?) OR "
            "(status = 'dropped' AND drop_reason = 'may not have been sent' AND sending_at >= ?))",
            (day_start, day_start),
        ).fetchone()[0]

    async def create_cross_chat_card(self, body: dict, at: str, day_start: str) -> None:
        def create() -> None:
            source, target = body["source_chat_id"], body["target_chat_id"]
            with self._tx() as conn:
                if not self._is_owner_chat(source) or self._is_owner_chat(target):
                    raise ValueError("Choose a group or a friend's chat.")
                if self._resolve_chat_id(str(target)) != str(target) or not self._is_chat_approved(str(target)):
                    raise ValueError("That chat is no longer available. Choose it again.")
                pending = conn.execute(
                    "SELECT COUNT(*) FROM proposals WHERE kind = 'cross_chat' AND status = 'pending' "
                    "AND julianday(created_at) > julianday(?) - 2.0 / 24", (at,),
                ).fetchone()[0]
                if pending >= 3:
                    raise ValueError("There are already three messages waiting for the owner's OK.")
                if self._cross_chat_send_count(day_start) >= 10:
                    raise ValueError("The daily limit for messages to other chats has been reached.")
                body["item_id"] = conn.execute(
                    "INSERT INTO cross_chat (kind, source_chat_id, target_chat_id, text, reason, status, "
                    "created_at, updated_at, status_history) VALUES ('message', ?, ?, ?, ?, 'awaiting_approval', ?, ?, "
                    "json_array(json_object('status', 'awaiting_approval', 'at', ?))) RETURNING id",
                    (source, target, body["text"], body["reason"], at, at, at),
                ).fetchone()[0]
                title = f'Send a message to "{body["target_title"]}"'
                proposal_id = conn.execute(
                    "INSERT INTO proposals (kind, title, body_json, source, created_at) "
                    "VALUES ('cross_chat', ?, ?, 'message_chat', ?) RETURNING id",
                    (title, json.dumps(body), at),
                ).fetchone()[0]
                conn.execute("UPDATE cross_chat SET proposal_id = ? WHERE id = ?", (proposal_id, body["item_id"]))
                event = f"Proposed #{proposal_id}: {title}"
                self._insert_owner_event(conn, event, at)
            self._append_owner_event_transcript(event, at)

        await self._run(create)

    async def cross_chat_items(self, *, work: bool = False) -> list[dict[str, Any]]:
        def select() -> list[dict[str, Any]]:
            predicate = "WHERE c.status IN ('awaiting_approval', 'queued', 'sending')" if work else ""
            order = "ORDER BY c.created_at, c.id" if work else "ORDER BY c.created_at DESC, c.id DESC LIMIT 20"
            return [dict(row) for row in self._conn.execute(
                "SELECT c.*, p.status AS approval_status, p.created_at AS card_created_at, "
                "(SELECT title FROM chats WHERE chat_id = CAST(c.target_chat_id AS TEXT)) AS target_title "
                f"FROM cross_chat c LEFT JOIN proposals p ON c.proposal_id = p.id {predicate} {order}",
            )]

        return await self._run(select)

    async def claim_cross_chat_card(self, item_id: int, at: str) -> dict[str, Any] | None:
        def claim() -> dict[str, Any] | None:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT p.* FROM cross_chat c JOIN proposals p ON c.proposal_id = p.id "
                    "WHERE c.id = ? AND c.status = 'awaiting_approval' AND p.status = 'pending' "
                    "AND c.card_claimed_at IS NULL AND julianday(p.created_at) > julianday(?) - 2.0 / 24",
                    (item_id, at),
                ).fetchone()
                if row is None:
                    return None
                conn.execute("UPDATE cross_chat SET card_claimed_at = ? WHERE id = ?", (at, item_id))
                return dict(row)

        return await self._run(claim)

    async def change_cross_chat(
        self, item_id: int, before: str, after: str, at: str, *, target: int | None = None,
        day_start: str | None = None, reason: str | None = None, sent_msg_id: int | None = None,
    ) -> bool:
        def change() -> bool:
            event = None
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT c.*, p.status AS approval_status, "
                    "(SELECT title FROM chats WHERE chat_id = CAST(c.target_chat_id AS TEXT)) AS target_title "
                    "FROM cross_chat c LEFT JOIN proposals p ON c.proposal_id = p.id WHERE c.id = ? AND c.status = ?",
                    (item_id, before),
                ).fetchone()
                if row is None or (target is not None and target != row["target_chat_id"]):
                    return False
                status, why = after, reason
                if status == "sending":
                    if not self._is_chat_approved(str(row["target_chat_id"])):
                        status, why = "dropped", "chat gone"
                    elif row["kind"] == "message":
                        if row["approval_status"] != "approved":
                            status, why = "dropped", "refused"
                        elif datetime.fromisoformat(at) - datetime.fromisoformat(row["created_at"]) >= timedelta(hours=2):
                            status, why = "dropped", "expired"
                        elif day_start is not None and self._cross_chat_send_count(day_start) >= 10:
                            status, why = "dropped", "daily limit"
                if status == "dropped":
                    title = row["target_title"] or str(row["target_chat_id"])
                    words = " ".join(row["text"].split())[:80]
                    event = f'Delivery to "{title}" dropped ({why}): {words}'
                    if why == "expired":
                        conn.execute(
                            "UPDATE proposals SET status = 'expired', decided_at = ? "
                            "WHERE id = ? AND status IN ('pending', 'approved')", (at, row["proposal_id"]),
                        )
                elif status == "sent":
                    if row["kind"] == "alert":
                        event = row["text"]
                    elif row["kind"] == "message":
                        title = row["target_title"] or str(row["target_chat_id"])
                        event = f'Sent to "{title}": {row["text"][:80]}'
                conn.execute(
                    "UPDATE cross_chat SET status = ?, updated_at = ?, drop_reason = ?, sent_msg_id = ?, "
                    "queued_at = CASE WHEN ? = 'queued' THEN COALESCE(queued_at, ?) ELSE queued_at END, "
                    "sending_at = CASE WHEN ? = 'sending' THEN ? ELSE sending_at END, "
                    "status_history = json_insert(status_history, '$[#]', json_object('status', ?, 'at', ?)) WHERE id = ?",
                    (status, at, why, sent_msg_id, status, at, status, at, status, at, item_id),
                )
                if event and self.owner_id is not None:
                    self._insert_owner_event(conn, event, at)
            if event:
                self._append_owner_event_transcript(event, at)
            return status == after

        return await self._run(change)

    async def prune_cross_chat(self, before: datetime) -> None:
        await self._run(
            self._conn.execute,
            "DELETE FROM cross_chat WHERE created_at < ? AND status IN ('sent', 'dropped')",
            (before.astimezone(timezone.utc).isoformat(),),
        )

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

    async def update_proposal_cards(self, key: str, changes: dict[str, list[int]], *, remove: bool = False) -> None:
        def update() -> None:
            with self._tx() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
                try:
                    cards = json.loads(row[0] or "{}") if row else {}
                except ValueError:
                    cards = {}
                if not isinstance(cards, dict):
                    cards = {}
                for proposal, ids in changes.items():
                    old = cards.get(proposal, [])
                    current = [mid for mid in old if mid not in ids] if remove else list(dict.fromkeys([*old, *ids]))
                    if current:
                        cards[proposal] = current
                    else:
                        cards.pop(proposal, None)
                self._write_kv(conn, {key: json.dumps(cards)})

        await self._run(update)

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
                event = f"{status.capitalize()} proposal #{proposal_id}" if row and row["kind"] == "cross_chat" else None
                if event and self.owner_id is not None:
                    self._insert_owner_event(conn, event, at)
            if event:
                self._append_owner_event_transcript(event, at)
            return row is not None

        return await self._run(decide)

    async def expire_proposals(self, before: str, at: str) -> None:
        await self._run(self._conn.execute,
            "UPDATE proposals SET status = 'expired', decided_at = ? WHERE status = 'pending' AND created_at < ? AND kind != 'cross_chat'",
            (at, before),
        )

    async def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        def select() -> dict[str, Any] | None:
            row = self._conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            return dict(row) if row is not None else None

        return await self._run(select)

    async def settled_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM proposals WHERE status != 'pending' "
                "ORDER BY COALESCE(decided_at, created_at) DESC, id DESC LIMIT ?",
                (limit,),
            )
        ])

    async def delete_settled_proposal(
        self, proposal_id: int, expected_created_at: str, event_text: str, event_at: str, *, cards_key: str = "proposal-cards",
    ) -> str:
        """Verify identity and delete a settled proposal + card reference + record owner event in one tx."""
        def delete() -> str:
            with self._tx() as conn:
                row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
                if row is None:
                    return "not_found"
                if row["status"] == "pending":
                    return "pending"
                if not stamp_matches(row["created_at"], expected_created_at):
                    return "stale"
                if row["kind"] == "cross_chat" and conn.execute(
                    "SELECT 1 FROM cross_chat WHERE proposal_id = ? AND status IN ('awaiting_approval', 'queued', 'sending')",
                    (proposal_id,),
                ).fetchone():
                    return "in_flight"
                conn.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
                raw_cards = conn.execute("SELECT value FROM kv WHERE key = ?", (cards_key,)).fetchone()
                if raw_cards is not None:
                    try:
                        cards = json.loads(raw_cards[0] or "{}")
                    except ValueError:
                        cards = {}
                    if isinstance(cards, dict) and str(proposal_id) in cards:
                        cards.pop(str(proposal_id), None)
                        self._write_kv(conn, {cards_key: json.dumps(cards)})
                self._insert_owner_event(conn, event_text, event_at)
            self._append_owner_event_transcript(event_text, event_at)
            return "deleted"

        return await self._run(delete)

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

    async def cancel_reminder(
        self, chat_id: int | None, reminder_id: int, expected_created_at: str | None = None,
    ) -> bool:
        """Cancel an active reminder. With `expected_created_at`, only the reminder created then: the check and the
        update share one transaction, so a stale selection never cancels a new reminder that reused the id."""
        def cancel() -> bool:
            with self._tx() as conn:
                if expected_created_at is not None:
                    row = conn.execute(
                        "SELECT created_at FROM reminders WHERE chat_id = ? AND id = ?", (str(chat_id), reminder_id),
                    ).fetchone()
                    if row is None or not stamp_matches(row["created_at"], expected_created_at):
                        return False
                return conn.execute(
                    "UPDATE reminders SET status = 'cancelled' WHERE chat_id = ? AND id = ? AND status = 'active'",
                    (str(chat_id), reminder_id),
                ).rowcount == 1

        return await self._run(cancel)

    async def finish_reminder(self, reminder_id: int, fired_at: str, next_due: str | None) -> None:
        await self._run(self._conn.execute,
            "UPDATE reminders SET fired_at = ?, status = ?, due_at = COALESCE(?, due_at), undeliverable = 0 "
            "WHERE id = ? AND status = 'active'",
            (fired_at, "active" if next_due else "done", next_due, reminder_id),
        )

    async def set_reminder_undeliverable(self, reminder_id: int, undeliverable: bool = True) -> None:
        await self._run(
            self._conn.execute,
            "UPDATE reminders SET undeliverable = ? WHERE id = ? AND status = 'active'",
            (int(undeliverable), reminder_id),
        )

    async def get_reminder(self, chat_id: int | None, reminder_id: int) -> dict[str, Any] | None:
        if chat_id is None:
            return None

        def select() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE chat_id = ? AND id = ?", (str(chat_id), reminder_id),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self._run(select)

    async def list_reminders(self, chat_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM reminders WHERE chat_id = ? "
                "ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, "
                "CASE WHEN status = 'active' THEN due_at END ASC, id DESC LIMIT ?",
                (str(chat_id), limit),
            )
        ])

    async def delete_reminder(
        self,
        chat_id: int | None,
        reminder_id: int,
        expected_created_at: str,
        event_text: str,
        event_at: str,
        *,
        retry_key: str | None = None,
    ) -> str:
        """Verify identity and delete one reminder, its retry KV row, and record owner event in a single transaction."""
        if chat_id is None:
            return "not_found"
        kv_key = retry_key or f"reminder_retry:{reminder_id}"

        def delete() -> str:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT * FROM reminders WHERE chat_id = ? AND id = ?", (str(chat_id), reminder_id),
                ).fetchone()
                if row is None:
                    return "not_found"
                if not stamp_matches(row["created_at"], expected_created_at):
                    return "stale"
                conn.execute("DELETE FROM reminders WHERE chat_id = ? AND id = ?", (str(chat_id), reminder_id))
                conn.execute("DELETE FROM kv WHERE key = ?", (kv_key,))
                self._insert_owner_event(conn, event_text, event_at)
            self._append_owner_event_transcript(event_text, event_at)
            return "deleted"

        return await self._run(delete)

    # ---- usage ------------------------------------------------------------

    async def usage_by_day(self, bounds: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        values = ", ".join(["(?, ?, ?)"] * len(bounds))
        return await self._run(lambda: [
            dict(row) for row in self._conn.execute(
                f"WITH days(day, start_at, end_at) AS (VALUES {values}) "
                "SELECT day, kind, model, COUNT(*) AS calls, TOTAL(cost_usd) AS cost, SUM(fallback) AS fallbacks, "
                "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
                "FROM days JOIN usage ON at >= start_at AND at < end_at GROUP BY day, kind, model "
                "ORDER BY day, kind, model",
                [value for day in bounds for value in day],
            )
        ]) if bounds else []

    async def record_usage(
        self, at: str, kind: str, model: str, host: str, chat_id: int | None,
        input_tokens: int, output_tokens: int, cost_usd: float, fallback: int,
    ) -> int:
        cursor = await self._run(
            self._conn.execute,
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, kind, model, host, chat_id, input_tokens, output_tokens, cost_usd, fallback),
        )
        return cursor.lastrowid

    async def set_usage_cost(
        self, usage_id: int, cost_usd: float, *, input_tokens: int | None = None, output_tokens: int | None = None,
    ) -> None:
        """Set the reported cost and any token counts that arrived after the call."""
        await self._run(
            self._conn.execute,
            "UPDATE usage SET cost_usd = ?, input_tokens = COALESCE(?, input_tokens), "
            "output_tokens = COALESCE(?, output_tokens) WHERE id = ?",
            (cost_usd, input_tokens, output_tokens, usage_id),
        )

    async def delete_usage(self, usage_id: int) -> None:
        """Remove a reserved usage row when a request failed before being accepted."""
        await self._run(self._conn.execute, "DELETE FROM usage WHERE id = ?", (usage_id,))

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
            aliases = sorted(self._chat_id_aliases(requester_chat_id))
            marks = ",".join("?" * len(aliases))
            sql += f" AND m.chat_id IN ({marks}) AND m.visibility_scope NOT IN {_PRIVATE_SCOPES_SQL}"
            params.extend(aliases)
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

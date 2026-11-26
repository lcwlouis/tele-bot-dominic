"""Rebuildable entity graph and hybrid meaning index over messages and Markdown notes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import struct
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from dom import notes
from dom.config import KnowledgeConfig
from dom.embedding import EmbeddingProvider, EmbeddingRouter
from dom.home import Home
from dom.store import Store
from dom.store.store import recency_weight
from dom.usage import ModelCallSkipped, model_request

log = logging.getLogger("dom.knowledge")
BATCH_SIZE = 16
SYNC_CHUNK = 500  # messages read per short transaction
SWEEP_CHUNK = 2000  # message facts checked per short transaction
SWEEP_EVERY = timedelta(hours=1)  # the safety-net sweep for messages removed by a path that skips the store hooks
PROBE_TEXT = "Dom remembers a quiet afternoon and the promise attached to it."
PROBE_DRIFT = 0.08
MAX_RESULTS = 30
KEYWORD_WEIGHT, MEANING_WEIGHT = 0.45, 0.55
# What a score means, for the label Dom and the dashboard show. A blended score tops out at 1 for a hit that matches
# the words and the meaning, and "strong" needs both signals; a hit found by meaning alone peaks at MEANING_WEIGHT,
# so "good" is its normal best.
STRONG_SCORE, GOOD_SCORE = 0.5, 0.3
_AS_OF = re.compile(r"\(as of (\d{4}-\d{2})(?:-\d{2})?\)")
_UNTIL = re.compile(r"\(until (\d{4}-\d{2}-\d{2})\)")
_CHAT_NOTE = re.compile(r"memory/chats/(-?\d+)\.md")
_ENTITY_NOTE = re.compile(r"memory/(?:places|projects|goals)/[a-z0-9-]+\.md")
_INDEX_RULES = "2"  # bump when what a line's tags mean changes, so every note file is read again
_PRIVATE_SCOPES = ("owner_private", "admin_private")
# A message fact counts only while its message is live: deletions that skip the store hooks still hide it.
_LIVE = "(k.message_id IS NULL OR EXISTS (SELECT 1 FROM messages m WHERE m.id = k.message_id AND m.is_deleted = 0))"


@dataclass(frozen=True)
class KnowledgeHit:
    text: str
    file: str
    source_chat: str | None
    at: str
    entities: tuple[str, ...]
    score: float = 0.0
    message_id: int | None = None  # the messages row a message fact came from; None for note lines
    both: bool = False  # found by the words and by the meaning, which "strong" requires

    @property
    def match(self) -> str:
        """How far to trust the hit: strong, good or weak (see STRONG_SCORE)."""
        return match_label(self.score, self.both)


def match_label(score: float, both: bool = True) -> str:
    return "strong" if score >= STRONG_SCORE and both else "good" if score >= GOOD_SCORE else "weak"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blob(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unblob(value: bytes, dimensions: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dimensions}f", value)


def _cosine_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    return 1.0 if not left_norm or not right_norm else 1.0 - dot / (left_norm * right_norm)


def _fts_query(query: str) -> str:
    return " ".join('"' + token.replace('"', '""') + '"' for token in query.split()[:12] if token)


def _file_entity(rel: str) -> str | None:
    match = re.fullmatch(r"memory/(people|places|projects|goals)/([a-z0-9-]+)\.md", rel)
    if match is None:
        return None
    kind = {"people": "person", "places": "place", "projects": "project", "goals": "goal"}[match.group(1)]
    return f"{kind}:{match.group(2)}"


def _entity_parts(ref: str) -> tuple[str, str, str]:
    kind, key = ref.split(":", 1)
    return kind, key, key.replace("-", " ")


def _requester(chat_id: int | str, default: int | None) -> int | None:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return default


def _never() -> bool:
    return False


class KnowledgeIndex:
    def __init__(
        self,
        store: Store,
        home: Home,
        router: EmbeddingRouter | None = None,
        *,
        allowed: Callable[[], Awaitable[bool]] | None = None,
        chat_id: int | None = None,
        config: KnowledgeConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config or KnowledgeConfig()
        self.home = home
        self.allowed = allowed or _allowed
        self.chat_id = chat_id
        self.router, self.staged_router = self._boot_routes(router)
        self._indexing_keys: set[str] = set()
        self._refresh_lock = asyncio.Lock()
        self._reindexing = False
        self._cancel = False
        self._no_vec_logged = False

    def _boot_routes(self, router: EmbeddingRouter | None) -> tuple[EmbeddingRouter | None, EmbeddingRouter | None]:
        """What serves searches at start-up, and what waits for the owner. The config's model is only a request:
        while a complete index of another model exists, that model's index serves and the config model is staged, so
        nothing is spent on a partial index before the owner confirms. A model that is no longer configured has no
        endpoint left to embed queries with, so meaning search uses keywords until the reindex is confirmed."""
        if router is None:
            return None, None
        with self.store._lock:
            active = {row[0] for row in self.store._conn.execute(
                "SELECT model_key FROM embedding_indexes WHERE active = 1"
            )}
        if not active:
            return router, None
        serving = [provider for provider in router.providers if provider.model_key in active]
        if len(serving) == len(router.providers):
            return router, None
        log.info("embedding model changed since the last complete index; confirm the reindex to switch")
        return (EmbeddingRouter(*serving) if serving else None), router

    # ---- sources and graph -------------------------------------------------

    def _note_paths(self) -> list[str]:
        people, chats = self.home.list_memory_notes()
        public = ["memory/MEMORY.md", *people, *chats, *self.home.list_entity_notes()]
        private = ["private/USER.md", "private/NOTES.md"]
        return [rel for rel in (*public, *private) if (self.home.root / rel).is_file()]

    async def refresh(self, *, full: bool = False) -> None:
        """Incrementally replace changed note files, then mirror new and edited messages. No model call."""
        async with self._refresh_lock:
            paths = self._note_paths()
            known = await self.store._run(self._known_files)
            for rel in paths:
                try:
                    text = self.home.scoped(rel.startswith("private/")).read(rel)
                except (OSError, ValueError, PermissionError):
                    continue
                fingerprint = _hash(f"{_INDEX_RULES}\n{text}")
                if full or known.get(rel) != fingerprint:
                    facts = self._facts(rel, text)
                    await self.store._run(self._replace_file, rel, fingerprint, facts)
            missing = set(known) - set(paths)
            if missing:
                await self.store._run(self._drop_files, missing)
            if full:
                await self.store._run(self._reset_messages)
            while await self.store._run(self._sync_chunk):
                await asyncio.sleep(0)  # between short transactions, let the loop and other store work in
            if full or await self.store._run(self._sweep_due):
                while await self.store._run(self._sweep_chunk):
                    await asyncio.sleep(0)
            await self.store._run(self._prune_vectors)

    async def rebuild(self) -> dict[str, int]:
        await self.refresh(full=True)
        return await self.store._run(self._counts)

    def _known_files(self) -> dict[str, str]:
        return {row["path"]: row["fingerprint"] for row in self.store._conn.execute(
            "SELECT path, fingerprint FROM knowledge_files"
        )}

    @staticmethod
    def _facts(rel: str, text: str) -> list[dict[str, Any]]:
        found = []
        chat_note = _CHAT_NOTE.fullmatch(rel)
        for number, raw in enumerate(text.splitlines(), 1):
            if not notes.is_bullet(raw) or notes.blank_body(raw):
                continue
            line = notes.parse_line(raw)
            body = line.body.strip()
            entities = list(line.entities)
            if inferred := _file_entity(rel):
                entities.append(inferred)
            source_chat = None if line.source in (None, notes.ANY) else line.source
            private = int(rel.startswith("private/"))
            if chat_note is not None:
                source_chat = chat_note.group(1)  # a chat's note belongs to that chat alone, whatever a line's tag says
            elif line.source is None and _ENTITY_NOTE.fullmatch(rel):
                private = 1  # an untagged line in a place, project or goal note is the owner's; {any} shares it
            if source_chat is not None:
                entities.append(f"chat:{source_chat}")
            valid_from = (_AS_OF.search(body).group(1) if _AS_OF.search(body) else "")
            valid_until = (_UNTIL.search(body).group(1) if _UNTIL.search(body) else "")
            found.append({
                "source_key": f"note:{rel}:{number}", "file": rel, "line_number": number,
                "text": body, "source_chat": source_chat, "private": private,
                "at": valid_from, "valid_from": valid_from, "valid_until": valid_until,
                "pinned": int(line.core is not None), "content_hash": _hash(body),
                "entities": tuple(dict.fromkeys(entities)),
            })
        return found

    def _replace_file(self, rel: str, fingerprint: str, facts: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.store._tx() as conn:
            conn.execute("DELETE FROM knowledge_facts WHERE source_kind = 'note' AND file = ?", (rel,))
            for fact in facts:
                fact_id = self._insert_fact(conn, "note", fact)
                self._link_entities(conn, fact_id, fact["entities"])
            conn.execute(
                "INSERT INTO knowledge_files(path, fingerprint, indexed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint, indexed_at=excluded.indexed_at",
                (rel, fingerprint, now),
            )
            self.store.prune_knowledge_entities(conn)

    def _drop_files(self, paths: set[str]) -> None:
        with self.store._tx() as conn:
            for rel in paths:
                conn.execute("DELETE FROM knowledge_facts WHERE source_kind = 'note' AND file = ?", (rel,))
                conn.execute("DELETE FROM knowledge_files WHERE path = ?", (rel,))
            self.store.prune_knowledge_entities(conn)

    @staticmethod
    def _insert_fact(conn, kind: str, fact: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO knowledge_facts(source_key, source_kind, file, line_number, text, source_chat, private, "
            "at, valid_from, valid_until, pinned, content_hash, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fact["source_key"], kind, fact.get("file", ""), fact.get("line_number"), fact["text"],
             fact.get("source_chat"), fact.get("private", 0), fact.get("at", ""), fact.get("valid_from", ""),
             fact.get("valid_until", ""), fact.get("pinned", 0), fact["content_hash"], fact.get("message_id")),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _link_entities(conn, fact_id: int, entities: tuple[str, ...] | list[str]) -> None:
        for ref in dict.fromkeys(entities):
            kind, key, label = _entity_parts(ref)
            conn.execute(
                "INSERT INTO knowledge_entities(kind, entity_key, label) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, entity_key) DO UPDATE SET label=excluded.label", (kind, key, label),
            )
            entity_id = conn.execute(
                "SELECT id FROM knowledge_entities WHERE kind = ? AND entity_key = ?", (kind, key),
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_fact_entities(fact_id, entity_id) VALUES (?, ?)",
                (fact_id, entity_id),
            )

    # ---- messages: a cursor, an edit queue and a rare sweep ------------------------------------------

    def _reset_messages(self) -> None:
        with self.store._tx() as conn:
            conn.execute("DELETE FROM knowledge_facts WHERE source_kind = 'message'")
            conn.execute("DELETE FROM knowledge_dirty_messages")
            conn.execute("UPDATE knowledge_sync SET last_message_id = 0, sweep_after_fact_id = 0, swept_at = ''")
            self.store.prune_knowledge_entities(conn)

    def _sync_chunk(self) -> bool:
        """Mirror the next messages past the cursor, and the ones edited since they were read. One short transaction;
        True when more is waiting."""
        columns = "id, chat_id, visibility_scope, role, text, created_at, is_deleted"
        with self.store._tx() as conn:
            cursor = conn.execute("SELECT last_message_id FROM knowledge_sync WHERE id = 1").fetchone()[0]
            fresh = conn.execute(
                f"SELECT {columns} FROM messages WHERE id > ? ORDER BY id LIMIT ?", (cursor, SYNC_CHUNK),
            ).fetchall()
            edited_ids = [row[0] for row in conn.execute(
                "SELECT message_id FROM knowledge_dirty_messages ORDER BY message_id LIMIT ?", (SYNC_CHUNK,),
            )]
            edited = []
            for chunk in range(0, len(edited_ids), 500):
                part = edited_ids[chunk:chunk + 500]
                edited += conn.execute(
                    f"SELECT {columns} FROM messages WHERE id IN ({','.join('?' * len(part))})", part,
                ).fetchall()
            for row in [*edited, *fresh]:
                conn.execute("DELETE FROM knowledge_facts WHERE message_id = ?", (row["id"],))
                if row["is_deleted"] or row["role"] == "event" or not row["text"].strip():
                    continue
                private = int(row["visibility_scope"] in _PRIVATE_SCOPES)
                fact_id = self._insert_fact(conn, "message", {
                    "source_key": f"message:{row['id']}", "text": row["text"], "source_chat": row["chat_id"],
                    "private": private, "at": row["created_at"], "content_hash": _hash(row["text"]),
                    "message_id": row["id"],
                })
                self._link_entities(conn, fact_id, (f"chat:{row['chat_id']}",))
            if edited_ids:
                conn.execute(
                    f"DELETE FROM knowledge_dirty_messages WHERE message_id IN ({','.join('?' * len(edited_ids))})",
                    edited_ids,
                )
            if fresh:
                conn.execute("UPDATE knowledge_sync SET last_message_id = ?", (fresh[-1]["id"],))
            self.store.prune_knowledge_entities(conn)
            return len(fresh) == SYNC_CHUNK or len(edited_ids) == SYNC_CHUNK

    def _sweep_due(self) -> bool:
        row = self.store._conn.execute("SELECT sweep_after_fact_id, swept_at FROM knowledge_sync WHERE id = 1").fetchone()
        if row["sweep_after_fact_id"]:
            return True  # a sweep is part-way through
        if not row["swept_at"]:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(row["swept_at"]) > SWEEP_EVERY
        except ValueError:
            return True

    def _sweep_chunk(self) -> bool:
        """Drop message facts whose message is gone or deleted by a path that never told the index. Chunked so no
        transaction is long; True while more facts are left to check."""
        with self.store._tx() as conn:
            after = conn.execute("SELECT sweep_after_fact_id FROM knowledge_sync WHERE id = 1").fetchone()[0]
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM knowledge_facts WHERE source_kind = 'message' AND id > ? ORDER BY id LIMIT ?",
                (after, SWEEP_CHUNK),
            )]
            if ids:
                conn.execute(
                    "DELETE FROM knowledge_facts WHERE id BETWEEN ? AND ? AND source_kind = 'message' AND NOT EXISTS "
                    "(SELECT 1 FROM messages m WHERE m.id = knowledge_facts.message_id AND m.is_deleted = 0)",
                    (ids[0], ids[-1]),
                )
                self.store.prune_knowledge_entities(conn)
            more = len(ids) == SWEEP_CHUNK
            if more:
                conn.execute("UPDATE knowledge_sync SET sweep_after_fact_id = ?", (ids[-1],))
            else:
                conn.execute(
                    "UPDATE knowledge_sync SET sweep_after_fact_id = 0, swept_at = ?",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            return more

    def _prune_vectors(self) -> None:
        """Vectors of texts no fact has any more. Cheap, and keeps an edited line's old vector from piling up."""
        with self.store._tx() as conn:
            conn.execute(
                "DELETE FROM knowledge_vectors WHERE NOT EXISTS "
                "(SELECT 1 FROM knowledge_facts k WHERE k.content_hash = knowledge_vectors.content_hash)"
            )

    def _counts(self) -> dict[str, int]:
        return {
            "files": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_files").fetchone()[0],
            "facts": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0],
            "entities": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0],
        }

    # ---- retrieval --------------------------------------------------------

    async def search(
        self, query: str, *, requester_chat_id: int | str, owner: bool = False,
        mode: str = "keyword", limit: int = 10, participants: Iterable[int] = (),
    ) -> list[KnowledgeHit]:
        """`participants` are the people present in the requesting chat: another chat sees a person's note only for
        them. The owner's DM (`owner=True`) sees every public line."""
        query = " ".join(query.split())[:200]
        limit = max(1, min(limit, 30))
        if not query:
            return []
        chat, people = str(requester_chat_id), tuple(participants)
        if mode == "entity":
            hits = await self.store._run(self._entity_search, query, chat, owner, people, limit)
            return self._by_score(hits)
        keyword = await self.store._run(self._keyword_rows, query, chat, owner, people, MAX_RESULTS)
        if mode != "meaning" or self.router is None:
            return self._by_score(self._hits(keyword))[:limit]
        semantic = await self._meaning_rows(query, chat, owner, people, _requester(requester_chat_id, self.chat_id))
        if not semantic:
            log.info("meaning search fell back to keywords")
            return self._by_score(self._hits(keyword))[:limit]
        return self._blend(keyword, semantic, limit)

    @staticmethod
    def _by_score(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
        """Best first, strictly by score; hits of equal score keep the order they came in."""
        return sorted(hits, key=lambda hit: hit.score, reverse=True)

    async def _meaning_rows(
        self, query: str, chat_id: str, owner: bool, people: tuple[int, ...], requester: int | None,
    ) -> list[dict[str, Any]]:
        """Vector hits from the first provider whose own index can answer. A provider whose index is stale or still
        empty is skipped before any paid call; one that answers with nothing for this chat is final, never a reason
        to spend on the backup."""
        assert self.router is not None
        if not self.store.vec_ok:
            return []
        for index, provider in enumerate(self.router.providers):
            if not await self.store._run(self._servable, provider.model_key, provider.endpoint.model):
                continue
            try:
                with model_request("embedding", requester, self.allowed):
                    batch = await provider.embed([query], fallback=index > 0)
                return await self.store._run(
                    self._vector_rows, batch.model_key, batch.model_id, batch.vectors[0],
                    chat_id, owner, people, MAX_RESULTS,
                )
            except (ModelCallSkipped, httpx.HTTPError, ValueError, RuntimeError, sqlite3.Error):
                log.info("embedding query provider unavailable", exc_info=True)
        return []

    def _servable(self, model_key: str, model_id: str) -> bool:
        row = self.store._conn.execute(
            "SELECT stale FROM embedding_indexes WHERE model_key = ? AND model_id = ?", (model_key, model_id),
        ).fetchone()
        if row is None or row["stale"]:
            return False
        return self.store._conn.execute(
            "SELECT 1 FROM knowledge_vectors WHERE model_key = ? AND model_id = ? LIMIT 1", (model_key, model_id),
        ).fetchone() is not None

    def _scope(self, owner: bool, chat_id: str, people: tuple[int, ...]) -> tuple[str, list[Any]]:
        """The SQL that keeps a requester to the lines it may see, enforced here and never by the prompt. The owner's
        DM sees every public and private line. Anyone else gets no private line, no line tagged to another chat
        (messages and untagged lines of another chat's note carry that chat), and a person's note only for people
        in the chat, the same reach as the notes prompt."""
        if owner:
            return _LIVE, []
        aliases = sorted(self.store._chat_id_aliases(chat_id))
        chats = ",".join("?" * len(aliases))
        sql = f"{_LIVE} AND k.private = 0 AND (k.source_chat IS NULL OR k.source_chat IN ({chats}))"
        params: list[Any] = list(aliases)
        files = [f"memory/people/{int(uid)}.md" for uid in people]
        sql += " AND (k.file NOT LIKE 'memory/people/%'"
        if files:
            sql += f" OR k.file IN ({','.join('?' * len(files))})"
            params += files
        return sql + ")", params

    def _keyword_rows(
        self, query: str, chat_id: str, owner: bool, people: tuple[int, ...], limit: int,
    ) -> list[dict[str, Any]]:
        match = _fts_query(query)
        if not match:
            return []
        scope, scope_params = self._scope(owner, chat_id, people)
        rows = self.store._conn.execute(
            "SELECT k.*, bm25(knowledge_facts_fts) AS bm25 FROM knowledge_facts_fts "
            "JOIN knowledge_facts k ON k.id = knowledge_facts_fts.rowid "
            f"WHERE knowledge_facts_fts MATCH ? AND {scope} ORDER BY bm25 LIMIT ?", [match, *scope_params, limit],
        ).fetchall()
        # bm25 is negative and smaller is better: turn it into a 0..1 relevance, 1.0 for the best hit of this search
        relevance = [max(0.0, -float(row["bm25"])) for row in rows]
        best = max(relevance, default=0.0)
        return [
            {**self._row(row), "score": value / best if best > 0 else 1.0}
            for row, value in zip(rows, relevance)
        ]

    def _vector_rows(
        self, model_key: str, model_id: str, vector: tuple[float, ...], chat_id: str, owner: bool,
        people: tuple[int, ...], limit: int,
    ) -> list[dict[str, Any]]:
        if not self.store.vec_ok:
            return []
        state = self.store._conn.execute(
            "SELECT stale FROM embedding_indexes WHERE model_key = ?", (model_key,),
        ).fetchone()
        if state is not None and state["stale"]:
            return []
        scope, scope_params = self._scope(owner, chat_id, people)
        try:
            rows = self.store._conn.execute(
                "SELECT k.*, vec_distance_cosine(v.vector, ?) AS distance FROM knowledge_vectors v "
                "JOIN knowledge_facts k ON k.content_hash = v.content_hash "
                f"WHERE v.model_key = ? AND v.model_id = ? AND v.dimensions = ? AND {scope} "
                "ORDER BY distance LIMIT ?",
                [_blob(vector), model_key, model_id, len(vector), *scope_params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            log.warning("vector query failed; meaning search uses keywords", exc_info=True)
            return []
        similar = [{**self._row(row), "score": max(0.0, 1.0 - float(row["distance"]))} for row in rows]
        return [row for row in similar if row["score"] >= self.config.min_similarity]

    def _entity_search(
        self, query: str, chat_id: str, owner: bool, people: tuple[int, ...], limit: int,
    ) -> list[KnowledgeHit]:
        needle = query.strip().lower().replace(" ", "-")
        entities = self.store._conn.execute(
            "SELECT id, kind, entity_key FROM knowledge_entities "
            "WHERE lower(label) LIKE ? OR lower(entity_key) = ? ORDER BY kind, entity_key LIMIT 8",
            (f"%{query.strip().lower()}%", needle),
        ).fetchall()
        if not entities:
            return []
        marks = ",".join("?" * len(entities))
        scope, scope_params = self._scope(owner, chat_id, people)
        rows = self.store._conn.execute(
            "SELECT DISTINCT k.* FROM knowledge_facts k "
            "JOIN knowledge_fact_entities fe ON fe.fact_id = k.id "
            f"WHERE fe.entity_id IN ({marks}) AND {scope} ORDER BY k.pinned DESC, k.at DESC LIMIT ?",
            [*(row["id"] for row in entities), *scope_params, limit],
        ).fetchall()
        return self._hits([{**self._row(row), "score": 1.0} for row in rows])

    async def related(
        self, entity: str, *, requester_chat_id: int | str, owner: bool = False, limit: int = 20,
        participants: Iterable[int] = (),
    ) -> list[str]:
        """Entities co-mentioned with ``entity``, after the same code-enforced visibility filter."""
        return await self.store._run(
            self._related, entity, str(requester_chat_id), owner, tuple(participants), limit,
        )

    def _related(self, entity: str, chat_id: str, owner: bool, people: tuple[int, ...], limit: int) -> list[str]:
        kind, _, key = entity.partition(":")
        scope, scope_params = self._scope(owner, chat_id, people)
        rows = self.store._conn.execute(
            "SELECT DISTINCT other.kind || ':' || other.entity_key AS ref FROM knowledge_entities target "
            "JOIN knowledge_fact_entities tf ON tf.entity_id = target.id "
            "JOIN knowledge_facts k ON k.id = tf.fact_id "
            "JOIN knowledge_fact_entities ofe ON ofe.fact_id = k.id "
            "JOIN knowledge_entities other ON other.id = ofe.entity_id "
            f"WHERE target.kind = ? AND target.entity_key = ? AND other.id != target.id AND {scope} "
            "ORDER BY ref LIMIT ?", [kind, key, *scope_params, max(1, min(limit, 50))],
        ).fetchall()
        return [row["ref"] for row in rows]

    def _row(self, row) -> dict[str, Any]:
        entities = tuple(item[0] for item in self.store._conn.execute(
            "SELECT e.kind || ':' || e.entity_key FROM knowledge_fact_entities fe "
            "JOIN knowledge_entities e ON e.id = fe.entity_id WHERE fe.fact_id = ? ORDER BY e.kind, e.entity_key",
            (row["id"],),
        ))
        return {**dict(row), "entities": entities}

    @staticmethod
    def _hits(rows: list[dict[str, Any]]) -> list[KnowledgeHit]:
        return [KnowledgeHit(
            row["text"], row["file"], row["source_chat"], row["at"], row["entities"], float(row.get("score", 0.0)),
            row.get("message_id"),
        ) for row in rows]

    def _blend(
        self, keyword: list[dict[str, Any]], semantic: list[dict[str, Any]], limit: int,
    ) -> list[KnowledgeHit]:
        by_id: dict[int, dict[str, Any]] = {}
        scores: dict[int, float] = {}
        by_words = {row["id"] for row in keyword}
        by_meaning = {row["id"] for row in semantic}
        for row in keyword:
            by_id[row["id"]] = row
            scores[row["id"]] = scores.get(row["id"], 0.0) + KEYWORD_WEIGHT * row["score"]
        for row in semantic:
            by_id[row["id"]] = row
            scores[row["id"]] = scores.get(row["id"], 0.0) + MEANING_WEIGHT * row["score"]
        now = datetime.now(timezone.utc)
        for fact_id, row in by_id.items():
            if row["pinned"] or not row["at"]:
                continue
            try:
                made = datetime.fromisoformat(row["at"])
                if made.tzinfo is None:
                    made = made.replace(tzinfo=timezone.utc)
                scores[fact_id] *= recency_weight((now - made).total_seconds() / 86400)
            except ValueError:
                pass
        kept = [fact_id for fact_id in by_id if scores[fact_id] >= self.config.min_score]
        ranked = sorted(kept, key=lambda fact_id: scores[fact_id], reverse=True)[:limit]
        return [KnowledgeHit(
            by_id[fact_id]["text"], by_id[fact_id]["file"], by_id[fact_id]["source_chat"],
            by_id[fact_id]["at"], by_id[fact_id]["entities"], scores[fact_id], by_id[fact_id].get("message_id"),
            fact_id in by_words and fact_id in by_meaning,
        ) for fact_id in ranked]

    # ---- embedding lifecycle ---------------------------------------------

    def stage(self, router: EmbeddingRouter | None) -> bool:
        """Remember a changed route without replacing the route that still serves the completed index."""
        if router is None:
            self.staged_router = None
            return False
        current = tuple(provider.model_key for provider in self.router.providers) if self.router else ()
        changed = tuple(provider.model_key for provider in router.providers) != current
        self.staged_router = router if changed else None
        return changed

    async def run_pending(self) -> None:
        """Refresh the graph, then embed what is new under the spend cap. The memory pass spawns this."""
        await self.refresh()
        if self.router is None:
            return
        if not self.store.vec_ok:
            if not self._no_vec_logged:
                self._no_vec_logged = True
                log.warning("sqlite-vec is unavailable; not embedding, meaning search uses keywords")
            return
        for provider in self.router.providers:
            if provider.available and await self._index_provider(provider, rebuild=False) and await self.store._run(
                self._ready, provider.model_key, provider.endpoint.model,
            ):
                await self.store._run(self._activate, [provider.model_key], False)

    async def run_background(self) -> None:
        """`run_pending` for a spawned task: a failure is logged here and never surfaces as an unretrieved exception."""
        try:
            await self.run_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("knowledge index refresh failed")

    async def estimate(self) -> dict[str, Any]:
        """What a confirm would start: a staged model, or the serving model rebuilt after its probe drifted."""
        staged = self.staged_router
        rebuild = staged is None and self.router is not None and await self.store._run(
            self._stale, self.router.providers[0].model_key,
        )
        chosen = staged or (self.router if rebuild else None)
        total = await self.store._run(self._distinct_texts)
        return {
            "items": total, "rough_seconds": round(total * 0.55, 1),
            "model": chosen.providers[0].endpoint.model if chosen else "",
            "staged": chosen is not None, "rebuild": bool(rebuild),
        }

    async def reindex(self) -> bool:
        """Build the staged model beside the serving one and switch only when it is complete. With nothing staged but
        a drifted serving index, rebuild that model in place (its search is already off while it is stale)."""
        if self._reindexing:
            return False
        router, in_place = self.staged_router, False
        if router is None:
            if self.router is None or not await self.store._run(self._stale, self.router.providers[0].model_key):
                return False
            router, in_place = self.router, True
        self._reindexing, self._cancel = True, False
        try:
            return await self._reindex(router, in_place)
        finally:
            self._reindexing, self._cancel = False, False

    async def _reindex(self, router: EmbeddingRouter, in_place: bool) -> bool:
        await self.refresh()
        current_keys = {provider.model_key for provider in self.router.providers} if self.router else set()
        completed: list[EmbeddingProvider] = []
        for provider in router.providers:
            if self._cancel:
                break
            if not provider.available:
                continue
            key = provider.model_key
            fresh = key not in current_keys
            if fresh or await self.store._run(self._stale, key):
                await self.store._run(self._reset_model, provider)
                rebuild = True
            else:
                rebuild = False
            if await self._index_provider(provider, rebuild=rebuild, stop=lambda: self._cancel) and await self.store._run(
                self._ready, key, provider.endpoint.model,
            ):
                completed.append(provider)
        if self._cancel:
            # Keep the flag until the whole run has stopped, then leave nothing of the staged model behind.
            if not in_place:
                await self.store._run(self._drop_models, [p.model_key for p in router.providers if p.model_key not in current_keys])
                self.staged_router = None
            return False
        if not completed:
            return False
        await self.store._run(self._activate, [provider.model_key for provider in completed], True)
        self.router, self.staged_router = router, None
        if not in_place:  # after the switch, so no search ever finds the serving model without its vectors
            await self.store._run(self._drop_other_models, [provider.model_key for provider in router.providers])
        return True

    async def cancel(self) -> None:
        """Stop a running reindex between batches and providers, or discard a staged model that has not started."""
        if self._reindexing:
            self._cancel = True
            return
        router, self.staged_router = self.staged_router, None  # at once: a reindex task about to start finds nothing
        if router is None:
            return
        current_keys = {provider.model_key for provider in self.router.providers} if self.router else set()
        await self.store._run(self._drop_models, [p.model_key for p in router.providers if p.model_key not in current_keys])

    async def progress(self) -> dict[str, Any]:
        router = self.staged_router or self.router
        if router is None:
            total = await self.store._run(self._distinct_texts)
            return {"done": 0, "left": total, "model": "", "state": "idle", "last_error": "", "stale": False}
        provider = router.providers[0]
        progress = await self.store._run(self._progress, provider.model_key, provider.endpoint.model)
        if progress["state"] == "running" and provider.model_key not in self._indexing_keys:
            progress["state"] = "paused"  # the run that wrote "running" is gone (a restart or a crash)
        return progress

    async def _index_provider(
        self, provider: EmbeddingProvider, *, rebuild: bool, stop: Callable[[], bool] = _never,
    ) -> bool:
        key = provider.model_key
        if key in self._indexing_keys:
            return False
        self._indexing_keys.add(key)
        try:
            await self.store._run(self._start_state, provider)
            try:
                return await self._embed_pending(provider, rebuild, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A database or provider surprise pauses this index; it never leaves it "running" or breaks the caller.
                log.warning("embedding index paused for %s: %s", provider.endpoint.model, type(exc).__name__, exc_info=True)
                await self.store._run(self._finish_state, key, "paused", type(exc).__name__)
                return False
        finally:
            self._indexing_keys.discard(key)

    async def _embed_pending(self, provider: EmbeddingProvider, rebuild: bool, stop: Callable[[], bool]) -> bool:
        key = provider.model_key
        if not rebuild and await self.store._run(self._stale, key):
            await self.store._run(self._finish_state, key, "paused", "probe drift")
            return False
        first = True
        while True:
            if stop():
                await self.store._run(self._finish_state, key, "idle", "")
                return False
            pending = await self.store._run(self._pending, key, BATCH_SIZE)
            if not pending:
                await self.store._run(self._finish_state, key, "idle", "")
                return True
            texts = [row["text"] for row in pending]
            request = [PROBE_TEXT, *texts] if first else texts
            try:
                with model_request("embedding", self.chat_id, self.allowed):
                    batch = await provider.embed(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = "spend cap reached" if isinstance(exc, ModelCallSkipped) else type(exc).__name__
                log.warning("embedding index paused for %s: %s", provider.endpoint.model, error)
                await self.store._run(self._finish_state, key, "paused", error)
                return False
            offset = 0
            if first:
                drifted = await self.store._run(self._save_probe, provider, batch.vectors[0])
                if drifted and not rebuild:
                    await self.store._run(self._finish_state, key, "paused", "probe drift")
                    return False
                offset = 1
                first = False
            await self.store._run(self._save_vectors, provider, pending, batch.vectors[offset:])

    def _distinct_texts(self) -> int:
        return self.store._conn.execute("SELECT COUNT(DISTINCT content_hash) FROM knowledge_facts").fetchone()[0]

    def _done(self, model_key: str, model_id: str | None = None) -> int:
        """Distinct texts this model has a vector for."""
        extra, params = ("", (model_key,)) if model_id is None else (" AND v.model_id = ?", (model_key, model_id))
        return self.store._conn.execute(
            "SELECT COUNT(DISTINCT k.content_hash) FROM knowledge_facts k JOIN knowledge_vectors v "
            f"ON v.content_hash = k.content_hash WHERE v.model_key = ?{extra}", params,
        ).fetchone()[0]

    def _start_state(self, provider: EmbeddingProvider) -> None:
        self.store._conn.execute(
            "INSERT INTO embedding_indexes(model_key, model_id, state, total, done, updated_at) "
            "VALUES (?, ?, 'running', ?, ?, ?) ON CONFLICT(model_key) DO UPDATE SET model_id=excluded.model_id, "
            "state='running', total=excluded.total, done=excluded.done, last_error='', updated_at=excluded.updated_at",
            (provider.model_key, provider.endpoint.model, self._distinct_texts(),
             self._done(provider.model_key, provider.endpoint.model), datetime.now(timezone.utc).isoformat()),
        )

    def _pending(self, model_key: str, limit: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store._conn.execute(
            "SELECT k.content_hash, k.text FROM knowledge_facts k WHERE NOT EXISTS "
            "(SELECT 1 FROM knowledge_vectors v WHERE v.model_key = ? AND v.content_hash = k.content_hash) "
            "GROUP BY k.content_hash ORDER BY MIN(k.id) LIMIT ?", (model_key, limit),
        )]

    def _stale(self, model_key: str) -> bool:
        row = self.store._conn.execute(
            "SELECT stale FROM embedding_indexes WHERE model_key=?", (model_key,),
        ).fetchone()
        return bool(row and row["stale"])

    def _ready(self, model_key: str, model_id: str) -> bool:
        row = self.store._conn.execute(
            "SELECT stale FROM embedding_indexes WHERE model_key=? AND model_id=?", (model_key, model_id),
        ).fetchone()
        if row is None or row["stale"]:
            return False
        return self._distinct_texts() == self._done(model_key, model_id)

    def _save_vectors(
        self, provider: EmbeddingProvider, facts: list[dict[str, Any]], vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        """Store what the batch returned for the texts that still exist; a text edited away while the call was in
        flight is skipped, never an error."""
        if len(facts) != len(vectors):
            raise ValueError("embedding provider returned the wrong batch size")
        with self.store._tx() as conn:
            for fact, vector in zip(facts, vectors):
                conn.execute(
                    "INSERT INTO knowledge_vectors(model_key, content_hash, model_id, dimensions, vector) "
                    "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM knowledge_facts WHERE content_hash = ?) "
                    "ON CONFLICT(model_key, content_hash) DO UPDATE SET model_id=excluded.model_id, "
                    "dimensions=excluded.dimensions, vector=excluded.vector",
                    (provider.model_key, fact["content_hash"], provider.endpoint.model, len(vector), _blob(vector),
                     fact["content_hash"]),
                )
            conn.execute(
                "UPDATE embedding_indexes SET total=?, done=?, updated_at=? WHERE model_key=?",
                (self._distinct_texts(), self._done(provider.model_key, provider.endpoint.model),
                 datetime.now(timezone.utc).isoformat(), provider.model_key),
            )

    def _save_probe(self, provider: EmbeddingProvider, vector: tuple[float, ...]) -> bool:
        """Compare the fixed probe with the FIRST one stored, never the latest, so slow drift adds up and is caught."""
        row = self.store._conn.execute(
            "SELECT probe, probe_dimensions FROM embedding_indexes WHERE model_key=?", (provider.model_key,),
        ).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        if row is not None and row["probe"] is not None and row["probe_dimensions"]:
            if _cosine_distance(_unblob(row["probe"], row["probe_dimensions"]), vector) > PROBE_DRIFT:
                self.store._conn.execute(
                    "UPDATE embedding_indexes SET stale=1, updated_at=? WHERE model_key=?", (now, provider.model_key),
                )
                return True
            return False
        self.store._conn.execute(
            "UPDATE embedding_indexes SET probe=?, probe_dimensions=?, stale=0, updated_at=? WHERE model_key=?",
            (_blob(vector), len(vector), now, provider.model_key),
        )
        return False

    def _finish_state(self, key: str, state: str, error: str) -> None:
        self.store._conn.execute(
            "UPDATE embedding_indexes SET state=?, last_error=?, updated_at=? WHERE model_key=?",
            (state, error[:200], datetime.now(timezone.utc).isoformat(), key),
        )

    def _reset_model(self, provider: EmbeddingProvider) -> None:
        with self.store._tx() as conn:
            conn.execute("DELETE FROM knowledge_vectors WHERE model_key=?", (provider.model_key,))
            conn.execute(
                "INSERT INTO embedding_indexes(model_key, model_id, state, total, done, stale, active, updated_at) "
                "VALUES (?, ?, 'idle', 0, 0, 0, 0, ?) ON CONFLICT(model_key) DO UPDATE SET model_id=excluded.model_id, "
                "state='idle', total=0, done=0, last_error='', probe=NULL, probe_dimensions=NULL, stale=0, active=0, "
                "updated_at=excluded.updated_at",
                (provider.model_key, provider.endpoint.model, datetime.now(timezone.utc).isoformat()),
            )

    def _activate(self, keys: list[str], exclusive: bool) -> None:
        """Flag complete indexes as the ones that serve. `exclusive` is a confirmed switch: no other index serves."""
        with self.store._tx() as conn:
            if exclusive:
                conn.execute("UPDATE embedding_indexes SET active=0")
            for key in keys:
                conn.execute("UPDATE embedding_indexes SET active=1 WHERE model_key=? AND stale=0", (key,))

    def _drop_models(self, keys: list[str]) -> None:
        with self.store._tx() as conn:
            for key in keys:
                conn.execute("DELETE FROM knowledge_vectors WHERE model_key=?", (key,))
                conn.execute("DELETE FROM embedding_indexes WHERE model_key=?", (key,))

    def _drop_other_models(self, keep: list[str]) -> None:
        """After a confirmed switch only the new route's indexes remain."""
        marks = ",".join("?" * len(keep))
        with self.store._tx() as conn:
            conn.execute(f"DELETE FROM knowledge_vectors WHERE model_key NOT IN ({marks})", keep)
            conn.execute(f"DELETE FROM embedding_indexes WHERE model_key NOT IN ({marks})", keep)

    def _progress(self, key: str, model: str) -> dict[str, Any]:
        row = self.store._conn.execute(
            "SELECT model_id, state, total, done, last_error, stale FROM embedding_indexes WHERE model_key=?", (key,),
        ).fetchone()
        if row is None:
            return {"done": 0, "left": self._distinct_texts(), "model": model, "state": "idle", "last_error": "", "stale": False}
        return {
            "done": row["done"], "left": max(0, row["total"] - row["done"]), "model": row["model_id"],
            "state": row["state"], "last_error": row["last_error"], "stale": bool(row["stale"]),
        }


async def _allowed() -> bool:
    return True

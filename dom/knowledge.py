"""Rebuildable entity graph and hybrid meaning index over messages and Markdown notes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from dom import notes
from dom.embedding import EmbeddingProvider, EmbeddingRouter
from dom.home import Home
from dom.store import Store
from dom.store.store import recency_weight
from dom.usage import ModelCallSkipped, model_request

log = logging.getLogger("dom.knowledge")
BATCH_SIZE = 16
PROBE_TEXT = "Dom remembers a quiet afternoon and the promise attached to it."
PROBE_DRIFT = 0.08
MAX_RESULTS = 30
_AS_OF = re.compile(r"\(as of (\d{4}-\d{2})(?:-\d{2})?\)")
_UNTIL = re.compile(r"\(until (\d{4}-\d{2}-\d{2})\)")


@dataclass(frozen=True)
class KnowledgeHit:
    text: str
    file: str
    source_chat: str | None
    at: str
    entities: tuple[str, ...]
    score: float = 0.0


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


class KnowledgeIndex:
    def __init__(
        self,
        store: Store,
        home: Home,
        router: EmbeddingRouter | None = None,
        *,
        allowed: Callable[[], Awaitable[bool]] | None = None,
        chat_id: int | None = None,
    ) -> None:
        self.store = store
        self.home = home
        self.router = router
        self.staged_router: EmbeddingRouter | None = None
        self.allowed = allowed or _allowed
        self.chat_id = chat_id
        self._indexing_keys: set[str] = set()

    # ---- sources and graph -------------------------------------------------

    def _note_paths(self) -> list[str]:
        people, chats = self.home.list_memory_notes()
        public = ["memory/MEMORY.md", *people, *chats, *self.home.list_entity_notes()]
        private = ["private/USER.md", "private/NOTES.md"]
        return [rel for rel in (*public, *private) if (self.home.root / rel).is_file()]

    async def refresh(self, *, full: bool = False) -> None:
        """Incrementally replace changed note files, then mirror changed messages. No model call."""
        paths = self._note_paths()
        known = await self.store._run(self._known_files)
        for rel in paths:
            try:
                text = self.home.scoped(rel.startswith("private/")).read(rel)
            except (OSError, ValueError, PermissionError):
                continue
            fingerprint = _hash(text)
            if full or known.get(rel) != fingerprint:
                facts = self._facts(rel, text)
                await self.store._run(self._replace_file, rel, fingerprint, facts)
        missing = set(known) - set(paths)
        if missing:
            await self.store._run(self._drop_files, missing)
        await self.store._run(self._sync_messages)

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
        for number, raw in enumerate(text.splitlines(), 1):
            if not notes.is_bullet(raw) or notes.blank_body(raw):
                continue
            line = notes.parse_line(raw)
            body = line.body.strip()
            entities = list(line.entities)
            if inferred := _file_entity(rel):
                entities.append(inferred)
            source_chat = None if line.source in (None, notes.ANY) else line.source
            if source_chat is not None:
                entities.append(f"chat:{source_chat}")
            valid_from = (_AS_OF.search(body).group(1) if _AS_OF.search(body) else "")
            valid_until = (_UNTIL.search(body).group(1) if _UNTIL.search(body) else "")
            found.append({
                "source_key": f"note:{rel}:{number}", "file": rel, "line_number": number,
                "text": body, "source_chat": source_chat, "private": int(rel.startswith("private/")),
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
            self._cleanup_entities(conn)

    def _drop_files(self, paths: set[str]) -> None:
        with self.store._tx() as conn:
            for rel in paths:
                conn.execute("DELETE FROM knowledge_facts WHERE source_kind = 'note' AND file = ?", (rel,))
                conn.execute("DELETE FROM knowledge_files WHERE path = ?", (rel,))
            self._cleanup_entities(conn)

    @staticmethod
    def _cleanup_entities(conn) -> None:
        conn.execute(
            "DELETE FROM knowledge_entities WHERE NOT EXISTS "
            "(SELECT 1 FROM knowledge_fact_entities WHERE entity_id = knowledge_entities.id)"
        )

    @staticmethod
    def _insert_fact(conn, kind: str, fact: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO knowledge_facts(source_key, source_kind, file, line_number, text, source_chat, private, "
            "at, valid_from, valid_until, pinned, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fact["source_key"], kind, fact.get("file", ""), fact.get("line_number"), fact["text"],
             fact.get("source_chat"), fact.get("private", 0), fact.get("at", ""), fact.get("valid_from", ""),
             fact.get("valid_until", ""), fact.get("pinned", 0), fact["content_hash"]),
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

    def _sync_messages(self) -> None:
        rows = self.store._conn.execute(
            "SELECT id, chat_id, visibility_scope, text, created_at FROM messages "
            "WHERE is_deleted = 0 AND role != 'event' AND trim(text) != ''"
        ).fetchall()
        live = {f"message:{row['id']}" for row in rows}
        with self.store._tx() as conn:
            existing = {row[0] for row in conn.execute(
                "SELECT source_key FROM knowledge_facts WHERE source_kind = 'message'"
            )}
            for source_key in existing - live:
                conn.execute("DELETE FROM knowledge_facts WHERE source_key = ?", (source_key,))
            for row in rows:
                source_key = f"message:{row['id']}"
                content_hash = _hash(row["text"])
                old = conn.execute(
                    "SELECT id, content_hash, source_chat, private, at FROM knowledge_facts WHERE source_key = ?",
                    (source_key,),
                ).fetchone()
                private = int(row["visibility_scope"] in ("owner_private", "admin_private"))
                if old is not None and (
                    old["content_hash"], old["source_chat"], old["private"], old["at"]
                ) == (content_hash, row["chat_id"], private, row["created_at"]):
                    continue
                if old is not None:
                    conn.execute("DELETE FROM knowledge_facts WHERE id = ?", (old["id"],))
                fact_id = self._insert_fact(conn, "message", {
                    "source_key": source_key, "text": row["text"], "source_chat": row["chat_id"],
                    "private": private, "at": row["created_at"], "content_hash": content_hash,
                })
                self._link_entities(conn, fact_id, (f"chat:{row['chat_id']}",))
            self._cleanup_entities(conn)

    def _counts(self) -> dict[str, int]:
        return {
            "files": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_files").fetchone()[0],
            "facts": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0],
            "entities": self.store._conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0],
        }

    # ---- retrieval --------------------------------------------------------

    async def search(
        self, query: str, *, requester_chat_id: int | str, owner: bool = False,
        mode: str = "keyword", limit: int = 10,
    ) -> list[KnowledgeHit]:
        query = " ".join(query.split())[:200]
        limit = max(1, min(limit, 30))
        if not query:
            return []
        if mode == "entity":
            return await self.store._run(self._entity_search, query, str(requester_chat_id), owner, limit)
        keyword = await self.store._run(self._keyword_rows, query, str(requester_chat_id), owner, MAX_RESULTS)
        if mode != "meaning" or self.router is None:
            return self._hits(keyword[:limit])
        semantic = await self._meaning_rows(query, str(requester_chat_id), owner)
        if not semantic:
            log.info("meaning search fell back to keywords")
            return self._hits(keyword[:limit])
        return self._blend(keyword, semantic, limit)

    async def _meaning_rows(self, query: str, chat_id: str, owner: bool) -> list[dict[str, Any]]:
        assert self.router is not None
        for index, provider in enumerate(self.router.providers):
            try:
                with model_request("embedding", self.chat_id, self.allowed):
                    batch = await provider.embed([query], fallback=index > 0)
                rows = await self.store._run(
                    self._vector_rows, batch.model_key, batch.model_id, batch.vectors[0],
                    chat_id, owner, MAX_RESULTS,
                )
                if rows:
                    return rows
            except (ModelCallSkipped, httpx.HTTPError, ValueError, RuntimeError):
                log.info("embedding query provider unavailable", exc_info=True)
        return []

    @staticmethod
    def _visibility(owner: bool) -> tuple[str, tuple[Any, ...]]:
        return ("1=1", ()) if owner else ("k.private = 0 AND (k.source_chat IS NULL OR k.source_chat = ?)", ())

    def _keyword_rows(self, query: str, chat_id: str, owner: bool, limit: int) -> list[dict[str, Any]]:
        match = _fts_query(query)
        if not match:
            return []
        scope = "1=1" if owner else "k.private = 0 AND (k.source_chat IS NULL OR k.source_chat = ?)"
        params: list[Any] = [match]
        if not owner:
            params.append(chat_id)
        params.append(limit)
        rows = self.store._conn.execute(
            "SELECT k.*, bm25(knowledge_facts_fts) AS distance FROM knowledge_facts_fts "
            "JOIN knowledge_facts k ON k.id = knowledge_facts_fts.rowid "
            f"WHERE knowledge_facts_fts MATCH ? AND {scope} ORDER BY distance LIMIT ?", params,
        ).fetchall()
        return [self._row(row) for row in rows]

    def _vector_rows(
        self, model_key: str, model_id: str, vector: tuple[float, ...], chat_id: str, owner: bool, limit: int,
    ) -> list[dict[str, Any]]:
        state = self.store._conn.execute(
            "SELECT stale FROM embedding_indexes WHERE model_key = ?", (model_key,),
        ).fetchone()
        if state is not None and state["stale"]:
            return []
        scope = "1=1" if owner else "k.private = 0 AND (k.source_chat IS NULL OR k.source_chat = ?)"
        rows = self.store._conn.execute(
            "SELECT k.*, vec_distance_cosine(v.vector, ?) AS distance FROM knowledge_vectors v "
            "JOIN knowledge_facts k ON k.id = v.fact_id "
            "WHERE v.model_key = ? AND v.model_id = ? AND v.dimensions = ? "
            "AND v.content_hash = k.content_hash AND " + scope + " ORDER BY distance LIMIT ?",
            [_blob(vector), model_key, model_id, len(vector), *( [] if owner else [chat_id]), limit],
        ).fetchall()
        return [self._row(row) for row in rows]

    def _entity_search(self, query: str, chat_id: str, owner: bool, limit: int) -> list[KnowledgeHit]:
        needle = query.strip().lower().replace(" ", "-")
        entities = self.store._conn.execute(
            "SELECT id, kind, entity_key FROM knowledge_entities "
            "WHERE lower(label) LIKE ? OR lower(entity_key) = ? ORDER BY kind, entity_key LIMIT 8",
            (f"%{query.strip().lower()}%", needle),
        ).fetchall()
        if not entities:
            return []
        marks = ",".join("?" * len(entities))
        scope = "1=1" if owner else "k.private = 0 AND (k.source_chat IS NULL OR k.source_chat = ?)"
        params: list[Any] = [*(row["id"] for row in entities)]
        if not owner:
            params.append(chat_id)
        params.append(limit)
        rows = self.store._conn.execute(
            "SELECT DISTINCT k.*, 0.0 AS distance FROM knowledge_facts k "
            "JOIN knowledge_fact_entities fe ON fe.fact_id = k.id "
            f"WHERE fe.entity_id IN ({marks}) AND {scope} ORDER BY k.pinned DESC, k.at DESC LIMIT ?", params,
        ).fetchall()
        return self._hits([self._row(row) for row in rows])

    async def related(
        self, entity: str, *, requester_chat_id: int | str, owner: bool = False, limit: int = 20,
    ) -> list[str]:
        """Entities co-mentioned with ``entity``, after the same code-enforced visibility filter."""
        return await self.store._run(self._related, entity, str(requester_chat_id), owner, limit)

    def _related(self, entity: str, chat_id: str, owner: bool, limit: int) -> list[str]:
        kind, _, key = entity.partition(":")
        scope = "1=1" if owner else "k.private = 0 AND (k.source_chat IS NULL OR k.source_chat = ?)"
        params: list[Any] = [kind, key]
        if not owner:
            params.append(chat_id)
        params.append(max(1, min(limit, 50)))
        rows = self.store._conn.execute(
            "SELECT DISTINCT other.kind || ':' || other.entity_key AS ref FROM knowledge_entities target "
            "JOIN knowledge_fact_entities tf ON tf.entity_id = target.id "
            "JOIN knowledge_facts k ON k.id = tf.fact_id "
            "JOIN knowledge_fact_entities ofe ON ofe.fact_id = k.id "
            "JOIN knowledge_entities other ON other.id = ofe.entity_id "
            f"WHERE target.kind = ? AND target.entity_key = ? AND other.id != target.id AND {scope} "
            "ORDER BY ref LIMIT ?", params,
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
            row["text"], row["file"], row["source_chat"], row["at"], row["entities"],
            1.0 / (1.0 + max(0.0, float(row.get("distance", 0.0)))),
        ) for row in rows]

    def _blend(
        self, keyword: list[dict[str, Any]], semantic: list[dict[str, Any]], limit: int,
    ) -> list[KnowledgeHit]:
        by_id: dict[int, dict[str, Any]] = {}
        scores: dict[int, float] = {}
        for rank, row in enumerate(keyword):
            by_id[row["id"]] = row
            scores[row["id"]] = scores.get(row["id"], 0.0) + 0.45 / (rank + 1)
        for rank, row in enumerate(semantic):
            by_id[row["id"]] = row
            similarity = max(0.0, 1.0 - float(row["distance"]))
            scores[row["id"]] = scores.get(row["id"], 0.0) + 0.55 * similarity / (rank + 1) ** 0.25
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
        ranked = sorted(by_id, key=lambda fact_id: scores[fact_id], reverse=True)[:limit]
        return [KnowledgeHit(
            by_id[fact_id]["text"], by_id[fact_id]["file"], by_id[fact_id]["source_chat"],
            by_id[fact_id]["at"], by_id[fact_id]["entities"], scores[fact_id],
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
        await self.refresh()
        if self.router is None:
            return
        for provider in self.router.providers:
            if provider.available:
                await self._index_provider(provider, rebuild=False)

    async def estimate(self) -> dict[str, Any]:
        router = self.staged_router
        total = await self.store._run(lambda: self.store._conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts"
        ).fetchone()[0])
        model = router.providers[0].endpoint.model if router else ""
        return {"items": total, "rough_seconds": round(total * 0.55, 1), "model": model, "staged": router is not None}

    async def reindex(self) -> bool:
        router = self.staged_router
        if router is None:
            return False
        await self.refresh()
        current_keys = {provider.model_key for provider in self.router.providers} if self.router else set()
        completed: list[EmbeddingProvider] = []
        for provider in router.providers:
            if not provider.available:
                continue
            if provider.model_key not in current_keys:
                await self.store._run(self._reset_model, provider)
                rebuild = True
            else:
                rebuild = False
            if await self._index_provider(provider, rebuild=rebuild) and await self.store._run(
                self._ready, provider.model_key, provider.endpoint.model,
            ):
                completed.append(provider)
        if not completed:
            return False
        await self.store._run(self._activate, completed[0].model_key)
        self.router, self.staged_router = router, None
        return True

    async def cancel(self) -> None:
        router = self.staged_router
        if router is None:
            return
        await self.store._run(self._request_cancel, [provider.model_key for provider in router.providers])

    async def progress(self) -> dict[str, Any]:
        key = None
        if self.staged_router is not None:
            key = self.staged_router.providers[0].model_key
        elif self.router is not None:
            key = self.router.providers[0].model_key
        if key is None:
            total = await self.store._run(lambda: self.store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_facts"
            ).fetchone()[0])
            return {"done": 0, "left": total, "model": "", "state": "idle", "last_error": "", "stale": False}
        router = self.staged_router or self.router
        model = router.providers[0].endpoint.model if router is not None else ""
        return await self.store._run(self._progress, key, model)

    async def _index_provider(self, provider: EmbeddingProvider, *, rebuild: bool) -> bool:
        if provider.model_key in self._indexing_keys:
            return False
        self._indexing_keys.add(provider.model_key)
        try:
            await self.store._run(self._start_state, provider)
            if not rebuild and await self.store._run(self._stale, provider.model_key):
                await self.store._run(self._finish_state, provider.model_key, "paused", "probe drift")
                return False
            first = True
            while True:
                if await self.store._run(self._cancelled, provider.model_key):
                    await self.store._run(self._finish_state, provider.model_key, "idle", "")
                    return False
                pending = await self.store._run(self._pending, provider.model_key, BATCH_SIZE)
                if not pending:
                    await self.store._run(self._finish_state, provider.model_key, "idle", "")
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
                    await self.store._run(self._finish_state, provider.model_key, "paused", error)
                    return False
                offset = 0
                if first:
                    drifted = await self.store._run(self._save_probe, provider, batch.vectors[0])
                    if drifted and not rebuild:
                        await self.store._run(self._finish_state, provider.model_key, "paused", "probe drift")
                        return False
                    offset = 1
                    first = False
                await self.store._run(self._save_vectors, provider, pending, batch.vectors[offset:])
        finally:
            self._indexing_keys.discard(provider.model_key)

    def _start_state(self, provider: EmbeddingProvider) -> None:
        total = self.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0]
        done = self.store._conn.execute(
            "SELECT COUNT(*) FROM knowledge_vectors v JOIN knowledge_facts k ON k.id = v.fact_id "
            "WHERE v.model_key = ? AND v.model_id = ? AND v.content_hash = k.content_hash",
            (provider.model_key, provider.endpoint.model),
        ).fetchone()[0]
        self.store._conn.execute(
            "INSERT INTO embedding_indexes(model_key, model_id, state, total, done, updated_at) "
            "VALUES (?, ?, 'running', ?, ?, ?) ON CONFLICT(model_key) DO UPDATE SET model_id=excluded.model_id, "
            "state='running', total=excluded.total, done=excluded.done, last_error='', cancel_requested=0, updated_at=excluded.updated_at",
            (provider.model_key, provider.endpoint.model, total, done, datetime.now(timezone.utc).isoformat()),
        )

    def _pending(self, model_key: str, limit: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store._conn.execute(
            "SELECT k.id, k.text, k.content_hash FROM knowledge_facts k LEFT JOIN knowledge_vectors v "
            "ON v.fact_id = k.id AND v.model_key = ? WHERE v.fact_id IS NULL OR v.content_hash != k.content_hash "
            "ORDER BY k.id LIMIT ?", (model_key, limit),
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
        facts = self.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0]
        vectors = self.store._conn.execute(
            "SELECT COUNT(*) FROM knowledge_vectors v JOIN knowledge_facts k ON k.id=v.fact_id "
            "WHERE v.model_key=? AND v.model_id=? AND v.content_hash=k.content_hash", (model_key, model_id),
        ).fetchone()[0]
        return facts == vectors

    def _save_vectors(
        self, provider: EmbeddingProvider, facts: list[dict[str, Any]], vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        if len(facts) != len(vectors):
            raise ValueError("embedding provider returned the wrong batch size")
        with self.store._tx() as conn:
            for fact, vector in zip(facts, vectors):
                conn.execute(
                    "INSERT INTO knowledge_vectors(fact_id, model_key, model_id, dimensions, vector, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(fact_id, model_key) DO UPDATE SET model_id=excluded.model_id, "
                    "dimensions=excluded.dimensions, vector=excluded.vector, content_hash=excluded.content_hash",
                    (fact["id"], provider.model_key, provider.endpoint.model, len(vector), _blob(vector), fact["content_hash"]),
                )
            done = conn.execute(
                "SELECT COUNT(*) FROM knowledge_vectors v JOIN knowledge_facts k ON k.id=v.fact_id "
                "WHERE v.model_key=? AND v.content_hash=k.content_hash", (provider.model_key,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE embedding_indexes SET done=?, updated_at=? WHERE model_key=?",
                (done, datetime.now(timezone.utc).isoformat(), provider.model_key),
            )

    def _save_probe(self, provider: EmbeddingProvider, vector: tuple[float, ...]) -> bool:
        row = self.store._conn.execute(
            "SELECT probe, probe_dimensions FROM embedding_indexes WHERE model_key=?", (provider.model_key,),
        ).fetchone()
        drifted = False
        if row is not None and row["probe"] is not None and row["probe_dimensions"]:
            old = _unblob(row["probe"], row["probe_dimensions"])
            drifted = _cosine_distance(old, vector) > PROBE_DRIFT
        if drifted:
            self.store._conn.execute(
                "UPDATE embedding_indexes SET stale=1, updated_at=? WHERE model_key=?",
                (datetime.now(timezone.utc).isoformat(), provider.model_key),
            )
            return True
        self.store._conn.execute(
            "UPDATE embedding_indexes SET probe=?, probe_dimensions=?, stale=0, updated_at=? WHERE model_key=?",
            (_blob(vector), len(vector), datetime.now(timezone.utc).isoformat(), provider.model_key),
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
                "INSERT INTO embedding_indexes(model_key, model_id, state, total, done, stale, active, cancel_requested, updated_at) "
                "VALUES (?, ?, 'idle', 0, 0, 0, 0, 0, ?) ON CONFLICT(model_key) DO UPDATE SET model_id=excluded.model_id, "
                "state='idle', total=0, done=0, last_error='', probe=NULL, probe_dimensions=NULL, stale=0, active=0, "
                "cancel_requested=0, updated_at=excluded.updated_at",
                (provider.model_key, provider.endpoint.model, datetime.now(timezone.utc).isoformat()),
            )

    def _activate(self, key: str) -> None:
        with self.store._tx() as conn:
            conn.execute("UPDATE embedding_indexes SET active=0")
            conn.execute("UPDATE embedding_indexes SET active=1 WHERE model_key=? AND stale=0", (key,))

    def _request_cancel(self, keys: list[str]) -> None:
        with self.store._tx() as conn:
            for key in keys:
                conn.execute("UPDATE embedding_indexes SET cancel_requested=1 WHERE model_key=?", (key,))

    def _cancelled(self, key: str) -> bool:
        row = self.store._conn.execute(
            "SELECT cancel_requested FROM embedding_indexes WHERE model_key=?", (key,),
        ).fetchone()
        return bool(row and row[0])

    def _progress(self, key: str, model: str) -> dict[str, Any]:
        row = self.store._conn.execute(
            "SELECT model_id, state, total, done, last_error, stale FROM embedding_indexes WHERE model_key=?", (key,),
        ).fetchone()
        if row is None:
            total = self.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0]
            return {"done": 0, "left": total, "model": model, "state": "idle", "last_error": "", "stale": False}
        return {
            "done": row["done"], "left": max(0, row["total"] - row["done"]), "model": row["model_id"],
            "state": row["state"], "last_error": row["last_error"], "stale": bool(row["stale"]),
        }


async def _allowed() -> bool:
    return True

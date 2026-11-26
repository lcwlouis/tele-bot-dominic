-- Rebuildable graph and meaning-search index. Markdown and messages remain the sources of truth.
CREATE TABLE knowledge_files (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE knowledge_facts (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('note', 'message')),
    file TEXT NOT NULL DEFAULT '',
    line_number INTEGER,
    text TEXT NOT NULL,
    source_chat TEXT,
    private INTEGER NOT NULL DEFAULT 0 CHECK(private IN (0, 1)),
    at TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
    content_hash TEXT NOT NULL,
    message_id INTEGER  -- messages.id for a message fact, NULL for a note line
);
CREATE INDEX idx_knowledge_facts_file ON knowledge_facts(file);
CREATE INDEX idx_knowledge_facts_hash ON knowledge_facts(content_hash);
CREATE INDEX idx_knowledge_facts_message ON knowledge_facts(message_id);
CREATE INDEX idx_knowledge_facts_scope ON knowledge_facts(private, source_chat);

CREATE VIRTUAL TABLE knowledge_facts_fts USING fts5(text, content='knowledge_facts', content_rowid='id');
CREATE TRIGGER knowledge_facts_ai AFTER INSERT ON knowledge_facts BEGIN
    INSERT INTO knowledge_facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER knowledge_facts_ad AFTER DELETE ON knowledge_facts BEGIN
    INSERT INTO knowledge_facts_fts(knowledge_facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER knowledge_facts_au AFTER UPDATE OF text ON knowledge_facts BEGIN
    INSERT INTO knowledge_facts_fts(knowledge_facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO knowledge_facts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE knowledge_entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('person', 'chat', 'place', 'project', 'goal')),
    entity_key TEXT NOT NULL,
    label TEXT NOT NULL,
    UNIQUE(kind, entity_key)
);
CREATE INDEX idx_knowledge_entities_label ON knowledge_entities(label);

CREATE TABLE knowledge_fact_entities (
    fact_id INTEGER NOT NULL REFERENCES knowledge_facts(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    PRIMARY KEY(fact_id, entity_id)
);
CREATE INDEX idx_knowledge_fact_entities_entity ON knowledge_fact_entities(entity_id, fact_id);

-- Vectors belong to a text (its hash), not to a fact row, so a line that survives an edit of its file keeps its
-- vector and is never embedded twice.
CREATE TABLE knowledge_vectors (
    model_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY(model_key, content_hash)
);

-- Where the incremental message sync stopped, and the messages edited since it read them.
CREATE TABLE knowledge_sync (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_message_id INTEGER NOT NULL DEFAULT 0,
    sweep_after_fact_id INTEGER NOT NULL DEFAULT 0,
    swept_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO knowledge_sync(id) VALUES (1);
CREATE TABLE knowledge_dirty_messages (
    message_id INTEGER PRIMARY KEY
);

CREATE TABLE embedding_indexes (
    model_key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'idle' CHECK(state IN ('running', 'paused', 'idle')),
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    probe BLOB,
    probe_dimensions INTEGER,
    stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
    updated_at TEXT NOT NULL
);

ALTER TABLE usage RENAME TO usage_before_embeddings;
CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'chat', 'triage', 'media', 'memory', 'search', 'reflection', 'guard', 'desktop', 'escalation',
        'studio_voice', 'studio_image', 'studio_video', 'embedding'
    )),
    model TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    chat_id INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1))
);
INSERT INTO usage SELECT * FROM usage_before_embeddings;
DROP TABLE usage_before_embeddings;
CREATE INDEX idx_usage_at ON usage(at);

PRAGMA user_version = 31;

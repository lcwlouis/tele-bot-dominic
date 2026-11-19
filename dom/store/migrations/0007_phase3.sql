CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE chats ADD COLUMN preset TEXT NOT NULL DEFAULT '';

-- Extend the usage kind constraint for Phase 3's paid calls.
ALTER TABLE usage RENAME TO usage_before_phase3;
CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('chat', 'triage', 'media', 'memory', 'search', 'reflection', 'guard')),
    model TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    chat_id INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1))
);
INSERT INTO usage SELECT * FROM usage_before_phase3;
DROP TABLE usage_before_phase3;
CREATE INDEX idx_usage_at ON usage(at);

PRAGMA user_version = 8;

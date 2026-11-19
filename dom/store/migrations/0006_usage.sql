CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('chat', 'triage', 'media', 'memory')),
    model TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    chat_id INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1))
);

CREATE INDEX idx_usage_at ON usage(at);

CREATE TABLE spend_cap_notices (
    day TEXT PRIMARY KEY
);

PRAGMA user_version = 7;

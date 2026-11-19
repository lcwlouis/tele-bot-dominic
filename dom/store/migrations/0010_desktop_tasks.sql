CREATE TABLE desktop_tasks (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'waiting_owner')),
    steps INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE UNIQUE INDEX idx_desktop_one_active ON desktop_tasks ((1))
WHERE status IN ('queued', 'running', 'waiting_owner');

ALTER TABLE usage RENAME TO usage_before_desktop;
CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('chat', 'triage', 'media', 'memory', 'search', 'reflection', 'guard', 'desktop')),
    model TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    chat_id INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1))
);
INSERT INTO usage SELECT * FROM usage_before_desktop;
DROP TABLE usage_before_desktop;
CREATE INDEX idx_usage_at ON usage(at);

PRAGMA user_version = 11;

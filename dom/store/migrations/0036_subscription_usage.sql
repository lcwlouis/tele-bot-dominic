-- Calls to the owner's subscription agents (ACP pilot) are attributed to their own usage kind: they are billed to a
-- plan, not per call, so they record cost 0. The kind list is a CHECK constraint, so the table is rebuilt with it,
-- keeping every row (and its `settled` flag) as it was.
ALTER TABLE usage RENAME TO usage_before_subscription;
CREATE TABLE usage (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'chat', 'triage', 'media', 'memory', 'search', 'reflection', 'guard', 'desktop', 'escalation',
        'studio_voice', 'studio_image', 'studio_video', 'embedding', 'assistant', 'subscription'
    )),
    model TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    chat_id INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1)),
    settled INTEGER NOT NULL DEFAULT 0
);
INSERT INTO usage (id, at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback, settled)
    SELECT id, at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback, settled
    FROM usage_before_subscription;
DROP TABLE usage_before_subscription;
CREATE INDEX idx_usage_at ON usage(at);

PRAGMA user_version = 37;

-- Self-directed work (W13): small tasks Dom plans in his nightly daydream. Each is announced to the owner
-- (DM and dashboard) before it may start. A new table only, so the previous release runs on this schema.
CREATE TABLE self_tasks (
    id INTEGER PRIMARY KEY,
    goal TEXT NOT NULL,
    why TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    sites_json TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    steps_budget INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned', 'running', 'done', 'failed', 'cancelled', 'refused', 'expired')),
    steps INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    files_json TEXT NOT NULL DEFAULT '[]',
    announced_at TEXT NOT NULL,
    not_before TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX idx_self_tasks_status ON self_tasks(status);

PRAGMA user_version = 16;

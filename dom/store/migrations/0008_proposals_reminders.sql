CREATE TABLE proposals (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('soul_edit', 'memory_fact', 'skill', 'plugin')),
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    source TEXT NOT NULL
);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    importance TEXT NOT NULL CHECK(importance IN ('critical', 'normal', 'casual')),
    repeat TEXT NOT NULL CHECK(repeat IN ('', 'daily', 'weekdays', 'weekly')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'done', 'cancelled')),
    created_at TEXT NOT NULL,
    fired_at TEXT
);

PRAGMA user_version = 9;

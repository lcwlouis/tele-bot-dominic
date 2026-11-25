-- Rebuild desktop_tasks and reminders with AUTOINCREMENT so deleted ids are never reused.
ALTER TABLE desktop_tasks RENAME TO desktop_tasks_before_autoincrement;
CREATE TABLE desktop_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'waiting_owner')),
    steps INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    container TEXT NOT NULL DEFAULT 'default',
    steps_budget INTEGER,
    extra_steps INTEGER NOT NULL DEFAULT 0
);
INSERT INTO desktop_tasks (
    id, chat_id, brief, status, steps, result, error, created_at, started_at, finished_at,
    container, steps_budget, extra_steps
)
SELECT
    id, chat_id, brief, status, steps, result, error, created_at, started_at, finished_at,
    container, steps_budget, extra_steps
FROM desktop_tasks_before_autoincrement;
DROP TABLE desktop_tasks_before_autoincrement;
CREATE UNIQUE INDEX idx_desktop_one_active_per_container ON desktop_tasks (container)
WHERE status IN ('queued', 'running', 'waiting_owner');

ALTER TABLE reminders RENAME TO reminders_before_autoincrement;
CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    importance TEXT NOT NULL CHECK(importance IN ('critical', 'normal', 'casual')),
    repeat TEXT NOT NULL CHECK(repeat IN ('', 'daily', 'weekdays', 'weekly')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'done', 'cancelled')),
    created_at TEXT NOT NULL,
    fired_at TEXT,
    undeliverable INTEGER NOT NULL DEFAULT 0
);
INSERT INTO reminders (
    id, chat_id, text, due_at, importance, repeat, status, created_at, fired_at, undeliverable
)
SELECT
    id, chat_id, text, due_at, importance, repeat, status, created_at, fired_at, undeliverable
FROM reminders_before_autoincrement;
DROP TABLE reminders_before_autoincrement;

DELETE FROM sqlite_sequence WHERE name IN ('desktop_tasks', 'reminders');
INSERT INTO sqlite_sequence (name, seq)
SELECT 'desktop_tasks', COALESCE(MAX(id), 0) FROM desktop_tasks;
INSERT INTO sqlite_sequence (name, seq)
SELECT 'reminders', COALESCE(MAX(id), 0) FROM reminders;

PRAGMA user_version = 30;

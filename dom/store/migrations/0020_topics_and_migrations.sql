-- 0020_topics_and_migrations: remember forum topic thread ids per message,
-- keep migrated group chat aliases, and mark undeliverable critical reminders.

ALTER TABLE messages ADD COLUMN thread_id INTEGER;
ALTER TABLE reminders ADD COLUMN undeliverable INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS chat_migrations (
    old_chat_id TEXT PRIMARY KEY,
    new_chat_id TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);

PRAGMA user_version = 21;

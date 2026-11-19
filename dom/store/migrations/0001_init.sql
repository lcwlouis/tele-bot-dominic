-- 0001_init: baseline Dominic V2 schema (matches the schema written by scripts/migrate_v1_to_v2.py).
-- Idempotent (IF NOT EXISTS) so it is safe on a DB already created by the V1->V2 migration script.

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    chat_type TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    is_sleeping INTEGER NOT NULL DEFAULT 0,
    sleep_until TEXT,
    is_offline INTEGER NOT NULL DEFAULT 0,
    offline_until TEXT,
    online_until TEXT,
    last_read_msg_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people_identities (
    person_key TEXT PRIMARY KEY,
    telegram_user_id INTEGER UNIQUE,
    current_handle TEXT,
    known_handles_json TEXT NOT NULL DEFAULT '[]',
    display_name TEXT,
    preferred_name TEXT,
    profile_path TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_people_handle
    ON people_identities(current_handle);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('heartbeat', 'reminder', 'cron')),
    importance TEXT NOT NULL CHECK(importance IN ('critical', 'normal', 'casual')),
    target_chat_id TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    schedule_expr TEXT NOT NULL,
    jitter_seconds INTEGER NOT NULL DEFAULT 0,
    bypass_sleep INTEGER NOT NULL DEFAULT 0,
    prompt_or_script TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    msg_id INTEGER,
    reply_to_msg_id INTEGER,
    sender_handle TEXT,
    sender_name TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    visibility_scope TEXT NOT NULL,
    text TEXT NOT NULL,
    media_json TEXT,
    is_edited INTEGER NOT NULL DEFAULT 0,
    edit_history_json TEXT NOT NULL DEFAULT '[]',
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_msg_id
    ON messages(chat_id, msg_id);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created
    ON messages(chat_id, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    chat_id UNINDEXED,
    visibility_scope UNINDEXED,
    sender_handle,
    sender_name,
    text,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, chat_id, visibility_scope, sender_handle, sender_name, text)
    VALUES (new.id, new.chat_id, new.visibility_scope, new.sender_handle, new.sender_name, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, chat_id, visibility_scope, sender_handle, sender_name, text)
    VALUES ('delete', old.id, old.chat_id, old.visibility_scope, old.sender_handle, old.sender_name, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, chat_id, visibility_scope, sender_handle, sender_name, text)
    VALUES ('delete', old.id, old.chat_id, old.visibility_scope, old.sender_handle, old.sender_name, old.text);
    INSERT INTO messages_fts(rowid, chat_id, visibility_scope, sender_handle, sender_name, text)
    SELECT new.id, new.chat_id, new.visibility_scope, new.sender_handle, new.sender_name, new.text
    WHERE new.is_deleted = 0;
END;

PRAGMA user_version = 2;

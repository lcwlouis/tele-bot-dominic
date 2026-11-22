-- 0021_topic_reads_and_cooldowns: per-topic unread/catch-up tracking for forum chats,
-- and persisted group no-answer cooldown across restarts.

ALTER TABLE chats ADD COLUMN group_no_answer_at TEXT;

CREATE TABLE IF NOT EXISTS chat_topic_reads (
    chat_id TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    last_read_msg_id INTEGER NOT NULL DEFAULT 0,
    catch_up_pending INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

INSERT OR IGNORE INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at)
SELECT chat_id, 0, last_read_msg_id, updated_at
FROM chats
WHERE last_read_msg_id IS NOT NULL;

INSERT OR IGNORE INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at)
SELECT DISTINCT m.chat_id, m.thread_id, c.last_read_msg_id, c.updated_at
FROM messages m
JOIN chats c ON c.chat_id = m.chat_id
WHERE m.thread_id IS NOT NULL AND c.last_read_msg_id IS NOT NULL;

PRAGMA user_version = 22;

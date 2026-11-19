-- 0005_memory_cursor: how far the background memory pass has read each chat (a Telegram msg_id).

ALTER TABLE chats ADD COLUMN memory_cursor_msg_id INTEGER NOT NULL DEFAULT 0;

PRAGMA user_version = 6;

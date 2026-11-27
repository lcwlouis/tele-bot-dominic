-- A new bot token restarts Telegram's message ids in every chat. `era_row` is the first messages.id of a chat's current
-- run of ids: rows before it belong to an earlier bot and are never unread or memorised. 0 means the chat has had one
-- bot only. Dom fills it in at start (from a drop in a chat's ids, or a changed bot id) and when such a drop arrives.
ALTER TABLE chats ADD COLUMN era_row INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_messages_chat_row ON messages(chat_id, id);

PRAGMA user_version = 37;

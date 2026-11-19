ALTER TABLE chats ADD COLUMN authorization_status TEXT NOT NULL DEFAULT 'pending_approval';
ALTER TABLE chats ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE chats ADD COLUMN invited_by_handle TEXT NOT NULL DEFAULT '';
ALTER TABLE chats ADD COLUMN approval_prompt_sent INTEGER NOT NULL DEFAULT 0;

-- Soft-deleted messages have already been removed from FTS.
DROP TRIGGER IF EXISTS messages_ad;
CREATE TRIGGER messages_ad AFTER DELETE ON messages WHEN old.is_deleted = 0 BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, chat_id, visibility_scope, sender_handle, sender_name, text)
    VALUES ('delete', old.id, old.chat_id, old.visibility_scope, old.sender_handle, old.sender_name, old.text);
END;

-- Preserve deletions and edits when an earlier replay created duplicate rows.
DELETE FROM messages WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY chat_id, msg_id ORDER BY is_deleted DESC, is_edited DESC, id DESC
        ) AS duplicate_rank
        FROM messages
        WHERE msg_id IS NOT NULL
    )
    WHERE duplicate_rank > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_msg_unique
    ON messages(chat_id, msg_id) WHERE msg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS pending_turns (
    chat_id TEXT PRIMARY KEY,
    first_queued_msg_id INTEGER,
    last_queued_msg_id INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

PRAGMA user_version = 4;

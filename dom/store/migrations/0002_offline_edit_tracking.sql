-- 0002_offline_edit_tracking: offline-aware edit tracking.
-- The migration runner skips an ADD COLUMN whose column already exists, so this file is idempotent.

ALTER TABLE messages ADD COLUMN sender_user_id INTEGER;
ALTER TABLE messages ADD COLUMN original_seen_before_edit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN previous_seen_text TEXT;

PRAGMA user_version = 3;

-- Track whether the current bot can still reach a chat (E74), keeping its prior authorization_status for restore,
-- and whether the owner hid an unreachable chat from lists.
ALTER TABLE chats ADD COLUMN reachable INTEGER NOT NULL DEFAULT 1;
ALTER TABLE chats ADD COLUMN unreachable_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE chats ADD COLUMN unreachable_at TEXT;
ALTER TABLE chats ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;

PRAGMA user_version = 27;

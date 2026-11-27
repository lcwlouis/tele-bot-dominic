-- A desktop task that a group or a friend asked for (and the owner allowed): which chat, and who asked.
-- The row is still an owner desktop task (the Work page, cancel and delete work as they do), and these columns mark it.
ALTER TABLE desktop_tasks ADD COLUMN guest_chat_id INTEGER;
ALTER TABLE desktop_tasks ADD COLUMN guest_chat_title TEXT;
ALTER TABLE desktop_tasks ADD COLUMN guest_asker TEXT;
PRAGMA user_version = 41;

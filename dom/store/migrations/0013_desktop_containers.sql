-- A desktop task belongs to a named container. Existing rows keep running on the one container that
-- existed before, and the one-active-task limit now applies to each container on its own.
ALTER TABLE desktop_tasks ADD COLUMN container TEXT NOT NULL DEFAULT 'default';

DROP INDEX idx_desktop_one_active;
CREATE UNIQUE INDEX idx_desktop_one_active_per_container ON desktop_tasks (container)
WHERE status IN ('queued', 'running', 'waiting_owner');

PRAGMA user_version = 14;

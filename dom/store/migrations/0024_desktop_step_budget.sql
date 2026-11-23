-- A desktop task's step budget is recorded on the task: the budget it started with and the steps the owner added
-- when it was about to run out ("Keep going"). Older rows keep NULL and 0: they ran on the fixed budget of 30.
ALTER TABLE desktop_tasks ADD COLUMN steps_budget INTEGER;
ALTER TABLE desktop_tasks ADD COLUMN extra_steps INTEGER NOT NULL DEFAULT 0;

PRAGMA user_version = 25;

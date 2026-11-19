-- 0004_dom_core: per-chat personality overrides and the bot-to-bot turn counter.

ALTER TABLE chats ADD COLUMN traits_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE chats ADD COLUMN bot_depth INTEGER NOT NULL DEFAULT 0;

PRAGMA user_version = 5;

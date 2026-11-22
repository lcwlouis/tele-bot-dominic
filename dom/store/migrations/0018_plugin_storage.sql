CREATE TABLE IF NOT EXISTS plugin_storage (
    name TEXT PRIMARY KEY REFERENCES plugins(name) ON DELETE CASCADE,
    value_json TEXT NOT NULL CHECK(json_valid(value_json) AND length(CAST(value_json AS BLOB)) <= 65536)
);

ALTER TABLE plugins ADD COLUMN test_run_json TEXT;

PRAGMA user_version = 19;

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS records (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(kind, id)
);
CREATE INDEX IF NOT EXISTS records_kind_sequence ON records(kind, sequence);
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'append_only'); END;

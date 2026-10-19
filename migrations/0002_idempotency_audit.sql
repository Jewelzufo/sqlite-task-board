BEGIN;

CREATE TABLE IF NOT EXISTS idempotency_ledger (
  key           TEXT PRIMARY KEY,
  action_type   TEXT NOT NULL,
  payload_hash  TEXT NOT NULL,
  task_id       INTEGER NOT NULL,
  result_json   TEXT NOT NULL,
  completed_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp    TEXT NOT NULL,
  event        TEXT NOT NULL,
  task_id      INTEGER,
  payload_json TEXT NOT NULL,
  prev_hash    TEXT NOT NULL,
  entry_hash   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_event_timestamp ON audit_log(event, timestamp);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (2, 'Idempotency ledger and hash-chained audit log');

COMMIT;

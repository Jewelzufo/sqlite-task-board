BEGIN;

CREATE TABLE IF NOT EXISTS tasks (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  instruction      TEXT NOT NULL,
  action_type      TEXT NOT NULL,
  idempotency_key  TEXT UNIQUE,
  checksum         TEXT,
  status           TEXT NOT NULL DEFAULT 'pending',
  priority         TEXT DEFAULT 'medium',
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at       TIMESTAMP,
  completed_at     TIMESTAMP,
  failed_at        TIMESTAMP,
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  attempt_count    INTEGER DEFAULT 0,
  max_attempts     INTEGER DEFAULT 3,
  error_code       TEXT,
  error_message    TEXT,
  last_error       TEXT,
  CONSTRAINT status_valid CHECK (status IN ('pending', 'running', 'completed', 'failed', 'dead-lettered')),
  CONSTRAINT priority_valid CHECK (priority IN ('critical', 'high', 'medium', 'low'))
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_generation_log (
  generation_date TEXT NOT NULL,
  generated_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (generation_date)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority_status ON tasks(priority, status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Initial schema: autonomous SQLite task board with retry tracking and bootstrap support');

COMMIT;

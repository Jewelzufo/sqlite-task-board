-- 0001_initial.sql
-- SQLite Task Board - Initial Schema
-- Creates core tables for task queue

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'completed', 'dead-lettered')),
    priority TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('critical', 'high', 'medium', 'low')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT
);

-- Index for efficient queue polling (status + priority ordering)
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

-- Audit table for task execution history (optional, for observability)
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,
    output TEXT,
    error TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);

-- Ensure payload is valid JSON (SQLite 3.38+)
-- Comment out if using older SQLite
-- CREATE TRIGGER IF NOT EXISTS tasks_payload_json
-- BEFORE INSERT ON tasks
-- BEGIN
-- SELECT CASE WHEN json_valid(NEW.payload) = 0 THEN RAISE(ABORT, 'payload must be valid JSON') END;
-- END;

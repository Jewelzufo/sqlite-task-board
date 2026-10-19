BEGIN;

ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER REFERENCES tasks(id);
ALTER TABLE tasks ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (3, 'Task lineage: parent_task_id and depth');

COMMIT;

BEGIN;

INSERT OR IGNORE INTO tasks (instruction, action_type, idempotency_key, status, priority, max_attempts)
VALUES
  (
    '{"action":"verify_runtime","language":"python","min_version":"3.11","idempotency_key":"bootstrap-verify-python-311"}',
    'verify_runtime',
    'bootstrap-verify-python-311',
    'pending',
    'critical',
    3
  ),
  (
    '{"action":"create_directories","paths":["./workspace/logs","./workspace/data","./workspace/tmp"],"idempotency_key":"bootstrap-create-workspace-dirs"}',
    'create_directories',
    'bootstrap-create-workspace-dirs',
    'pending',
    'high',
    3
  ),
  (
    '{"action":"run_health_check","endpoint":"http://127.0.0.1:0/health","timeout_seconds":1,"idempotency_key":"bootstrap-health-placeholder"}',
    'run_health_check',
    'bootstrap-health-placeholder',
    'pending',
    'low',
    1
  );

COMMIT;

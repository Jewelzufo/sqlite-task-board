# Runtime Reference

This file contains the detailed schema, event, and error tables referenced by `AGENTS.md` and `README.md`.

## Task states

```text
pending -> running -> completed
                 -> pending        retriable failure
                 -> dead-lettered  exhausted or unrecoverable failure
```

Priorities: `critical`, `high`, `medium`, `low`.

## Claim semantics

`Agent.claim_next_task()` uses `BEGIN IMMEDIATE`, selects the oldest highest-priority pending task, increments `attempt_count`, and verifies that the conditional update affected exactly one row before committing. Connections use WAL and a 5-second busy timeout.

## Action schemas

All payloads set `additionalProperties: false` and require `idempotency_key` matching `^[a-z0-9\-_]+$`.

### `verify_runtime`

```json
{
  "action": "verify_runtime",
  "language": "python",
  "min_version": "3.11",
  "idempotency_key": "verify-python"
}
```

Only Python is implemented. The runtime executes `python --version` with a minimal environment, a 10-second timeout, and patch-level version comparison.

### `create_directories`

```json
{
  "action": "create_directories",
  "paths": ["logs", "data/tmp"],
  "idempotency_key": "create-workspace-dirs"
}
```

All paths are validated before any directory is created.

### `run_health_check`

```json
{
  "action": "run_health_check",
  "endpoint": "http://127.0.0.1:8080/health",
  "timeout_seconds": 5,
  "idempotency_key": "health-check"
}
```

Only `http` and `https` are allowed. The host must appear in `security.network_allowlist`. Redirects are rejected rather than followed.

## Events

Every lifecycle event is JSONL on stdout and contains `timestamp` and `event`. Task-scoped events contain `task_id`.

| Event | Meaning |
|---|---|
| `CONFIG_LOAD_FAILED` | Configuration could not be loaded |
| `INIT_COMPLETE` | Migration, seed, and workspace initialization completed |
| `STARTUP` | Continuous worker started |
| `SCHEMA_CHECK_OK` | Database schema matches configuration |
| `SCHEMA_VERSION_MISMATCH` | Applied and configured schema versions differ |
| `EXEC_START` | Task claimed and execution started |
| `EXEC_PLAN` | Dry-run plan produced |
| `EXEC_END` | Task completed or scheduled for retry |
| `EXEC_ERROR` | Task failure with `error_code` |
| `TASK_DEAD_LETTERED` | Task exhausted retries |
| `SHUTDOWN_REQUESTED` | SIGINT or SIGTERM received |
| `SHUTDOWN_COMPLETE` | Worker stopped cleanly |

## Error codes

| Code | Meaning |
|---|---|
| `CONFIG_LOAD_FAILED` | Configuration could not be loaded |
| `SCHEMA_VERSION_MISMATCH` | Schema version is unsupported or mismatched |
| `INVALID_JSON` | Instruction is not valid JSON |
| `SCHEMA_VALIDATION_FAILED` | Payload or runtime requirement failed validation |
| `UNKNOWN_ACTION` | Action is not registered |
| `SECURITY_POLICY_VIOLATION` | Generic security boundary breach |
| `PATH_OUTSIDE_WORKSPACE` | Absolute or resolved path escapes the workspace |
| `PATH_TRAVERSAL_ATTEMPT` | Path contains `..` |
| `UNAPPROVED_HOST` | Host is not allowlisted, including redirect targets |
| `UNAPPROVED_PROTOCOL` | URL is not `http` or `https` |
| `DEPENDENCY_MISSING` | A required Python dependency is unavailable |
| `PROCESS_TIMEOUT` | Subprocess exceeded its timeout |
| `PROCESS_EXIT_NONZERO` | Subprocess returned a non-zero exit code |
| `INTERRUPTED_MAX_ATTEMPTS_EXCEEDED` | Recovered task has no retries left |
| `MAX_ATTEMPTS_EXCEEDED` | Task reached its attempt limit |
| `FILE_NOT_FOUND` | Referenced file does not exist |
| `PERMISSION_DENIED` | Filesystem permission failure |
| `HEALTH_CHECK_FAILED` | Health request failed or returned an unhealthy status |
| `DATABASE_NOT_INITIALIZED` | Database is missing or uninitialized |
| `SCHEMA_FILE_MISSING` | Migration file is missing |
| `SEED_FILE_MISSING` | Seed file is missing |
| `INIT_FAILED` | Initialization failed |
| `PYTEST_NOT_AVAILABLE` | Optional pytest checks were skipped |
| `UNKNOWN_EXCEPTION` | Unclassified runtime failure |

## Recovery

At startup, `running` tasks with attempts remaining return to `pending` with `started_at=NULL`. Tasks that already reached `max_attempts` are dead-lettered with `INTERRUPTED_MAX_ATTEMPTS_EXCEEDED`.

## Dry-run

`AGENT_DRY_RUN=true` or `--dry-run` selects the next task without claiming it, validates its payload and security policy, emits `EXEC_PLAN`, and performs no database or filesystem writes.

## Exit codes

| Command | Exit codes |
|---|---|
| `--check` | `0` pass, `1` failure |
| `--once` | `0` success, `1` task failure, `2` no pending work |
| execution loop | `0` clean shutdown |

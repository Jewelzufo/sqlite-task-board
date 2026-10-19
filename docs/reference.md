# Runtime Reference

This file contains the detailed schema, event, and error tables referenced by `AGENTS.md` and `README.md`.

## Schema version

Schema version `3` applies `migrations/0001_initial.sql`,
`0002_idempotency_audit.sql`, and `0003_task_lineage.sql` in order. `--init`
records each version in `schema_migrations` and skips already-applied files.
`agent.schema_version` must equal `3`; any other value fails with
`SCHEMA_VERSION_MISMATCH`.

## Task states

```text
pending -> running -> completed
                 -> pending        retriable failure
                 -> dead-lettered  exhausted or unrecoverable failure
```

Priorities: `critical`, `high`, `medium`, `low`.

## Claim semantics

`Agent.claim_next_task()` uses `BEGIN IMMEDIATE`, selects the oldest highest-priority pending task, increments `attempt_count`, and verifies that the conditional update affected exactly one row before committing. Connections use WAL and a 5-second busy timeout.

## Idempotency

Every validated payload must carry an `idempotency_key`. On success the runtime stores
`(key, action_type, payload_hash, result_json)` in `idempotency_ledger` in the same
transaction that marks the task completed.

`enqueue_task()` and `--enqueue` are idempotent. Repeating the same key, action, and canonical
payload returns the existing task id and inserts no second row. If that task already completed
successfully, the response is:

```json
{"idempotent_replay": true, "result": {"status": "ok"}, "status": "completed", "task_id": 7}
```

A new or still-pending task returns `"idempotent_replay": false` and `"result": null`.
Reusing the key with a different action or payload fails closed with `IDEMPOTENCY_CONFLICT`
and writes no task; a conflicting payload already attached to a queued task is dead-lettered
immediately at execution. Failed attempts do not populate the ledger, so retries still execute.
A key stays bound to its first task: retrying a dead-lettered payload requires a new key.

## Audit chain

`audit_log` stores every event emitted by the runtime. Each row contains
`prev_hash` and `entry_hash = sha256(prev_hash + canonical_json(event))`; the genesis
`prev_hash` is 64 zeros. `taskboard --audit-verify` recomputes the chain and exits `1`
at the first `prev_hash` or `entry_hash` mismatch. Dry-run does not write audit rows.
A hash chain detects mutation of stored rows but cannot detect deletion of trailing
rows unless the head hash is recorded externally.

## Policy and limits

`policy.allowed_actions`, `policy.allowed_path_roots`, and `policy.max_payload_bytes`
are evaluated after schema validation and before dispatch. Every decision emits
`POLICY_DECISION` with `rule_id`, `decision`, and `reason`; a denied payload fails with
the rule's stable code (for example `POLICY_DENIED`, `PATH_TRAVERSAL_ATTEMPT`,
`UNAPPROVED_HOST`). Denial is not retried.

`limits.max_task_seconds` caps subprocess and HTTP timeouts and is checked after the
action. `limits.max_output_bytes` caps captured output. `limits.max_tasks_per_window`
and `limits.max_failures_per_window` are evaluated over `limits.window_seconds` before
claiming: the first emits `BUDGET_EXCEEDED`, the second opens the circuit with
`CIRCUIT_OPEN`. Both are deterministic and reset when the window elapses.

## Lineage

`tasks.parent_task_id` and `tasks.depth` record task lineage. `enqueue_task()` rejects a
missing parent or depth overflow with `LINEAGE_INVALID`, and a reused key with a different
action or payload with `IDEMPOTENCY_CONFLICT`. `--graph --format dot|json` exports the
lineage; JSON output contains `nodes` and `edges`.

## Replay

`--replay FILE` loads a JSON fixture `{payload, action_type?, workspace?, expect?}`,
validates it, evaluates policy, and prints `{"ok": ..., "result": ...}`. Replay performs
no database, filesystem, or network effects.

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
| `POLICY_DECISION` | Policy allow/deny decision with rule and reason |
| `BUDGET_EXCEEDED` | Task-window budget exhausted or task exceeded its time budget |
| `CIRCUIT_OPEN` | Failure threshold reached; claiming is paused for the window |
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
| `POLICY_DENIED` | Policy rejected an otherwise schema-valid payload |
| `IDEMPOTENCY_CONFLICT` | Key reused with a different action or payload |
| `BUDGET_EXCEEDED` | Task or window budget exhausted |
| `CIRCUIT_OPEN` | Failure breaker is open |
| `LINEAGE_INVALID` | Parent missing, depth exceeded, or lineage cycle |
| `AUDIT_INTEGRITY_FAILED` | Audit append or verification failed |

## Recovery

At startup, `running` tasks with attempts remaining return to `pending` with `started_at=NULL`. Tasks that already reached `max_attempts` are dead-lettered with `INTERRUPTED_MAX_ATTEMPTS_EXCEEDED`.

## Dry-run

`AGENT_DRY_RUN=true` or `--dry-run` selects the next task without claiming it, validates its payload and policy, emits `POLICY_DECISION` and `EXEC_PLAN`, and performs no database or filesystem writes. If the payload was already executed under the same idempotency key, the plan includes `idempotent_replay: true`.

## Exit codes

| Command | Exit codes |
|---|---|
| `--check` | `0` pass, `1` failure |
| `--once` | `0` success, `1` task failure, `2` no pending work |
| `--audit-verify` | `0` intact chain, `1` broken or missing chain |
| `--graph` | `0` success, `1` database or format failure |
| `--enqueue` | `0` inserted or replayed, `1` validation or database failure |
| `--replay` | `0` expectation matched, `1` mismatch or unreadable fixture |
| execution loop | `0` clean shutdown |

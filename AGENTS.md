# AGENTS.md - SQLite Task Board Protocol

**Schema version:** 1  
**Protocol version:** 3.1  
**Bootstrap mode:** autonomous  
**Runtime:** Python 3.11+ with SQLite

---

## 0. Prime directive

The user only needs to point OpenCode at this file. Read it completely, create the scaffold, initialize the database, seed the task queue, validate the runtime, and continue from the SQLite queue without asking for additional setup.

Do not:

- ask where files should go when the repository is writable;
- require manual editing before first run;
- store secrets in files or logs;
- write task output outside `agent.workspace`;
- use network access during bootstrap;
- run commands with `shell=True`;
- weaken a security boundary to make one task pass.

`AGENTS.md` is the source of truth. `README.md` and `docs/reference.md` are operator references.

---

## 1. Repository goal

This repository is a local SQLite task board for one autonomous execution agent. Tasks are JSON instructions in SQLite. The agent claims work in priority order, validates payloads against strict schemas, executes registered actions, emits structured events, retries failures, and dead-letters exhausted tasks.

Non-goals for this version: distributed workers, queue replenishment, a web UI, multi-tenant authentication, and hosted execution.

---

## 2. Required structure

```text
sqlite-task-board/
├── AGENTS.md
├── README.md
├── opencode.json
├── agent.py
├── config.yaml
├── pyproject.toml
├── requirements.txt
├── Makefile
├── CHANGELOG.md
├── .gitignore
├── migrations/0001_initial.sql
├── migrations/0002_idempotency_audit.sql
├── migrations/0003_task_lineage.sql
├── seeds/bootstrap_tasks.sql
├── docs/reference.md
├── docs/ci-workflow.yml
├── scripts/ci.sh
├── tests/
│   ├── test_agent_contract.py
│   └── test_behavior.py
└── workspace/.gitkeep
```

`workspace/` is the only default writable task workspace and must be safe to delete and recreate.

---

## 3. Bootstrap sequence

1. Resolve `REPO_ROOT` as the directory containing this file.
2. Create `opencode.json` first so future sessions load this protocol.
3. Create `migrations/`, `seeds/`, `tests/`, `workspace/`, and `workspace/.gitkeep`.
4. Create `config.yaml`, the migration, seed, runtime, tests, packaging, and documentation.
5. Run `python agent.py --init`.
6. Run `python -m py_compile agent.py`, `python agent.py --check`, and `python agent.py --status`.
7. Run `python -m pytest tests` when pytest is available.
8. Run `python agent.py --once` and continue operating from the queue.

`--init` must be idempotent. Re-running it must not duplicate bootstrap tasks.

---

## 4. Configuration

`config.yaml` is the only configuration file:

```yaml
agent:
  workspace: "./workspace"
  database: "./tasks.db"
  schema_version: 3
  protocol_version: "3.2"

security:
  network_allowlist:
    - "127.0.0.1"
    - "localhost"

policy:
  allowed_actions:
    - "verify_runtime"
    - "create_directories"
    - "run_health_check"
  allowed_path_roots:
    - "."
  max_payload_bytes: 65536

limits:
  max_task_seconds: 60
  max_output_bytes: 10240
  max_tasks_per_window: 100
  max_failures_per_window: 10
  window_seconds: 60
  max_spawn_depth: 3
```

Rules:

- Relative paths resolve from `REPO_ROOT`.
- `AGENT_CONFIG` overrides the config path.
- `agent.schema_version` must equal `3`; `agent.protocol_version` is `3.2`.
- `security.network_allowlist` must be a non-empty list of host strings.
- `policy.allowed_actions` defaults to every registered action; a configured list must name only registered actions.
- `policy.allowed_path_roots` are workspace-relative and must not be absolute or contain `..`.
- `limits` are deterministic integers; unknown keys fail closed.
- Unknown top-level keys produce a warning and are ignored.
- The file contains no secrets and remains tracked in Git.

---

## 5. Database schema

The migrations are `migrations/0001_initial.sql`, `0002_idempotency_audit.sql`, and
`0003_task_lineage.sql`. They create:

- `tasks`: task state, priority, retries, timestamps, error fields, and lineage
  (`parent_task_id`, `depth`);
- `schema_migrations`: applied schema versions;
- `idempotency_ledger`: key, action, payload hash, result, and completion time;
- `audit_log`: append-only, hash-chained lifecycle events;
- `task_generation_log`: reserved for future replenishment.

Task states:

```text
pending -> running -> completed
                 -> pending        retriable failure
                 -> dead-lettered  final or non-retriable failure
```

Priority order: `critical`, `high`, `medium`, `low`.

### Atomic claim

The runtime uses `BEGIN IMMEDIATE`, selects the oldest highest-priority eligible pending task, increments `attempt_count`, and requires the conditional update to affect exactly one row before committing. Connections use WAL and `PRAGMA busy_timeout=5000`.

### Idempotency

A successful action writes its result to `idempotency_ledger` in the same transaction that marks the task completed. Replaying the same key and payload returns the stored result without re-executing. Reusing a key with a different action or payload fails with `IDEMPOTENCY_CONFLICT` and is dead-lettered immediately. Failed attempts are not recorded.

---

## 6. Runtime contract

`agent.py` must:

- load YAML config from `AGENT_CONFIG` or `./config.yaml`;
- refuse unsupported schema versions;
- recover interrupted `running` tasks at startup;
- claim tasks atomically;
- validate JSON with action-specific schemas;
- enforce workspace and network boundaries;
- evaluate `policy.allowed_actions`, `policy.allowed_path_roots`, and `policy.max_payload_bytes` before dispatch;
- enforce `limits` and the deterministic failure breaker;
- record successful results in `idempotency_ledger` and replay them by key;
- append every event to the hash-chained `audit_log`;
- execute only actions in `ACTION_REGISTRY`;
- persist a stable `error_code` on failure;
- emit JSONL events on stdout and human progress on stderr;
- support `--init`, `--check`, `--status`, `--once`, `--dry-run`, `--audit-verify`,
  `--graph`, `--enqueue`, `--replay`, and continuous mode;
- support `AGENT_DRY_RUN=true`;
- close cleanly on SIGINT or SIGTERM.

Use `sqlite3`, `subprocess` with `shell=False`, `urllib`, `pyyaml`, and `jsonschema` from the declared dependencies. If PyYAML or jsonschema is missing, fail closed with `CONFIG_LOAD_FAILED` or `DEPENDENCY_MISSING`.

---

## 7. Actions

All schemas set `additionalProperties: false` and require an `idempotency_key` matching `^[a-z0-9\-_]+$`.

| Action | Behavior |
|---|---|
| `verify_runtime` | Python only; runs `python --version` with timeout, minimal env, and patch-level comparison |
| `create_directories` | Pre-validates all paths, then creates them beneath `workspace/` |
| `run_health_check` | Allows only `http`/`https` and exact allowlisted hosts; rejects redirects |

Full schemas and examples are in `docs/reference.md`. `ACTION_SCHEMAS` in `agent.py` is authoritative.

---

## 8. Security model

### Filesystem

- Task writes must stay inside `agent.workspace`.
- Absolute paths outside the workspace and any `..` component are rejected.
- Validate all paths before creating anything.
- Do not follow symlinks out of the workspace.

### Subprocesses

- Never use `shell=True`.
- Pass arguments as lists.
- Set bounded timeouts.
- Use a minimal environment.
- Truncate captured output before logging or storing it.

### Network

- Only `http` and `https` are allowed.
- Hosts must exactly match `security.network_allowlist`.
- Reject redirects rather than following an unvalidated host.
- Bootstrap does not require network access.

### Secrets

- Never log secrets.
- Do not write secrets into generated files.
- Redact keys containing `password`, `secret`, `token`, `credential`, or `api_key`.

### Policy and audit

- Policy evaluation happens after schema validation and before dispatch.
- Policy decisions are deterministic and emit `POLICY_DECISION`; denial is not retried.
- `--audit-verify` recomputes `sha256(prev_hash + canonical_json(event))` for every row
  and fails at the first broken link.
- Audit writes are skipped in dry-run so the command remains side-effect free.
- Resource limits and the breaker are computed from persisted timestamps, never from a
  model or a statistical baseline.

---

## 9. Events and errors

Every event is JSONL on stdout with `timestamp` and `event`. Task-scoped events include `task_id`.

Required events: `CONFIG_LOAD_FAILED`, `INIT_COMPLETE`, `STARTUP`, `SCHEMA_CHECK_OK`, `SCHEMA_VERSION_MISMATCH`, `EXEC_START`, `EXEC_PLAN`, `POLICY_DECISION`, `EXEC_END`, `EXEC_ERROR`, `BUDGET_EXCEEDED`, `CIRCUIT_OPEN`, `TASK_DEAD_LETTERED`, `SHUTDOWN_REQUESTED`, `SHUTDOWN_COMPLETE`.

`EXEC_ERROR` must include `task_id`, `error_code`, and `error`. Task failures persist `error_code` in SQLite. The complete error code table is in `docs/reference.md`.

---

## 10. Retry and recovery

At startup:

- `running` tasks with `attempt_count < max_attempts` return to `pending` and clear `started_at`;
- `running` tasks at or above the limit are dead-lettered with `INTERRUPTED_MAX_ATTEMPTS_EXCEEDED`.

On failure:

- below the limit: `pending` with `error_code` and `last_error`;
- at the limit: `dead-lettered` with `error_code`, `last_error`, and `failed_at`;
- `IDEMPOTENCY_CONFLICT`, `POLICY_DENIED`, `LINEAGE_INVALID`, and `BUDGET_EXCEEDED`
  are non-retriable and dead-letter immediately.

There is no mandatory inter-retry delay.

---

## 11. Dry-run

`AGENT_DRY_RUN=true` or `--dry-run` must:

- select the next task without claiming it;
- validate its payload and policy;
- emit `POLICY_DECISION` and `EXEC_PLAN` with `dry_run: true`;
- include `idempotent_replay: true` when the key already has a stored result;
- create no files;
- make no network requests;
- write no audit or ledger rows;
- leave task state and `attempt_count` unchanged.

The same task remains visible for the next dry-run until the queue changes.

---

## 12. Testing and acceptance

Run:

```bash
python -m py_compile agent.py
python agent.py --init
python agent.py --check
python agent.py --status
python -m pytest tests
python -m pytest --cov=agent --cov-fail-under=80
ruff check .
```

Acceptance criteria:

- `--init` creates three pending tasks and is idempotent;
- `--check` exits 0 and `--status` reports the queue;
- claiming is atomic and the race test passes;
- failures persist a documented `error_code` and emit `EXEC_ERROR`;
- idempotent replay executes the action once and returns the stored result;
- policy decisions are emitted for allow and deny, including dry-run;
- `--audit-verify` detects tampering at the correct sequence number;
- budgets and the breaker are deterministic under an injected window;
- lineage rejects missing parents and depth overflow;
- dry-run has no side effects;
- health checks reject unapproved hosts, protocols, and redirects;
- tests and lint pass via `scripts/ci.sh` and the `docs/ci-workflow.yml` template;
- no database, cache, or unreferenced binary asset is tracked.

---

## 13. Change control

Any change to actions, schemas, event names, or error codes must update:

1. `agent.py`;
2. `docs/reference.md`;
3. the parity tests;
4. `README.md` if operator behavior changes.

Do not add an action without a strict schema, a security review, and offline tests for valid, invalid, and adversarial payloads.

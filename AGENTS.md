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
  schema_version: 1
  protocol_version: "3.1"

security:
  network_allowlist:
    - "127.0.0.1"
    - "localhost"
```

Rules:

- Relative paths resolve from `REPO_ROOT`.
- `AGENT_CONFIG` overrides the config path.
- `agent.schema_version` must equal `1`.
- `security.network_allowlist` must be a non-empty list of host strings.
- Unknown top-level keys produce a warning and are ignored.
- The file contains no secrets and remains tracked in Git.

---

## 5. Database schema

The migration is `migrations/0001_initial.sql`. It creates:

- `tasks`: task state, priority, retries, timestamps, and error fields;
- `schema_migrations`: applied schema version;
- `task_generation_log`: reserved for future replenishment.

Task states:

```text
pending -> running -> completed
                 -> pending        retriable failure
                 -> dead-lettered  final failure
```

Priority order: `critical`, `high`, `medium`, `low`.

### Atomic claim

The runtime uses `BEGIN IMMEDIATE`, selects the oldest highest-priority eligible pending task, increments `attempt_count`, and requires the conditional update to affect exactly one row before committing. Connections use WAL and `PRAGMA busy_timeout=5000`.

---

## 6. Runtime contract

`agent.py` must:

- load YAML config from `AGENT_CONFIG` or `./config.yaml`;
- refuse unsupported schema versions;
- recover interrupted `running` tasks at startup;
- claim tasks atomically;
- validate JSON with action-specific schemas;
- enforce workspace and network boundaries;
- execute only actions in `ACTION_REGISTRY`;
- persist a stable `error_code` on failure;
- emit JSONL events on stdout and human progress on stderr;
- support `--init`, `--check`, `--status`, `--once`, `--dry-run`, and continuous mode;
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

---

## 9. Events and errors

Every event is JSONL on stdout with `timestamp` and `event`. Task-scoped events include `task_id`.

Required events: `CONFIG_LOAD_FAILED`, `INIT_COMPLETE`, `STARTUP`, `SCHEMA_CHECK_OK`, `SCHEMA_VERSION_MISMATCH`, `EXEC_START`, `EXEC_PLAN`, `EXEC_END`, `EXEC_ERROR`, `TASK_DEAD_LETTERED`, `SHUTDOWN_REQUESTED`, `SHUTDOWN_COMPLETE`.

`EXEC_ERROR` must include `task_id`, `error_code`, and `error`. Task failures persist `error_code` in SQLite. The complete error code table is in `docs/reference.md`.

---

## 10. Retry and recovery

At startup:

- `running` tasks with `attempt_count < max_attempts` return to `pending` and clear `started_at`;
- `running` tasks at or above the limit are dead-lettered with `INTERRUPTED_MAX_ATTEMPTS_EXCEEDED`.

On failure:

- below the limit: `pending` with `error_code` and `last_error`;
- at the limit: `dead-lettered` with `error_code`, `last_error`, and `failed_at`.

There is no mandatory inter-retry delay.

---

## 11. Dry-run

`AGENT_DRY_RUN=true` or `--dry-run` must:

- select the next task without claiming it;
- validate its payload and security policy;
- emit `EXEC_PLAN` with `dry_run: true`;
- create no files;
- make no network requests;
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

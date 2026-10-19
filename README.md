# SQLite Task Board for OpenCode

> A local, zero-trust task queue for one autonomous OpenCode execution agent, backed by Python and SQLite.

## Overview

SQLite is the source of task state, [`AGENTS.md`](./AGENTS.md) is the operating contract, and [`agent.py`](./agent.py) is the runtime. Point OpenCode at `AGENTS.md`; it creates the scaffold, initializes `tasks.db`, seeds the queue, and starts processing work.

- Tasks are JSON instructions in SQLite.
- Payloads pass strict action-specific JSON Schema validation.
- Only registered actions execute.
- Writes stay inside `workspace/`; subprocesses use `shell=False` and timeouts.
- Network requests require an allowlisted host and `http`/`https`.
- Lifecycle and error events are JSONL on stdout.
- Failed work is retried, then dead-lettered.
- Claiming is atomic and safe for one worker by default.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]'
taskboard init && taskboard check && taskboard status
taskboard --once
```

Without installation, use `python agent.py --init|--check|--status|--once`. `--init` is idempotent: it applies the migration, seeds three bootstrap tasks, and creates `workspace/logs`, `workspace/data`, and `workspace/tmp`.

## Project layout

```text
AGENTS.md  README.md  opencode.json  agent.py  config.yaml
pyproject.toml  requirements.txt  Makefile  CHANGELOG.md
docs/reference.md  migrations/0001_initial.sql  seeds/bootstrap_tasks.sql
tests/test_agent_contract.py  tests/test_behavior.py  workspace/.gitkeep
```

`workspace/` is disposable task output and the only writable location for ordinary execution.

## How execution works

Each task row contains an action type, JSON instruction, priority, retry counters, status, and timestamps. The agent recovers interrupted work, atomically claims the oldest highest-priority pending task, validates its payload and security policy, executes it, and records completion, retry, or dead-letter state.

```text
pending -> running -> completed
                 -> pending        retriable failure
                 -> dead-lettered  final failure
```

Priority order: `critical` → `high` → `medium` → `low`.

## Actions

| Action | Purpose |
|---|---|
| `verify_runtime` | Verifies Python meets `min_version` (patch-aware) |
| `create_directories` | Creates pre-validated directories under `workspace/` |
| `run_health_check` | Checks an allowlisted `http`/`https` endpoint |

The bootstrap health check targets `http://127.0.0.1:0/health`; its failure is intentional. Full schemas are in [`docs/reference.md`](./docs/reference.md).

## Security controls

- Writes resolve beneath `agent.workspace`; absolute paths and `..` are rejected.
- All directory paths are validated before any directory is created.
- Payloads use `additionalProperties: false` and a `FormatChecker`.
- Subprocesses use list arguments, `shell=False`, timeouts, a minimal environment, and truncated output.
- Health checks require `http`/`https` and an exact host allowlist match; redirects are rejected.
- Sensitive keys and `key=value` patterns are redacted from events and errors.
- No secrets are required or written by bootstrap.

## Configuration

```yaml
agent:
  workspace: "./workspace"
  database: "./tasks.db"
  schema_version: 1
  protocol_version: "3.1"

security:
  network_allowlist: ["127.0.0.1", "localhost"]
```

Relative paths resolve from the repository root. `AGENT_CONFIG` selects an alternate file. `agent.schema_version` must equal `1`; `security.network_allowlist` must be a non-empty list of hosts.

## Commands

| Command | Description |
|---|---|
| `taskboard --init` | Migrate, seed, and create workspace directories |
| `taskboard --check` | Validate dependencies, config, workspace, database, and schema |
| `taskboard --status [--json]` | Show queue counts and the next task |
| `taskboard --once [--dry-run]` | Execute or plan one task |
| `taskboard` | Run the continuous worker |

`--once` exit codes: `0` success, `1` task failure, `2` no pending work.

## Dry run

`AGENT_DRY_RUN=true taskboard --once` or `taskboard --once --dry-run` selects and validates the next task without claiming it, creates no files, makes no network requests, and performs no database writes. It emits `EXEC_PLAN` and leaves the task pending.

## Tests and CI

```bash
python -m pytest tests
python -m pytest --cov=agent --cov-fail-under=80
ruff check .
```

CI runs Python 3.11 and 3.12, linting, coverage, and the init/check/status smoke test. Copy `docs/ci-workflow.yml` to `.github/workflows/ci.yml` where workflow permissions allow; `scripts/ci.sh` runs the same checks locally.

## Operations and troubleshooting

- SQLite is authoritative; `tasks.db` is generated and ignored by Git.
- `AGENTS.md` is authoritative if documentation conflicts; `workspace/` output is disposable.
- Queue replenishment is not implemented; enqueue work explicitly. Logs are JSONL on stdout; human progress goes to stderr.
- Missing PyYAML/jsonschema: run `python -m pip install -r requirements.txt`; the runtime fails closed.
- Task stuck in `running`: restart the agent; recovery returns it to `pending` or dead-letters it.
- Health check failure is expected for the bootstrap port `0` endpoint. Extend only the narrowest allowlist or workspace setting for real work.

## License

MIT. See [`LICENSE`](./LICENSE).

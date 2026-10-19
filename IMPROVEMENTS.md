# Improvements to move `sqlite-task-board` above 90

**Status:** Draft for implementation
**Baseline:** `be3a98d` (working tree clean, 21 tests passing)
**Current score:** 80/100
**Projected score after this draft:** 93/100
**Constraint:** keep the project local-first, single-process by default, and security-narrow.

---

## 1. Why the score is 80 today

| Area | Current | Ceiling | Gap |
|---|---:|---:|---|
| First-run usability | 17 | 20 | Manual venv + `requirements.txt`; no `taskboard` entry point |
| Core correctness | 17 | 20 | Non-atomic claim (`agent.py:440-454`), weak error taxonomy, incomplete dry-run |
| Security | 13 | 15 | No subprocess environment minimization or log redaction |
| Tests | 12 | 15 | No concurrency, allowlist, successful-directory, dry-run, or recovery tests |
| Documentation | 11 | 15 | `README.md` still 369 lines; `AGENTS.md` 690; parity is manual |
| Maintainability | 10 | 15 | Unreferenced `assets/`, no CI/lint, temporary `PROPOSAL.md` |

The remaining gaps are not architectural. The runtime, migration, seed, CLI, and docs now agree on the critical path. Closing the items below is mostly packaging, test depth, and follow-through.

---

## 2. Target scorecard

| Area | Target | Gain | Main work |
|---|---:|---:|---|
| First-run usability | 19 | +2 | `pyproject.toml`, `taskboard` console script, optional Makefile |
| Core correctness | 20 | +3 | Atomic claim, error codes, honest dry-run, Python-only runtime check |
| Security | 14 | +1 | Subprocess env, output truncation, log redaction |
| Tests | 14 | +2 | 10–12 new tests including a two-connection claim race |
| Documentation | 14 | +3 | README ≤120 lines, trimmed AGENTS, doc/code parity tests |
| Maintainability | 12 | +2 | CI, ruff, coverage gate, remove assets and temporary docs |
| **Total** | **93** | **+13** | |

---

## 3. Workstream A — Packaging and one-command setup (+2)

**Problem:** Users still need `python -m venv`, `pip install -r requirements.txt`, and remember the entry point. Dependencies live in `requirements.txt` with no project metadata.

**Changes**

1. Add `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "sqlite-task-board"
version = "0.2.0"
description = "Local SQLite task board for one autonomous execution agent"
requires-python = ">=3.11"
dependencies = ["pyyaml>=6.0.0", "jsonschema>=4.0.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "pytest-cov>=5.0.0", "ruff>=0.6.0"]

[project.scripts]
taskboard = "agent:main"

[tool.setuptools]
py-modules = ["agent"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.coverage.run]
source = ["agent"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B"]
```

2. Delete `requirements.txt`; update `README.md:58`, `AGENTS.md:347-359`, and `tests/test_agent_contract.py::test_requirements_txt_exists`.
3. Add optional `Makefile` targets: `setup`, `init`, `check`, `status`, `run`, `test`, `lint`.
4. Keep `python agent.py ...` working as the fallback entry point.

**Acceptance**

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
taskboard init && taskboard check && taskboard status
taskboard --help
```

**Risk:** the console script imports `agent` from the repository root; setuptools `py-modules` handles this, but verify from a clean copy, not only editable install.

---

## 4. Workstream B — Atomic claim and error taxonomy (+3)

### B1. Atomic claim

**Problem:** `run_once` selects a task and then updates it in a separate statement (`agent.py:440-454`). The `status='pending'` predicate prevents double-completion, but a second worker can select the same row before the first update, causing duplicate action execution.

**Changes**

1. Add `claim_next_task()`:

```python
def claim_next_task(self):
    self.conn.execute("BEGIN IMMEDIATE")
    try:
        row = self.conn.execute(CLAIM_SELECT_SQL).fetchone()
        if row is None:
            self.conn.rollback()
            return None
        cur = self.conn.execute(
            "UPDATE tasks SET status='running', started_at=CURRENT_TIMESTAMP, "
            "attempt_count=attempt_count+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='pending'",
            (row["id"],),
        )
        if cur.rowcount != 1:
            self.conn.rollback()
            return None
        self.conn.commit()
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    except Exception:
        self.conn.rollback()
        raise
```

2. Use it from `run_once`; remove the separate update at lines 452-454.
3. On connection setup (`agent.py:148`), set `timeout=10`, `PRAGMA busy_timeout=5000`, and `PRAGMA journal_mode=WAL`.
4. Preserve `attempt_count` semantics: increment exactly once at claim time; failure handling uses the claimed count.

**Acceptance:** two agents on two connections, one task, one `claim_next_task()` winner. Add this as a test.

### B2. Error taxonomy

**Problem:** `run_once` stores only `str(e)` (`agent.py:468-478`). The `error_code` column is unused, and documented codes such as `INVALID_JSON`, `SCHEMA_VALIDATION_FAILED`, `UNKNOWN_ACTION`, `PATH_TRAVERSAL_ATTEMPT`, `UNAPPROVED_HOST`, and `PROCESS_TIMEOUT` never reach the database or logs.

**Changes**

1. Add a small exception hierarchy or a `classify_error(exc)` function mapping:
   - `json.JSONDecodeError` → `INVALID_JSON`
   - `ValueError("Payload validation failed…")` → `SCHEMA_VALIDATION_FAILED`
   - `ValueError("Unknown action type…")` → `UNKNOWN_ACTION`
   - `ValueError("Path traversal rejected…")` → `PATH_TRAVERSAL_ATTEMPT`
   - `ValueError("Host … not in allowlist")` → `UNAPPROVED_HOST`
   - `ValueError("Invalid protocol…")` → `UNAPPROVED_PROTOCOL`
   - `subprocess.TimeoutExpired` → `PROCESS_TIMEOUT`
   - fallback → `UNKNOWN_EXCEPTION`
2. Persist `error_code` on retry and dead-letter; keep `error_message` truncated to 500 chars.
3. Emit `EXEC_ERROR` with `{task_id, error_code, error}` on failures; keep `TASK_DEAD_LETTERED` for final failure.
4. Add `PROCESS_EXIT_NONZERO` handling in `verify_runtime` when `returncode != 0`.

### B3. Make `verify_runtime` honest

**Problem:** `agent.py:211-223` only checks Python; every other allowed language returns `{"status": "skipped"}` and the task is marked completed.

**Decision:** restrict the schema enum to `["python"]` for this version, or implement real checks for the other four languages. Recommendation: restrict now and add languages when their checks exist. Update `ACTION_SCHEMAS`, AGENTS §13, and tests together.

### B4. True dry-run semantics

**Problem:** `AGENT_DRY_RUN` skips DB writes (`agent.py:432-433`, `452`) but still calls handlers, so `create_directories` writes to disk. That is not a safe preview.

**Decision:** either:

- implement planner mode: validate + security-check + emit planned action, never call the handler; or
- explicitly state that dry-run suppresses only DB persistence and may still perform action side effects.

Recommendation: planner mode. Add `--dry-run` to argparse (in addition to the env var) and test that `create_directories` creates nothing.

---

## 5. Workstream C — Security hardening (+1)

1. **Subprocess environment:** pass a minimal environment to `verify_runtime`:
   ```python
   env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
   subprocess.run(..., env=env)
   ```
2. **Output truncation:** cap captured stdout/stderr at 10 KB before logging or persisting.
3. **Log redaction:** add `redact(value)` for keys matching `password|secret|token|credential|api_key|key`, apply to `_log_event` and error messages. No current payload contains secrets, but this prevents future leaks.
4. **Config warnings:** warn (do not fail) on unknown top-level config keys so removed keys such as `task_board` do not silently reappear.
5. Keep the existing path and allowlist checks; do not broaden them to satisfy tests.

**Acceptance:** tests for a 20 KB subprocess output truncated to 10 KB, log redaction, and `UNAPPROVED_HOST`.

---

## 6. Workstream D — Test depth (+2)

Add these to `tests/` (split `test_agent_contract.py` into `test_contract.py` and `test_behavior.py`):

| Test | Asserts |
|---|---|
| `test_claim_is_atomic` | Two connections, one task, exactly one winner |
| `test_claim_does_not_exceed_attempts` | Attempt count increments once per claim |
| `test_invalid_json_error_code` | `INVALID_JSON` persisted and logged |
| `test_schema_validation_error_code` | `SCHEMA_VALIDATION_FAILED` persisted |
| `test_unknown_action_error_code` | `UNKNOWN_ACTION` persisted |
| `test_health_check_host_rejected` | `UNAPPROVED_HOST`, no network call |
| `test_health_check_protocol_rejected` | `UNAPPROVED_PROTOCOL` |
| `test_create_directories_success` | `workspace/logs` exists and result is relative |
| `test_dry_run_does_not_create_dirs` | Planner mode has no side effects |
| `test_recovery_clears_started_at` | Interrupted task returns to pending |
| `test_status_json_contract` | Stable JSON keys and priority counts |
| `test_docs_action_parity` | AGENTS/README action names equal `ACTION_REGISTRY` |
| `test_event_names_parity` | Emitted event names are documented |

**Coverage gate:** `pytest --cov=agent --cov-fail-under=85`.

**Acceptance:** ≥30 tests, all offline, no fixed ports requiring a live service.


---

## 7. Workstream E — Documentation compression and parity (+3)

### E1. README to ≤120 lines

Keep only:

1. One-paragraph overview
2. Quickstart: install → `taskboard init` → `check` → `status` → `run`
3. Task lifecycle diagram
4. Three actions and their controls
5. Configuration block
6. Troubleshooting: missing deps, uninitialized DB, expected port-0 failure
7. Link to `AGENTS.md` for protocol detail

Move operational detail, full event examples, and error tables to `AGENTS.md` or `docs/reference.md`.

### E2. AGENTS.md trim

- Remove the duplicated migration and seed SQL blocks (`AGENTS.md` §5–§6); reference the files instead.
- Remove full action JSON schemas (§13); state that `ACTION_SCHEMAS` in `agent.py` is authoritative and list required fields.
- Keep bootstrap sequence, security model, state machine, and acceptance criteria.
- Target ≤300 lines.

### E3. Automated parity

Add tests that parse the documented action/event/error names and compare them to `ACTION_REGISTRY`, emitted event constants, and a `ERROR_CODES` set. This prevents the drift that caused P0-4.

### E4. OpenCode autonomy

`opencode.json` currently uses `"bash": "ask"`, which blocks autonomous bootstrap. Set `"bash": "allow"` with the AGENTS guardrails, or document that prompts are expected. Recommendation: allow, because bootstrap must run `pip`, `taskboard init`, and tests.

**Acceptance:** `wc -l README.md` ≤120; `wc -l AGENTS.md` ≤300; parity tests pass.

---

## 8. Workstream F — CI, lint, and repository hygiene (+2)

1. Add `.github/workflows/ci.yml`:
   - Python 3.11 and 3.12
   - `pip install -e '.[dev]'`
   - `ruff check .`
   - `pytest --cov=agent --cov-fail-under=85`
   - fresh-copy smoke: `taskboard init && taskboard check && taskboard status`
2. Add `ruff` and fix import/line-length issues; keep `agent.py` single-file.
3. Remove `assets/images/` (unreferenced) or move to `docs/assets/` and reference the images.
4. Move `PROPOSAL.md` and this draft into `docs/decisions/`, or delete after acceptance; do not leave temporary planning files in the root.
5. Set `source`, `branch`, and `title` metadata in a PR template only if the repository will accept contributions.
6. Add a `CHANGELOG.md` entry for 0.2.0 if versioning is adopted.

**Acceptance:** CI green on both Python versions; `ruff check .` exits 0; root contains no unreferenced binary assets.

---

## 9. Suggested implementation order

| PR | Scope | Score gain | Depends on |
|---|---|---:|---|
| 1 | `pyproject.toml`, console script, remove `requirements.txt`, Makefile | +2 | — |
| 2 | Atomic claim, DB pragmas, error taxonomy, Python-only runtime | +3 | — |
| 3 | Security hardening: env, truncation, redaction, config warnings | +1 | PR 2 |
| 4 | Test expansion and coverage gate | +2 | PR 1–3 |
| 5 | README/AGENTS rewrite, parity tests, `opencode.json` | +3 | PR 1 |
| 6 | CI, ruff, remove assets/temporary docs, changelog | +2 | PR 1–5 |

Each PR should leave `pytest` green and the CLI working. Keep the runtime single-file through PR 6; splitting into a package adds import and packaging risk without improving the user experience.

---

## 10. Definition of done

- [ ] `pip install -e '.[dev]'` and `taskboard` work from a clean clone.
- [ ] `taskboard init` is idempotent; `check`, `status`, and `run` use the same entry point.
- [ ] Claim is atomic; a two-connection race test passes.
- [ ] Every failure stores and logs a documented `error_code`.
- [ ] Dry-run performs no filesystem or database side effects.
- [ ] `verify_runtime` either supports a language or fails clearly.
- [ ] ≥30 tests, ≥85% coverage, `ruff check` clean.
- [ ] README ≤120 lines; AGENTS ≤300; doc/code parity tests pass.
- [ ] CI is green on Python 3.11 and 3.12.
- [ ] No tracked DBs, caches, or unreferenced binary assets.
- [ ] Projected score ≥93.

---

## 11. Risks and non-goals

| Risk | Mitigation |
|---|---|
| `BEGIN IMMEDIATE` blocks under contention | `busy_timeout=5000`; single-process remains the default; test two connections |
| `py-modules = ["agent"]` packaging surprises | Test editable and non-editable installs; keep `python agent.py` fallback |
| Removing `requirements.txt` breaks docs/CI | Update README, AGENTS, tests, and workflow in the same PR |
| Planner dry-run skips real side effects | Document explicitly; tests assert nothing is created |
| Restricting `verify_runtime` to Python narrows capability | Accurate behavior beats silent “skipped” completion; add languages individually |
| README/AGENTS trimming loses protocol detail | Keep AGENTS authoritative; move reference tables rather than deleting them |
| `bash: allow` increases local risk | Keep path/allowlist/timeout guardrails; document the tradeoff |

**Non-goals:** queue replenishment, distributed workers, web UI, ORM, authentication, hosted execution. Those change the project’s threat model and are not needed to exceed 90 on this rubric.


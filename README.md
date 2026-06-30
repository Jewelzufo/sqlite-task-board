# SQLite Task Board Opencode Bootstrap

*A minimal local bootstrap framework for running one autonomous Opencode execution agent from a SQLite task queue.*

---

## Project Structure

```
sqlite-task-board/
├── AGENTS.md                  # Primary bootstrap and runtime protocol
├── README.md                  # This file
├── opencode.json              # Opencode config (created by agent)
├── agent.py                   # Runtime entry point
├── config.example.yaml        # Template config
├── config.yaml                # Local config (gitignored)
├── requirements.txt
├── .gitignore
├── migrations/
│   └── 0001_initial.sql       # Database schema
├── seeds/
│   └── bootstrap_tasks.sql    # Initial safe tasks
├── tests/
│   └── test_agent_contract.py
└── workspace/                 # Sandboxed task output
    └── .gitkeep
```

---

## Task Queue Model

### Lifecycle
Tasks move through the following states:
```
pending → running → completed
                 → pending (retryable failure)
                 → dead-lettered (final failure)
```

### Priority Order
1. **critical**
2. **high**
3. **medium**
4. **low**

### Default Bootstrap Tasks
- Runtime verification
- Workspace directory creation
- Health-check validation
- Schema integrity test

---

## Security Defaults

Designed for **local, controlled execution** with the following safeguards:

| Area | Control |
|------|---------|
| **Filesystem** | Task writes stay inside `workspace/` |
| **Paths** | Path traversal and unsafe absolute paths are rejected |
| **Subprocesses** | Uses `shell=False` only |
| **Network** | Allows only `http` or `https` to allowlisted local hosts |
| **Secrets** | No real secrets are written or logged |
| **Validation** | Every task payload must match a strict schema |

---

## `opencode.json`

The agent creates this file on the first run. **Minimal working example:**
```json
{
  "system_prompt_file": "AGENTS.md"
}
```
This ensures every future Opencode session in this folder automatically loads `AGENTS.md` as its system prompt.

---

## Quick Start

### 1. Point Opencode at `AGENTS.md`
Open this folder in Opencode. It will read `AGENTS.md` and self-bootstrap.

### 2. Manual Setup (Optional)
#### Install Dependencies
```bash
python -m pip install -r requirements.txt
```

#### Initialize SQLite Manually
```bash
sqlite3 tasks.db < migrations/0001_initial.sql
sqlite3 tasks.db < seeds/bootstrap_tasks.sql
```

### 3. Validate
```bash
python -m py_compile agent.py
python agent.py --check
```

### 4. Run
#### Run One Queued Task
```bash
python agent.py --once
```

#### Run Continuously
```bash
python agent.py
```

---

## Runtime Modes

| Command | Purpose |
|---------|---------|
| `python agent.py --check` | Validate configuration and runtime readiness |
| `python agent.py --once` | Execute one queued task |
| `python agent.py` | Run the continuous task loop |
| `AGENT_DRY_RUN=true python agent.py --once` | Validate and plan without persisting task changes |

---

## Acceptance Criteria

Bootstrap is complete when:
- Project scaffold exists
- `opencode.json` references `AGENTS.md`
- `tasks.db` is initialized and migrated
- Bootstrap tasks are seeded
- `python agent.py --check` passes

---

## Summary

**One file (`AGENTS.md`) → one command (open in Opencode) → a fully functional SQLite-backed agent runtime.**
All future work is queued, validated, and executed locally through the task board.

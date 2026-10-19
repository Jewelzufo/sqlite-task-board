#!/usr/bin/env python3
"""
SQLite Task Board Agent

Local-first autonomous execution agent driven by AGENTS.md.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError

try:
    import yaml
except ImportError:
    yaml = None

try:
    from jsonschema import validate, ValidationError as SchemaValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

ROOT = Path(__file__).parent.resolve()
DB_PATH = ROOT / "tasks.db"
WORKSPACE = ROOT / "workspace"
CONFIG_PATH = ROOT / "config.yaml"
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DRY_RUN = os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")


class ConfigError(RuntimeError):
    """Configuration or scaffold error carrying a stable error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _timestamp():
    return datetime.utcnow().isoformat() + "Z"


def emit_event(event_type, **fields):
    """Emit one JSONL lifecycle event to stdout."""
    entry = {"timestamp": _timestamp(), "event": event_type}
    entry.update(fields)
    if DRY_RUN:
        entry["dry_run"] = True
    print(json.dumps(entry, default=str), flush=True)


def resolve_path(value, default):
    path = Path(value) if value else Path(default)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def configured_db_path(config, override=None):
    agent_config = config.get("agent") or {}
    return resolve_path(override or agent_config.get("database"), DB_PATH)


def load_config(path=None):
    config_path = Path(path or os.getenv("AGENT_CONFIG") or CONFIG_PATH)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if yaml is None:
        raise ConfigError(
            "CONFIG_LOAD_FAILED",
            "PyYAML is required; run: python -m pip install -r requirements.txt",
        )
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("CONFIG_LOAD_FAILED", f"Cannot read {config_path}: {exc}") from exc
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError("CONFIG_LOAD_FAILED", f"Invalid YAML in {config_path}: {exc}") from exc
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ConfigError("CONFIG_LOAD_FAILED", f"{config_path} must contain a mapping")
    if not isinstance(config.get("agent"), dict):
        raise ConfigError("CONFIG_LOAD_FAILED", f"{config_path} must define an 'agent' mapping")
    try:
        int(config["agent"].get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("CONFIG_LOAD_FAILED", "agent.schema_version must be an integer") from exc
    return config

ACTION_SCHEMAS = {
    "verify_runtime": {
        "type": "object",
        "required": ["action", "language", "min_version", "idempotency_key"],
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "const": "verify_runtime"},
            "language": {"type": "string", "enum": ["python", "node", "go", "rust", "java"], "maxLength": 32},
            "min_version": {"type": "string", "pattern": r"^\d+(\.\d+)?(\.\d+)?$", "maxLength": 16},
            "idempotency_key": {"type": "string", "maxLength": 128, "pattern": r"^[a-z0-9\-_]+$"}
        }
    },
    "create_directories": {
        "type": "object",
        "required": ["action", "paths", "idempotency_key"],
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "const": "create_directories"},
            "paths": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string", "maxLength": 256}},
            "idempotency_key": {"type": "string", "maxLength": 128, "pattern": r"^[a-z0-9\-_]+$"}
        }
    },
    "run_health_check": {
        "type": "object",
        "required": ["action", "endpoint", "timeout_seconds", "idempotency_key"],
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "const": "run_health_check"},
            "endpoint": {"type": "string", "maxLength": 512, "format": "uri"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
            "idempotency_key": {"type": "string", "maxLength": 128, "pattern": r"^[a-z0-9\-_]+$"}
        }
    }
}

ACTION_REGISTRY = {}

def register_action(name):
    def decorator(func):
        ACTION_REGISTRY[name] = func
        return func
    return decorator

class Agent:
    def __init__(self, db_path=None, config=None, auto_init=True):
        self.config = config if config is not None else load_config()
        agent_config = self.config.get("agent") or {}
        self.db_path = configured_db_path(self.config, db_path)
        self.workspace = resolve_path(agent_config.get("workspace"), WORKSPACE)
        self.schema_version = int(agent_config.get("schema_version", 1))
        self.protocol_version = str(agent_config.get("protocol_version", "3.1"))
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        if auto_init:
            self._init_db()
        self._register_actions()

    def _init_db(self):
        migration_file = ROOT / "migrations" / "0001_initial.sql"
        if not migration_file.exists():
            raise ConfigError("SCHEMA_FILE_MISSING", f"Migration not found: {migration_file}")
        self.conn.executescript(migration_file.read_text(encoding="utf-8"))
        self.conn.commit()

    def _workspace_path(self, relative):
        base = self.workspace.resolve()
        target = (base / relative).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"Path traversal rejected: {relative}")
        return target

    def database_ready(self):
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def initialize(self):
        """Apply the migration, seed the queue, and create workspace directories."""
        self._init_db()
        seed_file = ROOT / "seeds" / "bootstrap_tasks.sql"
        if not seed_file.exists():
            raise ConfigError("SEED_FILE_MISSING", f"Seed file not found: {seed_file}")
        self.conn.executescript(seed_file.read_text(encoding="utf-8"))
        self.conn.commit()
        for name in ("logs", "data", "tmp"):
            self._workspace_path(name).mkdir(parents=True, exist_ok=True)
        total = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='pending'"
        ).fetchone()[0]
        result = {
            "database": str(self.db_path),
            "workspace": str(self.workspace),
            "tasks": total,
            "pending_tasks": pending,
            "schema_version": self.schema_version,
        }
        self._log_event("INIT_COMPLETE", result)
        return result

    def _register_actions(self):
        @register_action("verify_runtime")
        def verify_runtime(payload):
            language = payload.get("language", "python")
            min_version = payload.get("min_version", "3.11")
            if language == "python":
                result = subprocess.run([sys.executable, "--version"], capture_output=True, text=True, shell=False, timeout=10)
                version_str = result.stdout.strip().replace("Python ", "")
                parts = version_str.split(".")
                current_major = int(parts[0])
                current_minor = int(parts[1]) if len(parts) > 1 else 0
                req_parts = min_version.split(".")
                req_major = int(req_parts[0])
                req_minor = int(req_parts[1]) if len(req_parts) > 1 else 0
                if current_major < req_major or (current_major == req_major and current_minor < req_minor):
                    raise RuntimeError(f"Python version {version_str} does not meet minimum {min_version}")
                return {"status": "ok", "version": version_str}
            return {"status": "skipped", "reason": f"Language {language} not supported"}

        @register_action("create_directories")
        def create_directories(payload):
            paths = payload.get("paths", [])
            created = []
            for rel_path in paths:
                target = self._workspace_path(rel_path)
                target.mkdir(parents=True, exist_ok=True)
                try:
                    label = str(target.relative_to(ROOT))
                except ValueError:
                    label = str(target)
                created.append(label)
            return {"created": created}

        @register_action("run_health_check")
        def run_health_check(payload):
            endpoint = payload.get("endpoint", "")
            timeout_seconds = payload.get("timeout_seconds", 5)
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid protocol: {parsed.scheme}")
            allowed_hosts = self.config.get("security", {}).get("network_allowlist")
            if not isinstance(allowed_hosts, list) or not allowed_hosts:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED",
                    "security.network_allowlist must be a non-empty list",
                )
            host = parsed.hostname or ""
            if host not in allowed_hosts:
                raise ValueError(f"Host {host} not in allowlist")
            try:
                req = Request(endpoint, method="GET")
                with urlopen(req, timeout=timeout_seconds) as response:
                    status_code = response.status
                    if 200 <= status_code < 400:
                        return {"status": "healthy", "status_code": status_code}
                    raise RuntimeError(f"Health check returned {status_code}")
            except (URLError, HTTPError) as e:
                raise RuntimeError(f"Health check failed: {e}")

    def _log_event(self, event_type, data=None):
        entry = {"timestamp": _timestamp(), "event": event_type}
        entry.update(data or {})
        if DRY_RUN:
            entry["dry_run"] = True
        print(json.dumps(entry, default=str), flush=True)

    def _validate_payload(self, action_type, payload):
        if action_type not in ACTION_SCHEMAS:
            raise ValueError(f"Unknown action type: {action_type}")
        if not HAS_JSONSCHEMA:
            raise RuntimeError(
                "jsonschema is required for payload validation; "
                "run: python -m pip install -r requirements.txt"
            )
        schema = ACTION_SCHEMAS[action_type]
        try:
            validate(instance=payload, schema=schema)
        except SchemaValidationError as e:
            raise ValueError(f"Payload validation failed: {e.message}")

    def _schema_version_matches(self):
        try:
            row = self.conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error:
            self._log_event(
                "SCHEMA_VERSION_MISMATCH",
                {"expected": self.schema_version, "applied": None},
            )
            return False
        applied = row["version"] if row else None
        if applied is None or int(applied) != self.schema_version:
            self._log_event(
                "SCHEMA_VERSION_MISMATCH",
                {"expected": self.schema_version, "applied": applied},
            )
            return False
        return True

    def check(self):
        errors = []
        if yaml is None:
            errors.append(
                "PyYAML is not installed; run: python -m pip install -r requirements.txt"
            )
        if not HAS_JSONSCHEMA:
            errors.append(
                "jsonschema is not installed; run: python -m pip install -r requirements.txt"
            )
        if not self.workspace.exists():
            errors.append(f"workspace missing: {self.workspace} (run --init)")
        else:
            try:
                test_file = self.workspace / ".write_test"
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink()
            except OSError as exc:
                errors.append(f"workspace not writable: {exc}")

        pending = 0
        if not self.database_ready():
            errors.append(f"database not initialized: {self.db_path} (run --init)")
        elif not self._schema_version_matches():
            errors.append(f"schema version mismatch: expected {self.schema_version}")
        else:
            pending = self.conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE status='pending'"
            ).fetchone()["c"]

        if errors:
            print("CHECK FAILED")
            for error in errors:
                print(f" - {error}")
            return False

        self._log_event("SCHEMA_CHECK_OK", {"schema_version": self.schema_version})
        print("CHECK PASSED")
        print(f" - workspace: {self.workspace}")
        print(f" - db: {self.db_path}")
        print(f" - schema version: {self.schema_version}")
        print(f" - pending tasks: {pending}")
        return True

    def status(self, as_json=False):
        if not self.database_ready():
            message = (
                f"Database not initialized: {self.db_path}. "
                "Run: python agent.py --init"
            )
            if as_json:
                print(json.dumps({"error": message}))
            else:
                print(message)
            return False

        counts = {
            row["status"]: row["count"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            )
        }
        priorities = {
            row["priority"]: row["count"]
            for row in self.conn.execute(
                "SELECT priority, COUNT(*) AS count FROM tasks "
                "WHERE status='pending' GROUP BY priority"
            )
        }
        next_task = self.fetch_next_task()
        payload = {
            "database": str(self.db_path),
            "schema_version": self.schema_version,
            "status_counts": counts,
            "pending_by_priority": priorities,
            "next_task_id": next_task["id"] if next_task else None,
        }
        if as_json:
            print(json.dumps(payload))
            return True

        print(f"Database: {self.db_path}")
        print(f"Schema version: {self.schema_version}")
        print(
            "Status counts: "
            + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none")
        )
        print(
            "Pending by priority: "
            + (
                ", ".join(f"{key}={value}" for key, value in sorted(priorities.items()))
                or "none"
            )
        )
        print(f"Next task: {payload['next_task_id'] if payload['next_task_id'] is not None else 'none'}")
        return True

    def fetch_next_task(self):
        sql = "SELECT * FROM tasks WHERE status='pending' AND attempt_count < max_attempts ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, created_at ASC, id ASC LIMIT 1"
        return self.conn.execute(sql).fetchone()

    def recover_interrupted_tasks(self):
        cur = self.conn.execute(
            "SELECT id, attempt_count, max_attempts FROM tasks WHERE status='running'"
        )
        for task in cur.fetchall():
            task_id = task["id"]
            attempts = task["attempt_count"]
            max_attempts = task["max_attempts"]
            if attempts >= max_attempts:
                self.conn.execute(
                    "UPDATE tasks SET status='dead-lettered', "
                    "error_code='INTERRUPTED_MAX_ATTEMPTS_EXCEEDED', "
                    "failed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (task_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET status='pending', started_at=NULL, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task_id,),
                )
        self.conn.commit()

    def update_task(self, task_id, **fields):
        if DRY_RUN:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def run_once(self):
        task = self.fetch_next_task()
        if not task:
            print("No pending tasks")
            return False
        task_id = task["id"]
        action_type = task["action_type"]
        instruction = task["instruction"]
        attempt_count = task["attempt_count"]
        max_attempts = task["max_attempts"]
        print(f"[{task_id}] {action_type} ({task['priority']}) attempt {attempt_count+1}/{max_attempts}")
        self._log_event("EXEC_START", {"task_id": task_id, "action_type": action_type})
        if not DRY_RUN:
            self.conn.execute("UPDATE tasks SET status='running', started_at=CURRENT_TIMESTAMP, attempt_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (attempt_count + 1, task_id))
            self.conn.commit()
        try:
            payload = json.loads(instruction)
            self._validate_payload(action_type, payload)
            if "idempotency_key" not in payload:
                raise ValueError("Missing required field: idempotency_key")
            if action_type not in ACTION_REGISTRY:
                raise ValueError(f"Unknown action type: {action_type}")
            handler = ACTION_REGISTRY[action_type]
            result = handler(payload)
            self.update_task(task_id, status="completed", completed_at=datetime.utcnow().isoformat(), last_error=None)
            print(f"[{task_id}] completed: {result}")
            self._log_event("EXEC_END", {"task_id": task_id, "status": "completed", "result": result})
            return True
        except Exception as e:
            err = str(e)
            new_attempt_count = attempt_count + 1
            if new_attempt_count >= max_attempts:
                self.update_task(task_id, status="dead-lettered", failed_at=datetime.utcnow().isoformat(), last_error=err, error_message=err[:500])
                print(f"[{task_id}] dead-lettered: {err}")
                self._log_event("TASK_DEAD_LETTERED", {"task_id": task_id, "error": err})
            else:
                self.update_task(task_id, status="pending", last_error=err)
                print(f"[{task_id}] retryable failure: {err}")
                self._log_event("EXEC_END", {"task_id": task_id, "status": "retry", "error": err})
            return False

    def run_loop(self):
        self._log_event(
            "STARTUP",
            {
                "workspace": str(self.workspace),
                "db": str(self.db_path),
                "protocol_version": self.protocol_version,
            },
        )
        print(f"Agent started. DRY_RUN={DRY_RUN}. Ctrl-C to stop.")
        try:
            while True:
                ran = self.run_once()
                if not ran:
                    time.sleep(2)
        except KeyboardInterrupt:
            print("\nShutdown requested.")
            self._log_event("SHUTDOWN_REQUESTED", {})
            self._log_event("SHUTDOWN_COMPLETE", {})


def main():
    parser = argparse.ArgumentParser(description="SQLite Task Board Agent")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Validate configuration and database")
    actions.add_argument("--init", action="store_true", help="Apply migrations, seed tasks, create workspace dirs")
    actions.add_argument("--status", action="store_true", help="Show queue status")
    actions.add_argument("--once", action="store_true", help="Run one task")
    parser.add_argument("--json", action="store_true", help="Emit JSON for --status")
    parser.add_argument("--db", default=None, help="Path to tasks.db (overrides config)")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        emit_event("CONFIG_LOAD_FAILED", error_code=exc.code, error=str(exc))
        return 1

    if args.init:
        try:
            agent = Agent(args.db, config=config, auto_init=False)
            agent.initialize()
        except (ConfigError, OSError, sqlite3.Error) as exc:
            emit_event(
                "EXEC_ERROR",
                error_code=getattr(exc, "code", "INIT_FAILED"),
                error=str(exc),
            )
            return 1
        return 0

    db_path = configured_db_path(config, args.db)
    if args.check and not db_path.exists():
        print("CHECK FAILED")
        print(f" - database not initialized: {db_path} (run --init)")
        return 1

    if args.check:
        agent = Agent(args.db, config=config, auto_init=False)
        return 0 if agent.check() else 1

    if args.status:
        if not db_path.exists():
            message = f"Database not initialized: {db_path}. Run: python agent.py --init"
            if args.json:
                print(json.dumps({"error": message}))
            else:
                print(message)
            return 1
        agent = Agent(args.db, config=config, auto_init=False)
        return 0 if agent.status(as_json=args.json) else 1

    missing = []
    if yaml is None:
        missing.append("pyyaml")
    if not HAS_JSONSCHEMA:
        missing.append("jsonschema")
    if missing:
        emit_event(
            "CONFIG_LOAD_FAILED",
            error_code="DEPENDENCY_MISSING",
            error="Missing dependencies: " + ", ".join(missing)
            + "; run: python -m pip install -r requirements.txt",
        )
        return 1

    agent = Agent(args.db, config=config, auto_init=False)
    if not agent.database_ready():
        emit_event(
            "EXEC_ERROR",
            error_code="DATABASE_NOT_INITIALIZED",
            error="Database not initialized. Run: python agent.py --init",
        )
        return 1
    if not agent._schema_version_matches():
        return 1

    agent.recover_interrupted_tasks()
    if args.once:
        agent.run_once()
        return 0
    agent.run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
SQLite Task Board Agent

Local-first autonomous execution agent driven by AGENTS.md.
"""

import argparse
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    import yaml
except ImportError:
    yaml = None

try:
    from jsonschema import FormatChecker, validate
    from jsonschema import ValidationError as SchemaValidationError
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

ROOT = Path(__file__).parent.resolve()
DB_PATH = ROOT / "tasks.db"
WORKSPACE = ROOT / "workspace"
CONFIG_PATH = ROOT / "config.yaml"
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SCHEMA_VERSION = 1
PROTOCOL_VERSION = "3.1"
MAX_CAPTURED_OUTPUT = 10 * 1024
DRY_RUN = os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")

ERROR_CODES = frozenset({
    "CONFIG_LOAD_FAILED",
    "SCHEMA_VERSION_MISMATCH",
    "INVALID_JSON",
    "SCHEMA_VALIDATION_FAILED",
    "UNKNOWN_ACTION",
    "SECURITY_POLICY_VIOLATION",
    "PATH_OUTSIDE_WORKSPACE",
    "PATH_TRAVERSAL_ATTEMPT",
    "UNAPPROVED_HOST",
    "UNAPPROVED_PROTOCOL",
    "DEPENDENCY_MISSING",
    "PROCESS_TIMEOUT",
    "PROCESS_EXIT_NONZERO",
    "INTERRUPTED_MAX_ATTEMPTS_EXCEEDED",
    "MAX_ATTEMPTS_EXCEEDED",
    "FILE_NOT_FOUND",
    "PERMISSION_DENIED",
    "UNKNOWN_EXCEPTION",
    "HEALTH_CHECK_FAILED",
    "DATABASE_NOT_INITIALIZED",
    "SCHEMA_FILE_MISSING",
    "SEED_FILE_MISSING",
    "INIT_FAILED",
    "PYTEST_NOT_AVAILABLE",
})

EVENT_NAMES = frozenset({
    "CONFIG_LOAD_FAILED",
    "INIT_COMPLETE",
    "STARTUP",
    "SCHEMA_CHECK_OK",
    "SCHEMA_VERSION_MISMATCH",
    "EXEC_START",
    "EXEC_PLAN",
    "EXEC_END",
    "EXEC_ERROR",
    "TASK_DEAD_LETTERED",
    "SHUTDOWN_REQUESTED",
    "SHUTDOWN_COMPLETE",
})

SENSITIVE_KEY_RE = re.compile(r"(password|secret|token|credential|api_key)", re.IGNORECASE)
SECRET_TEXT_RE = re.compile(
    r"(?i)\b(password|secret|token|credential|api_key)\b\s*[=:]\s*(\S+)"
)


class ConfigError(RuntimeError):
    """Configuration or scaffold error carrying a stable error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TaskError(RuntimeError):
    """Task execution error carrying a documented, stable error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _info(message):
    """Write human-readable operator output to stderr, keeping stdout JSONL."""
    print(message, file=sys.stderr, flush=True)


def truncate_output(value, limit=MAX_CAPTURED_OUTPUT):
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _parse_version(value):
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def redact_text(value):
    return SECRET_TEXT_RE.sub(r"\1=[REDACTED]", str(value))


def redact(value):
    """Recursively redact values whose keys look secret-bearing."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def emit_event(event_type, **fields):
    """Emit one JSONL lifecycle event to stdout."""
    entry = {"timestamp": _timestamp(), "event": event_type}
    entry.update(redact(fields))
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

    unknown = set(config) - {"agent", "security"}
    if unknown:
        _info(f"WARNING: ignoring unknown top-level config keys: {', '.join(sorted(unknown))}")

    if not isinstance(config.get("agent"), dict):
        raise ConfigError("CONFIG_LOAD_FAILED", f"{config_path} must define an 'agent' mapping")
    try:
        schema_version = int(config["agent"].get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise ConfigError("CONFIG_LOAD_FAILED", "agent.schema_version must be an integer") from exc
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(
            "SCHEMA_VERSION_MISMATCH",
            f"agent.schema_version {schema_version} is not supported; expected {SCHEMA_VERSION}",
        )
    for key in ("workspace", "database"):
        value = config["agent"].get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError("CONFIG_LOAD_FAILED", f"agent.{key} must be a string")

    security = config.get("security")
    if security is not None and not isinstance(security, dict):
        raise ConfigError("CONFIG_LOAD_FAILED", "security must be a mapping")
    if isinstance(security, dict):
        allowlist = security.get("network_allowlist")
        if allowlist is not None:
            if (
                not isinstance(allowlist, list)
                or not allowlist
                or not all(isinstance(host, str) and host for host in allowlist)
            ):
                raise ConfigError(
                    "CONFIG_LOAD_FAILED",
                    "security.network_allowlist must be a list of non-empty host strings",
                )
    return config

ACTION_SCHEMAS = {
    "verify_runtime": {
        "type": "object",
        "required": ["action", "language", "min_version", "idempotency_key"],
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "const": "verify_runtime"},
            "language": {"type": "string", "enum": ["python"], "maxLength": 32},
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
            "paths": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
            },
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

class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects rather than following them to an unvalidated host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TaskError(
            "UNAPPROVED_HOST",
            f"Health check redirect to {newurl} rejected by network policy",
        )


class Agent:
    def __init__(self, db_path=None, config=None, auto_init=True):
        self.config = config if config is not None else load_config()
        agent_config = self.config.get("agent") or {}
        self.db_path = configured_db_path(self.config, db_path)
        self.workspace = resolve_path(agent_config.get("workspace"), WORKSPACE)
        self.schema_version = int(agent_config.get("schema_version", SCHEMA_VERSION))
        self.protocol_version = str(agent_config.get("protocol_version", PROTOCOL_VERSION))
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
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
        candidate = Path(relative)
        if candidate.is_absolute():
            raise TaskError(
                "PATH_OUTSIDE_WORKSPACE",
                f"Absolute path rejected: {relative}",
            )
        if ".." in candidate.parts:
            raise TaskError(
                "PATH_TRAVERSAL_ATTEMPT",
                f"Path traversal rejected: {relative}",
            )
        target = (base / candidate).resolve()
        if target != base and base not in target.parents:
            raise TaskError(
                "PATH_OUTSIDE_WORKSPACE",
                f"Path outside workspace rejected: {relative}",
            )
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

    def _validate_endpoint(self, endpoint):
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https"):
            raise TaskError("UNAPPROVED_PROTOCOL", f"Invalid protocol: {parsed.scheme}")
        allowed_hosts = self.config.get("security", {}).get("network_allowlist")
        if not isinstance(allowed_hosts, list) or not allowed_hosts:
            raise ConfigError(
                "CONFIG_LOAD_FAILED",
                "security.network_allowlist must be a non-empty list",
            )
        host = parsed.hostname or ""
        if host not in allowed_hosts:
            raise TaskError("UNAPPROVED_HOST", f"Host {host} not in allowlist")
        return parsed

    def _register_actions(self):
        @register_action("verify_runtime")
        def verify_runtime(payload):
            language = payload.get("language", "python")
            if language != "python":
                raise TaskError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"Runtime verification is not implemented for {language}",
                )
            min_version = payload.get("min_version", "3.11")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
            try:
                result = subprocess.run(
                    [sys.executable, "--version"],
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=10,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise TaskError("PROCESS_TIMEOUT", "Python version check timed out") from exc
            except OSError as exc:
                raise TaskError("UNKNOWN_EXCEPTION", f"Unable to run Python: {exc}") from exc
            if result.returncode != 0:
                detail = truncate_output(result.stderr or result.stdout)
                raise TaskError(
                    "PROCESS_EXIT_NONZERO",
                    f"Python version check exited {result.returncode}: {detail}",
                )
            version_str = (result.stdout or result.stderr).strip().replace("Python ", "")
            current = _parse_version(version_str)
            required = _parse_version(min_version)
            if current is None:
                raise TaskError(
                    "UNKNOWN_EXCEPTION",
                    f"Could not parse Python version output: {truncate_output(version_str)}",
                )
            if current < required:
                raise TaskError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"Python version {version_str} does not meet minimum {min_version}",
                )
            return {"status": "ok", "version": version_str}

        @register_action("create_directories")
        def create_directories(payload):
            paths = payload.get("paths", [])
            # Validate every path before creating anything to avoid partial output.
            targets = [(rel_path, self._workspace_path(rel_path)) for rel_path in paths]
            created = []
            for _, target in targets:
                target.mkdir(parents=True, exist_ok=True)
                try:
                    label = str(target.relative_to(self.workspace.resolve()))
                except ValueError:
                    label = str(target)
                created.append(label)
            return {"created": created}

        @register_action("run_health_check")
        def run_health_check(payload):
            endpoint = payload.get("endpoint", "")
            timeout_seconds = payload.get("timeout_seconds", 5)
            self._validate_endpoint(endpoint)
            opener = build_opener(_NoRedirectHandler())
            try:
                req = Request(endpoint, method="GET")
                with opener.open(req, timeout=timeout_seconds) as response:
                    status_code = response.status
                    if 200 <= status_code < 300:
                        return {"status": "healthy", "status_code": status_code}
                    raise TaskError(
                        "HEALTH_CHECK_FAILED",
                        f"Health check returned {status_code}",
                    )
            except TaskError:
                raise
            except HTTPError as exc:
                raise TaskError(
                    "HEALTH_CHECK_FAILED",
                    f"Health check returned {exc.code}",
                ) from exc
            except (URLError, socket.timeout, TimeoutError) as exc:
                raise TaskError("HEALTH_CHECK_FAILED", f"Health check failed: {exc}") from exc

    def _log_event(self, event_type, data=None):
        emit_event(event_type, **(data or {}))

    def _validate_payload(self, action_type, payload):
        if action_type not in ACTION_SCHEMAS:
            raise TaskError("UNKNOWN_ACTION", f"Unknown action type: {action_type}")
        if not HAS_JSONSCHEMA:
            raise TaskError(
                "DEPENDENCY_MISSING",
                "jsonschema is required for payload validation; "
                "run: python -m pip install -r requirements.txt",
            )
        schema = ACTION_SCHEMAS[action_type]
        try:
            validate(instance=payload, schema=schema, format_checker=FormatChecker())
        except SchemaValidationError as exc:
            raise TaskError(
                "SCHEMA_VALIDATION_FAILED",
                f"Payload validation failed: {exc.message}",
            ) from exc


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
        allowlist = (self.config.get("security") or {}).get("network_allowlist")
        if not isinstance(allowlist, list) or not allowlist:
            errors.append("security.network_allowlist must be a non-empty list")
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
        next_id = payload["next_task_id"]
        print(f"Next task: {next_id if next_id is not None else 'none'}")
        return True

    def fetch_next_task(self):
        sql = (
            "SELECT * FROM tasks WHERE status='pending' "
            "AND attempt_count < max_attempts "
            "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "created_at ASC, id ASC LIMIT 1"
        )
        return self.conn.execute(sql).fetchone()

    def claim_next_task(self):
        """Atomically select and claim one pending task."""
        if DRY_RUN:
            return self.fetch_next_task()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            task = self.fetch_next_task()
            if task is None:
                self.conn.rollback()
                return None
            cursor = self.conn.execute(
                "UPDATE tasks SET status='running', started_at=?, "
                "updated_at=?, attempt_count=attempt_count+1 "
                "WHERE id=? AND status='pending'",
                (_timestamp(), _timestamp(), task["id"]),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                return None
            self.conn.commit()
            return self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()
        except sqlite3.Error:
            self.conn.rollback()
            raise

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
                    "failed_at=?, updated_at=? WHERE id=?",
                    (_timestamp(), _timestamp(), task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET status='pending', started_at=NULL, "
                    "updated_at=? WHERE id=?",
                    (_timestamp(), task_id),
                )
        self.conn.commit()

    def update_task(self, task_id, **fields):
        if DRY_RUN:
            return
        fields["updated_at"] = _timestamp()
        sets = ", ".join(f"{key}=?" for key in fields)
        vals = list(fields.values()) + [task_id]
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def _plan_action(self, action_type, payload):
        """Validate an action and describe its effects without performing them."""
        if action_type == "verify_runtime":
            return {
                "language": payload["language"],
                "min_version": payload["min_version"],
            }
        if action_type == "create_directories":
            targets = [self._workspace_path(rel_path) for rel_path in payload["paths"]]
            planned = []
            for target in targets:
                try:
                    planned.append(str(target.relative_to(self.workspace.resolve())))
                except ValueError:
                    planned.append(str(target))
            return {"planned_directories": planned}
        if action_type == "run_health_check":
            parsed = self._validate_endpoint(payload["endpoint"])
            return {
                "endpoint": payload["endpoint"],
                "host": parsed.hostname,
                "timeout_seconds": payload["timeout_seconds"],
            }
        raise TaskError("UNKNOWN_ACTION", f"Unknown action type: {action_type}")

    def run_once(self):
        task = self.fetch_next_task() if DRY_RUN else self.claim_next_task()
        if not task:
            _info("No pending tasks")
            return None
        task_id = task["id"]
        action_type = task["action_type"]
        instruction = task["instruction"]
        attempt_count = int(task["attempt_count"]) + (1 if DRY_RUN else 0)
        max_attempts = int(task["max_attempts"])
        _info(
            f"[{task_id}] {action_type} ({task['priority']}) "
            f"attempt {attempt_count}/{max_attempts}"
        )
        self._log_event("EXEC_START", {"task_id": task_id, "action_type": action_type})

        try:
            try:
                payload = json.loads(instruction)
            except json.JSONDecodeError as exc:
                raise TaskError("INVALID_JSON", f"Instruction is not valid JSON: {exc}") from exc
            self._validate_payload(action_type, payload)
            if action_type not in ACTION_REGISTRY:
                raise TaskError("UNKNOWN_ACTION", f"Unknown action type: {action_type}")
            if DRY_RUN:
                plan = self._plan_action(action_type, payload)
                self._log_event(
                    "EXEC_PLAN",
                    {"task_id": task_id, "action_type": action_type, "plan": plan},
                )
                _info(f"[{task_id}] dry-run plan: {plan}")
                return True

            result = ACTION_REGISTRY[action_type](payload)
            self.update_task(
                task_id,
                status="completed",
                completed_at=_timestamp(),
                error_code=None,
                error_message=None,
                last_error=None,
            )
            _info(f"[{task_id}] completed: {result}")
            self._log_event(
                "EXEC_END",
                {"task_id": task_id, "status": "completed", "result": result},
            )
            return True
        except Exception as exc:
            code = getattr(exc, "code", None) or "UNKNOWN_EXCEPTION"
            err = truncate_output(redact_text(str(exc)))
            self._log_event(
                "EXEC_ERROR",
                {
                    "task_id": task_id,
                    "action_type": action_type,
                    "error_code": code,
                    "error": err,
                },
            )
            if DRY_RUN:
                _info(f"[{task_id}] dry-run failed: {err}")
                return False
            if attempt_count >= max_attempts:
                self.update_task(
                    task_id,
                    status="dead-lettered",
                    failed_at=_timestamp(),
                    error_code=code,
                    error_message=err[:500],
                    last_error=err,
                )
                _info(f"[{task_id}] dead-lettered: {err}")
                self._log_event(
                    "TASK_DEAD_LETTERED",
                    {"task_id": task_id, "error_code": code, "error": err},
                )
            else:
                self.update_task(
                    task_id,
                    status="pending",
                    started_at=None,
                    error_code=code,
                    error_message=err[:500],
                    last_error=err,
                )
                _info(f"[{task_id}] retryable failure: {err}")
                self._log_event(
                    "EXEC_END",
                    {
                        "task_id": task_id,
                        "status": "retry",
                        "error_code": code,
                        "error": err,
                    },
                )
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
        _info(f"Agent started. DRY_RUN={DRY_RUN}. Ctrl-C to stop.")

        def _handle_shutdown(signum, frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, _handle_shutdown)
        try:
            while True:
                ran = self.run_once()
                if ran is not True:
                    time.sleep(2)
        except KeyboardInterrupt:
            _info("Shutdown requested.")
            self._log_event("SHUTDOWN_REQUESTED", {})
            self._log_event("SHUTDOWN_COMPLETE", {})
        finally:
            signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description="SQLite Task Board Agent")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true",
                         help="Validate configuration and database")
    actions.add_argument("--init", action="store_true",
                         help="Apply migrations, seed tasks, create workspace dirs")
    actions.add_argument("--status", action="store_true", help="Show queue status")
    actions.add_argument("--once", action="store_true", help="Run one task")
    parser.add_argument("--json", action="store_true", help="Emit JSON for --status")
    parser.add_argument("--dry-run", action="store_true", help="Plan one task without side effects")
    parser.add_argument("--db", default=None, help="Path to tasks.db (overrides config)")
    args = parser.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run or os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")

    try:
        config = load_config()
    except ConfigError as exc:
        emit_event("CONFIG_LOAD_FAILED", error_code=exc.code, error=str(exc))
        return 1

    if args.init:
        try:
            agent = Agent(args.db, config=config, auto_init=False)
            agent.initialize()
        except (ConfigError, TaskError, OSError, sqlite3.Error) as exc:
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
        result = agent.run_once()
        if result is None:
            return 2
        return 0 if result else 1
    agent.run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

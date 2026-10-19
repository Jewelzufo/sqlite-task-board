#!/usr/bin/env python3
"""
SQLite Task Board Agent

Local-first autonomous execution agent driven by AGENTS.md.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
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
SCHEMA_VERSION = 3
PROTOCOL_VERSION = "3.2"
MAX_CAPTURED_OUTPUT = 10 * 1024
AUDIT_GENESIS = "0" * 64
DRY_RUN = os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")

DEFAULT_POLICY = {
    "allowed_actions": None,  # None means every registered action
    "allowed_path_roots": ["."],
    "max_payload_bytes": 65536,
}

DEFAULT_LIMITS = {
    "max_task_seconds": 60,
    "max_output_bytes": MAX_CAPTURED_OUTPUT,
    "max_tasks_per_window": 100,
    "max_failures_per_window": 10,
    "window_seconds": 60,
    "max_spawn_depth": 3,
}

ERROR_CODES = frozenset({
    "CONFIG_LOAD_FAILED",
    "SCHEMA_VERSION_MISMATCH",
    "INVALID_JSON",
    "SCHEMA_VALIDATION_FAILED",
    "UNKNOWN_ACTION",
    "SECURITY_POLICY_VIOLATION",
    "POLICY_DENIED",
    "IDEMPOTENCY_CONFLICT",
    "BUDGET_EXCEEDED",
    "CIRCUIT_OPEN",
    "LINEAGE_INVALID",
    "AUDIT_INTEGRITY_FAILED",
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
    "POLICY_DECISION",
    "EXEC_END",
    "EXEC_ERROR",
    "BUDGET_EXCEEDED",
    "CIRCUIT_OPEN",
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


def canonical_json(value):
    """Stable JSON encoding used for hashes and replay comparisons."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def payload_hash(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def policy_settings(config):
    raw = config.get("policy") or {}
    allowed = raw.get("allowed_actions")
    return {
        "allowed_actions": list(allowed) if allowed is not None else sorted(ACTION_SCHEMAS),
        "allowed_path_roots": list(
            raw.get("allowed_path_roots") or DEFAULT_POLICY["allowed_path_roots"]
        ),
        "max_payload_bytes": int(
            raw.get("max_payload_bytes", DEFAULT_POLICY["max_payload_bytes"])
        ),
    }


def limit_settings(config):
    raw = config.get("limits") or {}
    return {
        key: int(raw.get(key, default))
        for key, default in DEFAULT_LIMITS.items()
    }


def validate_payload(action_type, payload):
    """Validate one action payload against its strict schema."""
    if action_type not in ACTION_SCHEMAS:
        raise TaskError("UNKNOWN_ACTION", f"Unknown action type: {action_type}")
    if not HAS_JSONSCHEMA:
        raise TaskError(
            "DEPENDENCY_MISSING",
            "jsonschema is required for payload validation; "
            "run: python -m pip install -r requirements.txt",
        )
    try:
        validate(
            instance=payload,
            schema=ACTION_SCHEMAS[action_type],
            format_checker=FormatChecker(),
        )
    except SchemaValidationError as exc:
        raise TaskError(
            "SCHEMA_VALIDATION_FAILED",
            f"Payload validation failed: {exc.message}",
        ) from exc


def workspace_target(workspace, relative):
    """Resolve a task-relative path and reject escapes from the workspace."""
    base = Path(workspace).resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise TaskError("PATH_OUTSIDE_WORKSPACE", f"Absolute path rejected: {relative}")
    if ".." in candidate.parts:
        raise TaskError("PATH_TRAVERSAL_ATTEMPT", f"Path traversal rejected: {relative}")
    target = (base / candidate).resolve()
    if target != base and base not in target.parents:
        raise TaskError("PATH_OUTSIDE_WORKSPACE", f"Path outside workspace rejected: {relative}")
    return target


def validate_endpoint(endpoint, config):
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise TaskError("UNAPPROVED_PROTOCOL", f"Invalid protocol: {parsed.scheme}")
    allowed_hosts = (config.get("security") or {}).get("network_allowlist")
    if not isinstance(allowed_hosts, list) or not allowed_hosts:
        raise ConfigError(
            "CONFIG_LOAD_FAILED",
            "security.network_allowlist must be a non-empty list",
        )
    host = parsed.hostname or ""
    if host not in allowed_hosts:
        raise TaskError("UNAPPROVED_HOST", f"Host {host} not in allowlist")
    return parsed


def evaluate_policy(action_type, payload, config, workspace):
    """Return a deterministic allow/deny decision for one validated payload."""
    policy = policy_settings(config)

    def deny(rule_id, reason, error_code="POLICY_DENIED"):
        return {
            "allow": False,
            "rule_id": rule_id,
            "reason": reason,
            "error_code": error_code,
        }

    if action_type not in policy["allowed_actions"]:
        return deny("policy.allowed_actions", f"Action {action_type} is not allowed")
    size = len(canonical_json(payload).encode("utf-8"))
    if size > policy["max_payload_bytes"]:
        return deny(
            "policy.max_payload_bytes",
            f"Payload is {size} bytes; limit is {policy['max_payload_bytes']}",
        )

    base = Path(workspace).resolve()
    try:
        roots = [workspace_target(base, root) for root in policy["allowed_path_roots"]]
    except TaskError as exc:
        return deny("policy.allowed_path_roots", str(exc), exc.code)
    if action_type == "create_directories":
        for rel_path in payload.get("paths", []):
            try:
                target = workspace_target(base, rel_path)
            except TaskError as exc:
                return deny(f"policy.path.{exc.code}", str(exc), exc.code)
            if not any(target == root or root in target.parents for root in roots):
                return deny(
                    "policy.allowed_path_roots",
                    f"Path {rel_path} is outside the allowed roots",
                )
    if action_type == "run_health_check":
        try:
            validate_endpoint(payload.get("endpoint", ""), config)
        except TaskError as exc:
            return deny(f"policy.endpoint.{exc.code}", str(exc), exc.code)
    if action_type == "verify_runtime" and payload.get("language") != "python":
        return deny(
            "policy.runtime.language",
            f"Runtime verification is not implemented for {payload.get('language')}",
            "SCHEMA_VALIDATION_FAILED",
        )
    return {
        "allow": True,
        "rule_id": "policy.allow",
        "reason": "Payload satisfies the operating policy",
        "error_code": None,
    }


def plan_action(action_type, payload, workspace, config):
    """Describe an action's effects without performing them."""
    if action_type == "verify_runtime":
        return {
            "language": payload["language"],
            "min_version": payload["min_version"],
        }
    if action_type == "create_directories":
        targets = [workspace_target(workspace, rel_path) for rel_path in payload["paths"]]
        planned = []
        for target in targets:
            try:
                planned.append(str(target.relative_to(Path(workspace).resolve())))
            except ValueError:
                planned.append(str(target))
        return {"planned_directories": planned}
    if action_type == "run_health_check":
        parsed = validate_endpoint(payload["endpoint"], config)
        return {
            "endpoint": payload["endpoint"],
            "host": parsed.hostname,
            "timeout_seconds": payload["timeout_seconds"],
        }
    raise TaskError("UNKNOWN_ACTION", f"Unknown action type: {action_type}")


def replay_fixture(path, config=None):
    """Replay one recorded payload and compare it with the expected decision."""
    try:
        fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = {"ok": False, "error": f"Cannot read fixture: {exc}"}
        print(json.dumps(result, sort_keys=True))
        return False
    payload = fixture.get("payload")
    if not isinstance(payload, dict):
        result = {"ok": False, "error": "Fixture must contain a payload object"}
        print(json.dumps(result, sort_keys=True))
        return False
    action_type = fixture.get("action_type") or payload.get("action")
    config = config or {
        "agent": {},
        "security": {"network_allowlist": ["127.0.0.1", "localhost"]},
    }
    workspace = Path(
        fixture.get("workspace")
        or (config.get("agent") or {}).get("workspace")
        or "."
    )
    try:
        validate_payload(action_type, payload)
        decision = evaluate_policy(action_type, payload, config, workspace)
        if not decision["allow"]:
            result = {
                "decision": "deny",
                "error_code": decision["error_code"],
                "rule_id": decision["rule_id"],
            }
        else:
            result = {
                "decision": "allow",
                "plan": plan_action(action_type, payload, workspace, config),
            }
    except TaskError as exc:
        result = {"decision": "deny", "error_code": exc.code, "error": str(exc)}
    except Exception as exc:  # replay must never leak an unclassified crash
        result = {
            "decision": "deny",
            "error_code": "UNKNOWN_EXCEPTION",
            "error": str(exc),
        }
    expected = fixture.get("expect") or {}
    ok = all(result.get(key) == value for key, value in expected.items())
    print(json.dumps({"ok": ok, "result": result}, sort_keys=True))
    return ok


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

    unknown = set(config) - {"agent", "security", "policy", "limits"}
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

    policy = config.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            raise ConfigError("CONFIG_LOAD_FAILED", "policy must be a mapping")
        allowed_actions = policy.get("allowed_actions")
        if allowed_actions is not None:
            if (
                not isinstance(allowed_actions, list)
                or not allowed_actions
                or not all(isinstance(name, str) for name in allowed_actions)
            ):
                raise ConfigError(
                    "CONFIG_LOAD_FAILED",
                    "policy.allowed_actions must be a non-empty list of action names",
                )
            unknown_actions = sorted(set(allowed_actions) - set(ACTION_SCHEMAS))
            if unknown_actions:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED",
                    "policy.allowed_actions contains unknown actions: "
                    + ", ".join(unknown_actions),
                )
        roots = policy.get("allowed_path_roots")
        if roots is not None:
            if (
                not isinstance(roots, list)
                or not roots
                or not all(isinstance(root, str) and root for root in roots)
            ):
                raise ConfigError(
                    "CONFIG_LOAD_FAILED",
                    "policy.allowed_path_roots must be a non-empty list of relative paths",
                )
            for root in roots:
                if Path(root).is_absolute() or ".." in Path(root).parts:
                    raise ConfigError(
                        "CONFIG_LOAD_FAILED",
                        "policy.allowed_path_roots must stay inside the workspace",
                    )
        max_payload = policy.get("max_payload_bytes")
        if max_payload is not None:
            try:
                max_payload = int(max_payload)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED", "policy.max_payload_bytes must be an integer"
                ) from exc
            if max_payload < 1:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED", "policy.max_payload_bytes must be positive"
                )

    limits = config.get("limits")
    if limits is not None:
        if not isinstance(limits, dict):
            raise ConfigError("CONFIG_LOAD_FAILED", "limits must be a mapping")
        unknown_limits = sorted(set(limits) - set(DEFAULT_LIMITS))
        if unknown_limits:
            raise ConfigError(
                "CONFIG_LOAD_FAILED",
                "limits contains unknown keys: " + ", ".join(unknown_limits),
            )
        for key, value in limits.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED", f"limits.{key} must be an integer"
                ) from exc
            minimum = 0 if key == "max_spawn_depth" else 1
            if parsed < minimum:
                raise ConfigError(
                    "CONFIG_LOAD_FAILED", f"limits.{key} must be at least {minimum}"
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
        self.policy = policy_settings(self.config)
        self.limits = limit_settings(self.config)
        self.max_output_bytes = self.limits["max_output_bytes"]
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        if auto_init:
            self._init_db()
        self._register_actions()

    def _init_db(self):
        migration_dir = ROOT / "migrations"
        migration_files = sorted(migration_dir.glob("*.sql"))
        if not migration_files:
            raise ConfigError("SCHEMA_FILE_MISSING", f"No migrations found in {migration_dir}")
        applied = set()
        try:
            applied = {
                int(row["version"])
                for row in self.conn.execute("SELECT version FROM schema_migrations")
            }
        except sqlite3.Error:
            applied = set()
        for migration_file in migration_files:
            try:
                version = int(migration_file.name.split("_", 1)[0])
            except ValueError as exc:
                raise ConfigError(
                    "SCHEMA_FILE_MISSING", f"Invalid migration name: {migration_file.name}"
                ) from exc
            if version in applied:
                continue
            try:
                self.conn.executescript(migration_file.read_text(encoding="utf-8"))
                self.conn.commit()
            except sqlite3.Error as exc:
                raise ConfigError(
                    "INIT_FAILED", f"Migration {migration_file.name} failed: {exc}"
                ) from exc
            applied.add(version)

    def _workspace_path(self, relative):
        return workspace_target(self.workspace, relative)

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
        return validate_endpoint(endpoint, self.config)

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
            timeout_seconds = max(1, min(10, self.limits["max_task_seconds"]))
            try:
                result = subprocess.run(
                    [sys.executable, "--version"],
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=timeout_seconds,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise TaskError("PROCESS_TIMEOUT", "Python version check timed out") from exc
            except OSError as exc:
                raise TaskError("UNKNOWN_EXCEPTION", f"Unable to run Python: {exc}") from exc
            if result.returncode != 0:
                detail = truncate_output(result.stderr or result.stdout, self.max_output_bytes)
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
                    "Could not parse Python version output: "
                    + truncate_output(version_str, self.max_output_bytes),
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
            timeout_seconds = min(
                int(payload.get("timeout_seconds", 5)),
                max(1, self.limits["max_task_seconds"]),
            )
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
        """Emit JSONL and append the same entry to the tamper-evident audit chain."""
        entry = {"timestamp": _timestamp(), "event": event_type}
        entry.update(redact(data or {}))
        if DRY_RUN:
            entry["dry_run"] = True
        print(json.dumps(entry, default=str), flush=True)
        self._append_audit(entry)

    def _table_exists(self, name):
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def _append_audit(self, entry):
        if DRY_RUN or not self._table_exists("audit_log"):
            return
        payload_json = canonical_json(entry)
        started = not self.conn.in_transaction
        try:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["entry_hash"] if row else AUDIT_GENESIS
            entry_hash = hashlib.sha256(
                (prev_hash + payload_json).encode("utf-8")
            ).hexdigest()
            self.conn.execute(
                "INSERT INTO audit_log (timestamp, event, task_id, payload_json, "
                "prev_hash, entry_hash) VALUES (?,?,?,?,?,?)",
                (
                    entry["timestamp"],
                    entry["event"],
                    entry.get("task_id"),
                    payload_json,
                    prev_hash,
                    entry_hash,
                ),
            )
            if started:
                self.conn.commit()
        except sqlite3.Error as exc:
            if started:
                self.conn.rollback()
            raise TaskError(
                "AUDIT_INTEGRITY_FAILED", f"Unable to append audit entry: {exc}"
            ) from exc

    def audit_verify(self):
        """Recompute the audit hash chain and report the first broken link."""
        if not self._table_exists("audit_log"):
            return {"ok": False, "error": "audit_log table is missing", "entries": 0}
        prev_hash = AUDIT_GENESIS
        entries = 0
        for row in self.conn.execute("SELECT * FROM audit_log ORDER BY seq ASC"):
            entries += 1
            if row["prev_hash"] != prev_hash:
                return {
                    "ok": False,
                    "seq": row["seq"],
                    "error": "prev_hash mismatch",
                    "expected": prev_hash,
                    "actual": row["prev_hash"],
                    "entries": entries,
                }
            expected = hashlib.sha256(
                (prev_hash + row["payload_json"]).encode("utf-8")
            ).hexdigest()
            if row["entry_hash"] != expected:
                return {
                    "ok": False,
                    "seq": row["seq"],
                    "error": "entry_hash mismatch",
                    "expected": expected,
                    "actual": row["entry_hash"],
                    "entries": entries,
                }
            prev_hash = row["entry_hash"]
        return {"ok": True, "entries": entries, "head": prev_hash}

    def _validate_payload(self, action_type, payload):
        return validate_payload(action_type, payload)


    def _policy_decision(self, action_type, payload, task_id=None):
        decision = evaluate_policy(action_type, payload, self.config, self.workspace)
        self._log_event(
            "POLICY_DECISION",
            {
                "task_id": task_id,
                "action_type": action_type,
                "decision": "allow" if decision["allow"] else "deny",
                "rule_id": decision["rule_id"],
                "reason": decision["reason"],
            },
        )
        if not decision["allow"]:
            raise TaskError(
                decision["error_code"], f"{decision['rule_id']}: {decision['reason']}"
            )
        return decision

    def _ledger_entry(self, key):
        if not self._table_exists("idempotency_ledger"):
            return None
        return self.conn.execute(
            "SELECT * FROM idempotency_ledger WHERE key=?", (key,)
        ).fetchone()

    def _complete_with_ledger(self, task_id, key, action_type, digest, result):
        """Commit task completion and the idempotency record atomically."""
        if DRY_RUN:
            return "completed"
        result_json = canonical_json(result)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "UPDATE tasks SET status='completed', completed_at=?, updated_at=?, "
                "error_code=NULL, error_message=NULL, last_error=NULL WHERE id=?",
                (_timestamp(), _timestamp(), task_id),
            )
            self.conn.execute(
                "INSERT INTO idempotency_ledger (key, action_type, payload_hash, "
                "task_id, result_json, completed_at) VALUES (?,?,?,?,?,?)",
                (key, action_type, digest, task_id, result_json, _timestamp()),
            )
            self.conn.commit()
            return "completed"
        except sqlite3.IntegrityError:
            self.conn.rollback()
            existing = self._ledger_entry(key)
            if existing and existing["payload_hash"] == digest:
                self.update_task(
                    task_id,
                    status="completed",
                    completed_at=_timestamp(),
                    error_code=None,
                    error_message=None,
                    last_error=None,
                )
                return "replayed"
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} was already used with a different payload",
            ) from None
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def _window_cutoff(self):
        seconds = self.limits["window_seconds"]
        return (
            datetime.now(timezone.utc) - timedelta(seconds=seconds)
        ).isoformat().replace("+00:00", "Z")

    def _failures_in_window(self):
        cutoff = self._window_cutoff()
        if self._table_exists("audit_log"):
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM audit_log WHERE event='EXEC_ERROR' "
                "AND timestamp >= ?",
                (cutoff,),
            ).fetchone()
            return int(row["c"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE error_code IS NOT NULL "
            "AND updated_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["c"])

    def _check_governor(self):
        if DRY_RUN:
            return True
        failures = self._failures_in_window()
        if failures >= self.limits["max_failures_per_window"]:
            self._log_event(
                "CIRCUIT_OPEN",
                {
                    "failures": failures,
                    "limit": self.limits["max_failures_per_window"],
                    "window_seconds": self.limits["window_seconds"],
                },
            )
            _info("Circuit breaker open; refusing to claim more work")
            return False
        cutoff = self._window_cutoff()
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE started_at IS NOT NULL "
            "AND started_at >= ?",
            (cutoff,),
        ).fetchone()
        if int(row["c"]) >= self.limits["max_tasks_per_window"]:
            self._log_event(
                "BUDGET_EXCEEDED",
                {
                    "started_tasks": int(row["c"]),
                    "limit": self.limits["max_tasks_per_window"],
                    "window_seconds": self.limits["window_seconds"],
                },
            )
            _info("Task budget exhausted for this window")
            return False
        return True


    def _existing_enqueue_record(self, existing, action_type, digest, key):
        """Return an idempotent enqueue response or reject a payload mismatch."""
        try:
            previous_payload = json.loads(existing["instruction"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} has an unreadable stored payload",
            ) from exc
        if existing["action_type"] != action_type or payload_hash(previous_payload) != digest:
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} is already used with a different action or payload",
            )

        ledger = self._ledger_entry(key)
        if ledger is None:
            return {
                "task_id": int(existing["id"]),
                "status": existing["status"],
                "idempotent_replay": False,
                "result": None,
            }
        if ledger["action_type"] != action_type or ledger["payload_hash"] != digest:
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} conflicts with its stored result",
            )
        try:
            stored_result = json.loads(ledger["result_json"])
        except json.JSONDecodeError as exc:
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} has an unreadable stored result",
            ) from exc
        return {
            "task_id": int(existing["id"]),
            "status": existing["status"],
            "idempotent_replay": True,
            "result": stored_result,
        }

    def enqueue_task_record(
        self,
        action_type,
        payload,
        priority="medium",
        max_attempts=3,
        parent_task_id=None,
    ):
        """Validate and enqueue one task, returning replay metadata.

        Enqueueing an existing key with the same action and payload returns the
        existing task instead of inserting a duplicate. If that task already has a
        stored successful result, the response includes the result without executing
        the action again.
        """
        if priority not in PRIORITY_ORDER:
            raise TaskError("SCHEMA_VALIDATION_FAILED", f"Invalid priority: {priority}")
        if int(max_attempts) < 1:
            raise TaskError("SCHEMA_VALIDATION_FAILED", "max_attempts must be positive")
        validate_payload(action_type, payload)
        decision = evaluate_policy(action_type, payload, self.config, self.workspace)
        if not decision["allow"]:
            raise TaskError(
                decision["error_code"], f"{decision['rule_id']}: {decision['reason']}"
            )
        depth = 0
        if parent_task_id is not None:
            parent = self.conn.execute(
                "SELECT id, depth FROM tasks WHERE id=?", (parent_task_id,)
            ).fetchone()
            if parent is None:
                raise TaskError(
                    "LINEAGE_INVALID", f"Parent task {parent_task_id} does not exist"
                )
            depth = int(parent["depth"]) + 1
            if depth > self.limits["max_spawn_depth"]:
                raise TaskError(
                    "LINEAGE_INVALID",
                    f"Task depth {depth} exceeds limit {self.limits['max_spawn_depth']}",
                )

        key = payload["idempotency_key"]
        digest = payload_hash(payload)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT id, action_type, instruction, status FROM tasks "
                "WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                record = self._existing_enqueue_record(existing, action_type, digest, key)
                self.conn.commit()
                return record
            cursor = self.conn.execute(
                "INSERT INTO tasks (instruction, action_type, idempotency_key, "
                "priority, max_attempts, parent_task_id, depth) VALUES (?,?,?,?,?,?,?)",
                (
                    canonical_json(payload),
                    action_type,
                    key,
                    priority,
                    int(max_attempts),
                    parent_task_id,
                    depth,
                ),
            )
            task_id = int(cursor.lastrowid)
            self.conn.commit()
        except TaskError:
            self.conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise TaskError(
                "IDEMPOTENCY_CONFLICT",
                f"Idempotency key {key} is already used with a different action or payload",
            ) from exc
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return {
            "task_id": task_id,
            "status": "pending",
            "idempotent_replay": False,
            "result": None,
        }

    def enqueue_task(
        self,
        action_type,
        payload,
        priority="medium",
        max_attempts=3,
        parent_task_id=None,
    ):
        """Backward-compatible enqueue that returns only the task id."""
        return self.enqueue_task_record(
            action_type,
            payload,
            priority=priority,
            max_attempts=max_attempts,
            parent_task_id=parent_task_id,
        )["task_id"]

    def graph(self, fmt="dot"):
        """Emit task lineage as DOT or JSON."""
        rows = self.conn.execute(
            "SELECT id, action_type, status, priority, parent_task_id, depth "
            "FROM tasks ORDER BY id"
        ).fetchall()
        nodes = [
            {
                "id": row["id"],
                "action_type": row["action_type"],
                "status": row["status"],
                "priority": row["priority"],
                "parent_task_id": row["parent_task_id"],
                "depth": row["depth"],
            }
            for row in rows
        ]
        edges = [
            {"from": row["parent_task_id"], "to": row["id"]}
            for row in rows
            if row["parent_task_id"] is not None
        ]
        if fmt == "json":
            print(json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True))
            return {"nodes": nodes, "edges": edges}
        if fmt != "dot":
            raise TaskError("SCHEMA_VALIDATION_FAILED", f"Unsupported graph format: {fmt}")
        print("digraph tasks {")
        print("  rankdir=LR;")
        for node in nodes:
            label = f"{node['id']} {node['action_type']} [{node['status']}]"
            label = label.replace("\\", "\\\\").replace('"', '\\"')
            print(f'  t{node["id"]} [label="{label}"];')
        for edge in edges:
            print(f'  t{edge["from"]} -> t{edge["to"]};')
        print("}")
        return {"nodes": nodes, "edges": edges}


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
            for table in ("idempotency_ledger", "audit_log"):
                if not self._table_exists(table):
                    errors.append(f"required table missing: {table} (run --init)")

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
            "circuit_open": self._failures_in_window()
            >= self.limits["max_failures_per_window"],
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
        return plan_action(action_type, payload, self.workspace, self.config)

    def run_once(self):
        if not self._check_governor():
            return False
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
            self._policy_decision(action_type, payload, task_id=task_id)

            key = payload["idempotency_key"]
            digest = payload_hash(payload)
            existing = self._ledger_entry(key)
            if existing:
                if existing["action_type"] != action_type or existing["payload_hash"] != digest:
                    raise TaskError(
                        "IDEMPOTENCY_CONFLICT",
                        f"Idempotency key {key} was already used with a different payload",
                    )
                replay_result = json.loads(existing["result_json"])
                if not DRY_RUN:
                    self.update_task(
                        task_id,
                        status="completed",
                        completed_at=_timestamp(),
                        error_code=None,
                        error_message=None,
                        last_error=None,
                    )
                    _info(f"[{task_id}] replayed idempotent result: {replay_result}")
                    self._log_event(
                        "EXEC_END",
                        {
                            "task_id": task_id,
                            "status": "completed",
                            "result": replay_result,
                            "idempotent_replay": True,
                        },
                    )
                    return True
            if DRY_RUN:
                plan = self._plan_action(action_type, payload)
                if existing:
                    plan["idempotent_replay"] = True
                self._log_event(
                    "EXEC_PLAN",
                    {"task_id": task_id, "action_type": action_type, "plan": plan},
                )
                _info(f"[{task_id}] dry-run plan: {plan}")
                return True

            started = time.monotonic()
            result = ACTION_REGISTRY[action_type](payload)
            elapsed = time.monotonic() - started
            if elapsed > self.limits["max_task_seconds"]:
                raise TaskError(
                    "BUDGET_EXCEEDED",
                    f"Task ran {elapsed:.3f}s; limit is {self.limits['max_task_seconds']}s",
                )
            completion = self._complete_with_ledger(
                task_id, key, action_type, digest, result
            )
            _info(f"[{task_id}] completed: {result}")
            self._log_event(
                "EXEC_END",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "result": result,
                    "idempotent_replay": completion == "replayed",
                },
            )
            return True
        except Exception as exc:
            code = getattr(exc, "code", None) or "UNKNOWN_EXCEPTION"
            err = truncate_output(redact_text(str(exc)), self.max_output_bytes)
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
            non_retriable = {
                "IDEMPOTENCY_CONFLICT",
                "POLICY_DENIED",
                "LINEAGE_INVALID",
                "BUDGET_EXCEEDED",
            }
            if attempt_count >= max_attempts or code in non_retriable:
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
    actions.add_argument("--audit-verify", action="store_true",
                         help="Verify the hash-chained audit log")
    actions.add_argument("--graph", action="store_true", help="Emit task lineage")
    actions.add_argument("--enqueue", metavar="FILE",
                         help="Enqueue a JSON task payload from a file")
    actions.add_argument("--replay", metavar="FILE",
                         help="Replay a fixture without side effects")
    parser.add_argument("--json", action="store_true", help="Emit JSON for --status")
    parser.add_argument("--dry-run", action="store_true", help="Plan one task without side effects")
    parser.add_argument("--db", default=None, help="Path to tasks.db (overrides config)")
    parser.add_argument("--format", choices=("dot", "json"), default="dot",
                        help="Graph output format")
    parser.add_argument("--priority", choices=tuple(PRIORITY_ORDER), default="medium",
                        help="Priority for --enqueue")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="Attempt limit for --enqueue")
    parser.add_argument("--parent", type=int, default=None,
                        help="Parent task id for --enqueue")
    args = parser.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run or os.getenv("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes")

    try:
        config = load_config()
    except ConfigError as exc:
        emit_event("CONFIG_LOAD_FAILED", error_code=exc.code, error=str(exc))
        return 1

    if args.replay:
        return 0 if replay_fixture(args.replay, config) else 1

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
    if args.enqueue:
        if not db_path.exists():
            emit_event(
                "EXEC_ERROR",
                error_code="DATABASE_NOT_INITIALIZED",
                error="Database not initialized. Run: python agent.py --init",
            )
            return 1
        agent = Agent(args.db, config=config, auto_init=False)
        try:
            spec = json.loads(Path(args.enqueue).read_text(encoding="utf-8"))
            payload = spec.get("payload", spec) if isinstance(spec, dict) else spec
            if not isinstance(payload, dict):
                raise TaskError("SCHEMA_VALIDATION_FAILED", "Enqueue payload must be an object")
            action_type = (
                spec.get("action_type") if isinstance(spec, dict) else None
            ) or payload.get("action")
            record = agent.enqueue_task_record(
                action_type,
                payload,
                priority=(spec.get("priority", args.priority)
                          if isinstance(spec, dict) else args.priority),
                max_attempts=(spec.get("max_attempts", args.max_attempts)
                              if isinstance(spec, dict) else args.max_attempts),
                parent_task_id=(spec.get("parent_task_id", args.parent)
                                if isinstance(spec, dict) else args.parent),
            )
        except (OSError, json.JSONDecodeError, TaskError) as exc:
            emit_event(
                "EXEC_ERROR",
                error_code=getattr(exc, "code", "INVALID_JSON"),
                error=str(exc),
            )
            return 1
        finally:
            agent.conn.close()
        print(json.dumps(record, sort_keys=True))
        return 0

    if args.audit_verify or args.graph:
        if not db_path.exists():
            emit_event(
                "EXEC_ERROR",
                error_code="DATABASE_NOT_INITIALIZED",
                error="Database not initialized. Run: python agent.py --init",
            )
            return 1
        agent = Agent(args.db, config=config, auto_init=False)
        try:
            if args.audit_verify:
                result = agent.audit_verify()
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 1
            agent.graph(fmt=args.format)
            return 0
        except TaskError as exc:
            emit_event("EXEC_ERROR", error_code=exc.code, error=str(exc))
            return 1
        finally:
            agent.conn.close()

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

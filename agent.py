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
    def __init__(self, db_path=None, config=None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.config = config or self._load_config()
        self.workspace = Path(self.config.get("agent", {}).get("workspace", str(WORKSPACE)))
        if not self.workspace.is_absolute():
            self.workspace = ROOT / self.workspace
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._register_actions()

    def _load_config(self):
        if CONFIG_PATH.exists():
            if yaml is None:
                raise RuntimeError("PyYAML required")
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _init_db(self):
        migration_file = ROOT / "migrations" / "0001_initial.sql"
        if migration_file.exists():
            self.conn.executescript(migration_file.read_text())
        else:
            self.conn.executescript("""CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, instruction TEXT NOT NULL, action_type TEXT NOT NULL, idempotency_key TEXT UNIQUE, status TEXT DEFAULT 'pending', priority TEXT DEFAULT 'medium', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, attempt_count INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, last_error TEXT); CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);""")
        self.conn.commit()

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
                target = (self.workspace / rel_path).resolve()
                if not str(target).startswith(str(self.workspace.resolve())):
                    raise ValueError(f"Path traversal rejected: {rel_path}")
                target.mkdir(parents=True, exist_ok=True)
                created.append(str(target.relative_to(ROOT)))
            return {"created": created}

        @register_action("run_health_check")
        def run_health_check(payload):
            endpoint = payload.get("endpoint", "")
            timeout_seconds = payload.get("timeout_seconds", 5)
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid protocol: {parsed.scheme}")
            allowed_hosts = self.config.get("security", {}).get("network_allowlist", ["127.0.0.1", "localhost"])
            host = parsed.hostname or ""
            if host not in allowed_hosts and host not in ("127.0.0.1", "localhost"):
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
        log_entry = {"timestamp": datetime.utcnow().isoformat() + "Z", "event": event_type, "data": data or {}}
        print(json.dumps(log_entry), flush=True)

    def _validate_payload(self, action_type, payload):
        if not HAS_JSONSCHEMA or action_type not in ACTION_SCHEMAS:
            return
        schema = ACTION_SCHEMAS[action_type]
        try:
            validate(instance=payload, schema=schema)
        except SchemaValidationError as e:
            raise ValueError(f"Payload validation failed: {e.message}")

    def check(self):
        errors = []
        try:
            self.workspace.mkdir(exist_ok=True)
            test_file = self.workspace / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as e:
            errors.append(f"workspace not writable: {e}")
        try:
            cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
            if not cur.fetchone():
                errors.append("tasks table missing")
        except Exception as e:
            errors.append(f"db error: {e}")
        pending = 0
        try:
            pending = self.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status='pending'").fetchone()["c"]
        except:
            pass
        if errors:
            print("CHECK FAILED")
            for e in errors:
                print(f" - {e}")
            return False
        print("CHECK PASSED")
        print(f" - workspace: {self.workspace}")
        print(f" - db: {self.db_path}")
        print(f" - pending tasks: {pending}")
        return True

    def fetch_next_task(self):
        sql = "SELECT * FROM tasks WHERE status='pending' AND attempt_count < max_attempts ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, created_at ASC, id ASC LIMIT 1"
        return self.conn.execute(sql).fetchone()

    def recover_interrupted_tasks(self):
        cur = self.conn.execute("SELECT id, attempt_count, max_attempts FROM tasks WHERE status='running'")
        for task in cur.fetchall():
            task_id = task["id"]
            attempts = task["attempt_count"]
            max_attempts = task["max_attempts"]
            if attempts >= max_attempts:
                self.conn.execute("UPDATE tasks SET status='dead-lettered', failed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            else:
                self.conn.execute("UPDATE tasks SET status='pending', updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
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
        self._log_event("STARTUP", {"workspace": str(self.workspace), "db": str(self.db_path)})
        print(f"Agent started. DRY_RUN={DRY_RUN}. Ctrl-C to stop.")
        while True:
            ran = self.run_once()
            if not ran:
                time.sleep(2)

def main():
    parser = argparse.ArgumentParser(description="SQLite Task Board Agent")
    parser.add_argument("--check", action="store_true", help="Validate configuration")
    parser.add_argument("--once", action="store_true", help="Run one task")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to tasks.db")
    args = parser.parse_args()
    agent = Agent(args.db)
    agent.recover_interrupted_tasks()
    if args.check:
        ok = agent.check()
        sys.exit(0 if ok else 1)
    elif args.once:
        agent.run_once()
    else:
        agent.run_loop()

if __name__ == "__main__":
    main()

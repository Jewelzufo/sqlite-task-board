"""
test_agent_contract.py

Contract and behavior tests for the SQLite Task Board agent.

Validates:
- AGENTS.md exists
- opencode.json exists and references AGENTS.md
- config.yaml exists and uses the canonical schema
- migrations/0001_initial.sql exists and creates tasks and schema_migrations
- seeds/bootstrap_tasks.sql exists and inserts bootstrap tasks
- agent.py exists and contains ACTION_REGISTRY
- requirements.txt exists
- init/seed idempotency, schema mismatch, config failure, and CLI init
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

def test_agents_md_exists():
    """AGENTS.md must exist."""
    agents_file = ROOT / "AGENTS.md"
    assert agents_file.exists(), "AGENTS.md must exist"

def test_opencode_json_exists_and_references_agents():
    """opencode.json must exist and reference AGENTS.md."""
    opencode_file = ROOT / "opencode.json"
    assert opencode_file.exists(), "opencode.json must exist"
    
    content = json.loads(opencode_file.read_text())
    assert "instructions" in content, "opencode.json must have instructions field"
    assert "AGENTS.md" in content["instructions"], "opencode.json must reference AGENTS.md"

def test_config_yaml_exists():
    """config.yaml must exist."""
    config_file = ROOT / "config.yaml"
    assert config_file.exists(), "config.yaml must exist"

def test_migrations_initial_sql_exists():
    """migrations/0001_initial.sql must exist and create tasks and schema_migrations tables."""
    migration_file = ROOT / "migrations" / "0001_initial.sql"
    assert migration_file.exists(), "migrations/0001_initial.sql must exist"
    
    content = migration_file.read_text()
    assert "CREATE TABLE" in content and "tasks" in content, "Migration must create tasks table"
    assert "schema_migrations" in content, "Migration must create schema_migrations table"

def test_bootstrap_tasks_sql_exists():
    """seeds/bootstrap_tasks.sql must exist and insert bootstrap tasks."""
    seed_file = ROOT / "seeds" / "bootstrap_tasks.sql"
    assert seed_file.exists(), "seeds/bootstrap_tasks.sql must exist"
    
    content = seed_file.read_text()
    assert "INSERT" in content, "Seed file must contain INSERT statements"

def test_agent_py_exists_and_contains_action_registry():
    """agent.py must exist and contain ACTION_REGISTRY."""
    agent_file = ROOT / "agent.py"
    assert agent_file.exists(), "agent.py must exist"
    
    content = agent_file.read_text()
    assert "ACTION_REGISTRY" in content, "agent.py must contain ACTION_REGISTRY"

def test_requirements_txt_exists():
    """requirements.txt must exist."""
    req_file = ROOT / "requirements.txt"
    assert req_file.exists(), "requirements.txt must exist"


def setup_agent(tmpdir):
    """Set up an isolated agent with a temporary database and workspace."""
    from agent import Agent

    workspace = Path(tmpdir) / "workspace"
    workspace.mkdir(exist_ok=True)
    db_path = Path(tmpdir) / "test_tasks.db"
    config = {
        "agent": {
            "workspace": str(workspace),
            "database": str(db_path),
            "schema_version": 3,
            "protocol_version": "3.2",
        },
        "security": {"network_allowlist": ["127.0.0.1", "localhost"]},
    }
    return Agent(db_path, config=config)


def test_config_yaml_uses_canonical_schema():
    """Every key in config.yaml must be one the runtime reads."""
    import yaml

    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert set(config) == {"agent", "security", "policy", "limits"}
    assert set(config["agent"]) == {
        "workspace",
        "database",
        "schema_version",
        "protocol_version",
    }
    assert config["security"]["network_allowlist"]
    assert set(config["policy"]) == {
        "allowed_actions",
        "allowed_path_roots",
        "max_payload_bytes",
    }
    assert set(config["limits"]) == {
        "max_task_seconds",
        "max_output_bytes",
        "max_tasks_per_window",
        "max_failures_per_window",
        "window_seconds",
        "max_spawn_depth",
    }


def test_schema_creates_tasks_table():
    """Tasks table should be created."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        cur = agent.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        assert cur.fetchone() is not None, "tasks table should exist"


def test_priority_ordering():
    """Critical priority tasks should be fetched first."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        # Insert tasks in reverse priority
        for prio in ["low", "medium", "high", "critical"]:
            agent.conn.execute(
                "INSERT INTO tasks (instruction, action_type, idempotency_key, priority) VALUES (?,?,?,?)",
                (json.dumps({"action": "health_check", "idempotency_key": f"test-{prio}"}), "verify_runtime", f"test-{prio}", prio)
            )
        agent.conn.commit()
        task = agent.fetch_next_task()
        assert task["priority"] == "critical", "critical should be fetched first"


def test_lifecycle_pending_to_completed():
    """Task should transition from pending to completed."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        agent.conn.execute(
            "INSERT INTO tasks (instruction, action_type, idempotency_key, priority) VALUES (?,?,?,?)",
            (json.dumps({"action": "verify_runtime", "language": "python", "min_version": "3.0", "idempotency_key": "test-lifecycle"}), "verify_runtime", "test-lifecycle", "high")
        )
        agent.conn.commit()
        assert agent.run_once() is True
        status = agent.conn.execute("SELECT status FROM tasks").fetchone()[0]
        assert status == "completed"


def test_file_write_security():
    """Path traversal should be rejected - tested via create_directories action."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        
        # Test path traversal rejection in create_directories
        agent.conn.execute(
            "INSERT INTO tasks (instruction, action_type, idempotency_key) VALUES (?,?,?)",
            (json.dumps({"action": "create_directories", "paths": ["../escape"], "idempotency_key": "test-escape"}), "create_directories", "test-escape")
        )
        agent.conn.commit()

        agent.run_once()  # should fail due to path traversal

        # Check result - should be pending (retry) or dead-lettered
        row = agent.conn.execute("SELECT status, last_error FROM tasks").fetchone()
        assert row["status"] in ("pending", "dead-lettered")
        assert "traversal" in (row["last_error"] or "").lower()


def test_retry_then_dead_letter():
    """Task should be dead-lettered after max attempts."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        # Task that will always fail (unknown type)
        agent.conn.execute(
            "INSERT INTO tasks (instruction, action_type, idempotency_key, max_attempts) VALUES (?,?,?,?)",
            (json.dumps({"action": "unknown", "idempotency_key": "test-unknown"}), "unknown_task", "test-unknown", 2)
        )
        agent.conn.commit()

        agent.run_once()  # attempt 1 -> pending
        agent.run_once()  # attempt 2 -> dead-lettered

        status = agent.conn.execute("SELECT status, attempt_count FROM tasks").fetchone()
        assert status["status"] == "dead-lettered"
        assert status["attempt_count"] == 2


def test_check_passes():
    """Check should pass with valid configuration."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        assert agent.check() is True


def test_action_registry_matches_documented_surface():
    """Only the three documented actions should be registered."""
    import agent as agent_module

    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        assert set(agent_module.ACTION_REGISTRY) == {
            "verify_runtime",
            "create_directories",
            "run_health_check",
        }
        agent.conn.close()


def test_init_seeds_once_and_creates_workspace():
    """Repeated init must not duplicate seed tasks and must create dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = setup_agent(tmp)
        first = agent.initialize()
        second = agent.initialize()

        assert first["tasks"] == 3
        assert second["tasks"] == 3
        pending = agent.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='pending'"
        ).fetchone()[0]
        assert pending == 3
        for name in ("logs", "data", "tmp"):
            assert (agent.workspace / name).is_dir()
        agent.conn.close()


def test_schema_version_mismatch():
    """A configured schema version that differs from the DB must fail check."""
    from agent import Agent

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        db_path = Path(tmp) / "mismatch.db"
        config = {
            "agent": {
                "workspace": str(workspace),
                "database": str(db_path),
                "schema_version": 99,
            },
            "security": {"network_allowlist": ["127.0.0.1"]},
        }
        agent = Agent(db_path, config=config)
        assert agent.check() is False
        agent.conn.close()


def test_config_load_failure_is_structured():
    """Missing PyYAML must raise ConfigError, not a bare RuntimeError."""
    import agent as agent_module

    original = agent_module.yaml
    agent_module.yaml = None
    try:
        try:
            agent_module.load_config()
        except agent_module.ConfigError as exc:
            assert exc.code == "CONFIG_LOAD_FAILED"
            assert "PyYAML" in str(exc)
        else:
            raise AssertionError("load_config should fail without PyYAML")
    finally:
        agent_module.yaml = original


def test_agent_config_env_override():
    """AGENT_CONFIG must select an alternate configuration file."""
    import agent as agent_module

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "custom.yaml"
        config_path.write_text(
            "agent:\n  workspace: ./workspace\n  database: ./custom.db\n",
            encoding="utf-8",
        )
        original = os.environ.get("AGENT_CONFIG")
        os.environ["AGENT_CONFIG"] = str(config_path)
        try:
            config = agent_module.load_config()
        finally:
            if original is None:
                os.environ.pop("AGENT_CONFIG", None)
            else:
                os.environ["AGENT_CONFIG"] = original
        assert config["agent"]["database"] == "./custom.db"


def test_init_cli_seeds_database():
    """The --init command must create the DB and seed exactly three tasks."""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        db_path = Path(tmp) / "cli.db"
        config_path = Path(tmp) / "config.yaml"
        config_path.write_text(
            "agent:\n"
            f'  workspace: "{workspace}"\n'
            f'  database: "{db_path}"\n'
            "  schema_version: 3\n"
            '  protocol_version: "3.2"\n'
            "security:\n"
            '  network_allowlist: ["127.0.0.1", "localhost"]\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["AGENT_CONFIG"] = str(config_path)
        result = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "--init"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        assert count == 3



def test_check_cli_reports_uninitialized_without_creating_db():
    """--check must fail clearly and must not create an empty database."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        db_path = Path(tmp) / "missing.db"
        config_path = Path(tmp) / "config.yaml"
        config_path.write_text(
            "agent:\n"
            f'  workspace: "{workspace}"\n'
            f'  database: "{db_path}"\n'
            "  schema_version: 3\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["AGENT_CONFIG"] = str(config_path)
        result = subprocess.run(
            [sys.executable, str(ROOT / "agent.py"), "--check"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "run --init" in result.stdout
        assert not db_path.exists(), "check must not create a database"



if __name__ == "__main__":
    # Simple runner without pytest
    tests = [
        test_agents_md_exists,
        test_opencode_json_exists_and_references_agents,
        test_config_yaml_exists,
        test_config_yaml_uses_canonical_schema,
        test_migrations_initial_sql_exists,
        test_bootstrap_tasks_sql_exists,
        test_agent_py_exists_and_contains_action_registry,
        test_requirements_txt_exists,
        test_schema_creates_tasks_table,
        test_priority_ordering,
        test_lifecycle_pending_to_completed,
        test_file_write_security,
        test_retry_then_dead_letter,
        test_check_passes,
        test_action_registry_matches_documented_surface,
        test_init_seeds_once_and_creates_workspace,
        test_schema_version_mismatch,
        test_config_load_failure_is_structured,
        test_agent_config_env_override,
        test_init_cli_seeds_database,
        test_check_cli_reports_uninitialized_without_creating_db,
    ]
    failed = False
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed = True
        except Exception as e:
            print(f"✗ {test.__name__}: {type(e).__name__}: {e}")
            failed = True
    
    if failed:
        sys.exit(1)
    print("\nAll contract tests passed")

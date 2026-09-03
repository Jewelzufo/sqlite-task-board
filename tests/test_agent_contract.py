"""
test_agent_contract.py

Contract tests for SQLite Task Board agent.py

Validates:
- AGENTS.md exists
- opencode.json exists and references AGENTS.md
- config.example.yaml exists
- config.yaml exists
- migrations/0001_initial.sql exists and creates tasks and schema_migrations
- seeds/bootstrap_tasks.sql exists and inserts bootstrap tasks
- agent.py exists and contains ACTION_REGISTRY
- requirements.txt exists
"""

import json
import os
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

def test_config_example_yaml_exists():
    """config.example.yaml must exist."""
    config_file = ROOT / "config.example.yaml"
    assert config_file.exists(), "config.example.yaml must exist"

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
    """Set up agent with temporary database and workspace."""
    from agent import Agent
    
    db_path = Path(tmpdir) / "test_tasks.db"
    agent = Agent(db_path)
    
    # Override workspace to temp location for isolation
    import agent as agent_module
    agent_module.WORKSPACE = Path(tmpdir) / "workspace"
    agent.workspace = agent_module.WORKSPACE
    agent.workspace.mkdir(exist_ok=True)
    
    return agent


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


if __name__ == "__main__":
    # Simple runner without pytest
    tests = [
        test_agents_md_exists,
        test_opencode_json_exists_and_references_agents,
        test_config_example_yaml_exists,
        test_config_yaml_exists,
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

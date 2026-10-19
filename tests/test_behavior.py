"""Behavioral tests for the SQLite task board runtime."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import agent as agent_module
from agent import Agent, TaskError


def make_config(tmp_path, allowlist=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "agent": {
            "workspace": str(workspace),
            "database": str(tmp_path / "tasks.db"),
            "schema_version": 1,
            "protocol_version": "3.1",
        },
        "security": {
            "network_allowlist": ["127.0.0.1", "localhost"]
            if allowlist is None
            else allowlist
        },
    }


@pytest.fixture
def agent(tmp_path):
    instance = Agent(config=make_config(tmp_path))
    yield instance
    instance.conn.close()


def insert_task(agent, instruction, action_type, max_attempts=3, priority="medium", key="task"):
    agent.conn.execute(
        "INSERT INTO tasks (instruction, action_type, idempotency_key, priority, "
        "max_attempts) VALUES (?,?,?,?,?)",
        (
            instruction if isinstance(instruction, str) else json.dumps(instruction),
            action_type,
            key,
            priority,
            max_attempts,
        ),
    )
    agent.conn.commit()


def task_row(agent):
    row = agent.conn.execute("SELECT * FROM tasks ORDER BY id LIMIT 1").fetchone()
    return dict(row)


def test_claim_is_atomic(tmp_path):
    config = make_config(tmp_path)
    first = Agent(config=config)
    second = Agent(config=config)
    try:
        insert_task(
            first,
            {"action": "verify_runtime", "language": "python", "min_version": "3.11",
             "idempotency_key": "atomic"},
            "verify_runtime",
        )
        claimed = first.claim_next_task()
        assert claimed is not None
        assert claimed["status"] == "running"
        assert claimed["attempt_count"] == 1
        assert second.claim_next_task() is None
    finally:
        first.conn.close()
        second.conn.close()


def test_dry_run_has_no_side_effects(agent, monkeypatch):
    insert_task(
        agent,
        {"action": "create_directories", "paths": ["dry-run-dir"],
         "idempotency_key": "dry-run"},
        "create_directories",
    )
    monkeypatch.setattr(agent_module, "DRY_RUN", True)
    assert agent.run_once() is True
    assert not (agent.workspace / "dry-run-dir").exists()
    row = task_row(agent)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0


def test_invalid_json_records_error_code(agent):
    insert_task(agent, "not-json", "verify_runtime", max_attempts=1)
    assert agent.run_once() is False
    row = task_row(agent)
    assert row["status"] == "dead-lettered"
    assert row["error_code"] == "INVALID_JSON"


def test_schema_failure_records_error_code(agent):
    insert_task(
        agent,
        {"action": "verify_runtime", "language": "python"},
        "verify_runtime",
        max_attempts=1,
    )
    assert agent.run_once() is False
    row = task_row(agent)
    assert row["status"] == "dead-lettered"
    assert row["error_code"] == "SCHEMA_VALIDATION_FAILED"


def test_unknown_action_records_error_code(agent):
    insert_task(
        agent,
        {"action": "bogus", "idempotency_key": "bogus"},
        "bogus",
        max_attempts=1,
    )
    assert agent.run_once() is False
    row = task_row(agent)
    assert row["status"] == "dead-lettered"
    assert row["error_code"] == "UNKNOWN_ACTION"


def test_path_traversal_records_error_code(agent):
    insert_task(
        agent,
        {"action": "create_directories", "paths": ["../escape"],
         "idempotency_key": "escape"},
        "create_directories",
        max_attempts=1,
    )
    assert agent.run_once() is False
    row = task_row(agent)
    assert row["status"] == "dead-lettered"
    assert row["error_code"] == "PATH_TRAVERSAL_ATTEMPT"


def test_create_directories_success_and_prevalidation(agent):
    handler = agent_module.ACTION_REGISTRY["create_directories"]
    result = handler({"paths": ["a/b"], "idempotency_key": "dirs"})
    assert result["created"] == ["a/b"]
    assert (agent.workspace / "a" / "b").is_dir()

    with pytest.raises(TaskError) as exc:
        handler({"paths": ["should-not-exist", "../bad"], "idempotency_key": "bad"})
    assert exc.value.code == "PATH_TRAVERSAL_ATTEMPT"
    assert not (agent.workspace / "should-not-exist").exists()
def test_health_policy_rejections(agent):
    handler = agent_module.ACTION_REGISTRY["run_health_check"]
    with pytest.raises(TaskError) as exc:
        handler({"endpoint": "http://example.com/health", "timeout_seconds": 1,
                 "idempotency_key": "host"})
    assert exc.value.code == "UNAPPROVED_HOST"

    with pytest.raises(TaskError) as exc:
        handler({"endpoint": "ftp://127.0.0.1/health", "timeout_seconds": 1,
                 "idempotency_key": "protocol"})
    assert exc.value.code == "UNAPPROVED_PROTOCOL"


def test_redirect_is_rejected():
    handler = agent_module._NoRedirectHandler()
    with pytest.raises(TaskError) as exc:
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/health")
    assert exc.value.code == "UNAPPROVED_HOST"


def test_verify_runtime_version_handling(agent):
    handler = agent_module.ACTION_REGISTRY["verify_runtime"]
    result = handler({"language": "python", "min_version": "3.0.0",
                      "idempotency_key": "verify-ok"})
    assert result["status"] == "ok"

    with pytest.raises(TaskError) as exc:
        handler({"language": "python", "min_version": "99.0.0",
                 "idempotency_key": "verify-fail"})
    assert exc.value.code == "SCHEMA_VALIDATION_FAILED"

    with pytest.raises(TaskError) as exc:
        handler({"language": "node", "min_version": "1.0.0",
                 "idempotency_key": "verify-node"})
    assert exc.value.code == "SCHEMA_VALIDATION_FAILED"


def test_patch_version_ordering():
    assert agent_module._parse_version("3.11.0") < agent_module._parse_version("3.11.9")
    assert agent_module._parse_version("3.12.0") > agent_module._parse_version("3.11.9")


def test_recovery_transitions(agent):
    insert_task(agent, {"action": "bogus", "idempotency_key": "recover-pending"},
                "bogus", max_attempts=3, key="recover-pending")
    insert_task(agent, {"action": "bogus", "idempotency_key": "recover-dead"},
                "bogus", max_attempts=3, key="recover-dead")
    agent.conn.execute(
        "UPDATE tasks SET status='running', attempt_count=1, started_at='x' "
        "WHERE idempotency_key='recover-pending'"
    )
    agent.conn.execute(
        "UPDATE tasks SET status='running', attempt_count=3, started_at='x' "
        "WHERE idempotency_key='recover-dead'"
    )
    agent.conn.commit()
    agent.recover_interrupted_tasks()

    pending = agent.conn.execute(
        "SELECT status, started_at FROM tasks WHERE idempotency_key='recover-pending'"
    ).fetchone()
    dead = agent.conn.execute(
        "SELECT status, error_code FROM tasks WHERE idempotency_key='recover-dead'"
    ).fetchone()
    assert pending["status"] == "pending"
    assert pending["started_at"] is None
    assert dead["status"] == "dead-lettered"
    assert dead["error_code"] == "INTERRUPTED_MAX_ATTEMPTS_EXCEEDED"


def test_status_json_contract(agent, capsys):
    insert_task(agent, {"action": "verify_runtime", "language": "python",
                        "min_version": "3.11", "idempotency_key": "status"},
                "verify_runtime")
    assert agent.status(as_json=True) is True
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["schema_version"] == 1
    assert payload["status_counts"]["pending"] == 1
    assert payload["next_task_id"] is not None


def test_redaction_in_events(capsys):
    agent_module.emit_event(
        "EXEC_END",
        token="super-secret",
        nested={"password": "hunter2"},
        message="token=abc123",
    )
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["token"] == "[REDACTED]"
    assert payload["nested"]["password"] == "[REDACTED]"
    assert "abc123" not in payload["message"]


def test_check_requires_nonempty_allowlist(tmp_path, capsys):
    instance = Agent(config=make_config(tmp_path, allowlist=[]))
    try:
        assert instance.check() is False
        assert "network_allowlist" in capsys.readouterr().out
    finally:
        instance.conn.close()


def test_config_schema_version_mismatch(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n  schema_version: 99\n  workspace: ./workspace\n  database: ./tasks.db\n",
        encoding="utf-8",
    )
    with pytest.raises(agent_module.ConfigError) as exc:
        agent_module.load_config(config_path)
    assert exc.value.code == "SCHEMA_VERSION_MISMATCH"


def test_no_pending_task_returns_none(agent):
    assert agent.run_once() is None


def test_action_and_error_surfaces_are_consistent():
    assert set(agent_module.ACTION_REGISTRY) == set(agent_module.ACTION_SCHEMAS)
    assert "UNKNOWN_ACTION" in agent_module.ERROR_CODES
    assert "EXEC_ERROR" in agent_module.EVENT_NAMES
    assert "EXEC_PLAN" in agent_module.EVENT_NAMES
def _run_cli(tmp_path, *args):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agent:\n"
        f'  workspace: "{workspace}"\n'
        f'  database: "{tmp_path / "tasks.db"}"\n'
        "  schema_version: 1\n"
        '  protocol_version: "3.1"\n'
        "security:\n"
        '  network_allowlist: ["127.0.0.1", "localhost"]\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AGENT_CONFIG"] = str(config_path)
    return subprocess.run(
        [sys.executable, str(ROOT / "agent.py"), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def test_cli_once_exit_codes_and_jsonl(tmp_path):
    result = _run_cli(tmp_path, "--init")
    assert result.returncode == 0, result.stderr
    result = _run_cli(tmp_path, "--check")
    assert result.returncode == 0, result.stdout + result.stderr

    result = _run_cli(tmp_path, "--once")
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        assert json.loads(line)["event"] in agent_module.EVENT_NAMES

    assert _run_cli(tmp_path, "--once").returncode == 0
    assert _run_cli(tmp_path, "--once").returncode == 1
    assert _run_cli(tmp_path, "--once").returncode == 2


def test_cli_dry_run_leaves_queue_pending(tmp_path):
    assert _run_cli(tmp_path, "--init").returncode == 0
    result = _run_cli(tmp_path, "--once", "--dry-run")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["event"] == "EXEC_PLAN"
    status = _run_cli(tmp_path, "--status", "--json")
    assert json.loads(status.stdout.strip())["status_counts"]["pending"] == 3


def test_docs_match_runtime_surfaces():
    reference = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    for action in agent_module.ACTION_REGISTRY:
        assert action in reference
    for code in agent_module.ERROR_CODES:
        assert code in reference
    for event in agent_module.EVENT_NAMES:
        assert event in reference


def test_packaging_and_ci_exist():
    assert (ROOT / "pyproject.toml").exists()
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()
    assert (ROOT / "Makefile").exists()
    assert (ROOT / "docs" / "reference.md").exists()
    assert not (ROOT / "assets").exists()


def test_opencode_allows_bash_for_bootstrap():
    payload = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
    assert payload["instructions"] == ["AGENTS.md"]
    assert payload["permission"]["bash"] == "allow"


def _write_config(tmp_path, schema_version=1, extra=""):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n"
        f'  workspace: "{workspace}"\n'
        f'  database: "{tmp_path / "tasks.db"}"\n'
        f"  schema_version: {schema_version}\n"
        '  protocol_version: "3.1"\n'
        "security:\n"
        '  network_allowlist: ["127.0.0.1", "localhost"]\n' + extra,
        encoding="utf-8",
    )
    return path


def test_main_lifecycle_paths(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))

    for args, expected in [
        (["--init"], 0),
        (["--check"], 0),
        (["--status"], 0),
        (["--status", "--json"], 0),
        (["--once"], 0),
        (["--once", "--dry-run"], 0),
    ]:
        monkeypatch.setattr(sys, "argv", ["agent.py", *args])
        assert agent_module.main() == expected, args


def test_main_uninitialized_and_dry_run_paths(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    uninitialized = tmp_path / "missing"
    uninitialized.mkdir()
    missing_config = uninitialized / "config.yaml"
    missing_config.write_text(
        "agent:\n"
        f'  workspace: "{uninitialized}"\n'
        f'  database: "{uninitialized / "tasks.db"}"\n'
        "  schema_version: 1\n"
        "security:\n"
        '  network_allowlist: ["127.0.0.1"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_CONFIG", str(missing_config))
    for args in (["--check"], ["--status"], ["--status", "--json"], ["--once"]):
        monkeypatch.setattr(sys, "argv", ["agent.py", *args])
        assert agent_module.main() == 1, args


def test_main_init_failure(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    monkeypatch.setattr(agent_module.Agent, "initialize", lambda self: (_ for _ in ()).throw(
        TaskError("INIT_FAILED", "boom")
    ))
    monkeypatch.setattr(sys, "argv", ["agent.py", "--init"])
    assert agent_module.main() == 1


def test_main_missing_dependencies(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    monkeypatch.setattr(agent_module, "load_config", lambda: {"agent": {}})
    monkeypatch.setattr(agent_module, "yaml", None)
    monkeypatch.setattr(agent_module, "HAS_JSONSCHEMA", False)
    monkeypatch.setattr(sys, "argv", ["agent.py", "--once"])
    assert agent_module.main() == 1


def test_main_schema_mismatch(tmp_path, monkeypatch):
    import sqlite3

    config_path = _write_config(tmp_path)
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);"
        "INSERT INTO schema_migrations VALUES (2);"
    )
    conn.close()
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "argv", ["agent.py", "--once"])
    assert agent_module.main() == 1


def test_run_loop_shutdown(agent, monkeypatch):
    monkeypatch.setattr(agent, "run_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt))
    agent.run_loop()


def test_load_config_validation_branches(tmp_path):
    cases = [
        ("- just a list\n", "CONFIG_LOAD_FAILED"),
        ("foo: bar\n", "CONFIG_LOAD_FAILED"),
        ("agent: []\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: nope\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 1\n  workspace: 5\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 1\nsecurity: []\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 1\nsecurity:\n  network_allowlist: []\n",
         "CONFIG_LOAD_FAILED"),
    ]
    for index, (text, code) in enumerate(cases):
        path = tmp_path / f"bad-{index}.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(agent_module.ConfigError) as exc:
            agent_module.load_config(path)
        assert exc.value.code == code


def test_check_failure_paths(tmp_path):
    config = make_config(tmp_path)
    config["agent"]["workspace"] = str(tmp_path / "missing-workspace")
    instance = Agent(config=config)
    try:
        assert instance.check() is False
    finally:
        instance.conn.close()

    config = make_config(tmp_path / "other")
    config["agent"]["database"] = str(tmp_path / "other" / "missing.db")
    instance = Agent(config=config, auto_init=False)
    try:
        assert instance.check() is False
    finally:
        instance.conn.close()


def test_truncate_and_parse_helpers():
    assert agent_module.truncate_output("x" * 20, limit=10) == "x" * 10 + "...[truncated]"
    assert agent_module._parse_version("not-a-version") is None
    assert agent_module.redact(["token=abc"]) == ["token=[REDACTED]"]


def test_absolute_path_rejected(agent):
    with pytest.raises(TaskError) as exc:
        agent._workspace_path("/etc/passwd")
    assert exc.value.code == "PATH_OUTSIDE_WORKSPACE"


def test_status_uninitialized(tmp_path, capsys):
    config = make_config(tmp_path)
    instance = Agent(config=config, auto_init=False)
    try:
        assert instance.status(as_json=True) is False
        assert "Database not initialized" in capsys.readouterr().out
    finally:
        instance.conn.close()


def test_main_no_work_exit_code(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "argv", ["agent.py", "--init"])
    assert agent_module.main() == 0
    monkeypatch.setattr(sys, "argv", ["agent.py", "--once"])
    assert agent_module.main() == 0
    assert agent_module.main() == 0
    assert agent_module.main() == 1
    assert agent_module.main() == 2

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
            "schema_version": 3,
            "protocol_version": "3.2",
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
    assert payload["schema_version"] == 3
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
        "  schema_version: 3\n"
        '  protocol_version: "3.2"\n'
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
    assert (ROOT / "docs" / "ci-workflow.yml").exists()
    assert (ROOT / "scripts" / "ci.sh").exists()
    assert (ROOT / "Makefile").exists()
    assert (ROOT / "docs" / "reference.md").exists()
    assert not (ROOT / "assets").exists()


def test_opencode_allows_bash_for_bootstrap():
    payload = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
    assert payload["instructions"] == ["AGENTS.md"]
    assert payload["permission"]["bash"] == "allow"


def _write_config(tmp_path, schema_version=3, extra=""):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n"
        f'  workspace: "{workspace}"\n'
        f'  database: "{tmp_path / "tasks.db"}"\n'
        f"  schema_version: {schema_version}\n"
        '  protocol_version: "3.2"\n'
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
        "  schema_version: 3\n"
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
        ("agent:\n  schema_version: 3\n  workspace: 5\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\nsecurity: []\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\nsecurity:\n  network_allowlist: []\n",
         "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\npolicy: []\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\npolicy:\n  allowed_actions: []\n",
         "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\npolicy:\n  allowed_actions: [bogus]\n",
         "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\npolicy:\n  allowed_path_roots: [/tmp]\n",
         "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\npolicy:\n  max_payload_bytes: nope\n",
         "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\nlimits: []\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\nlimits:\n  unknown: 1\n", "CONFIG_LOAD_FAILED"),
        ("agent:\n  schema_version: 3\nlimits:\n  window_seconds: 0\n",
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


def test_idempotency_replay_skips_side_effect(agent, monkeypatch):
    calls = []
    original = agent_module.ACTION_REGISTRY["verify_runtime"]

    def counting(payload):
        calls.append(payload["idempotency_key"])
        return original(payload)

    monkeypatch.setitem(agent_module.ACTION_REGISTRY, "verify_runtime", counting)
    payload = {"action": "verify_runtime", "language": "python", "min_version": "3.11",
               "idempotency_key": "replay-once"}
    insert_task(agent, payload, "verify_runtime")
    assert agent.run_once() is True
    agent.conn.execute(
        "UPDATE tasks SET status='pending', attempt_count=0, started_at=NULL WHERE id=1"
    )
    agent.conn.commit()
    assert agent.run_once() is True
    assert calls == ["replay-once"]
    row = task_row(agent)
    assert row["status"] == "completed"
    ledger = agent.conn.execute("SELECT COUNT(*) FROM idempotency_ledger").fetchone()[0]
    assert ledger == 1


def test_idempotency_conflict_is_dead_lettered(agent):
    payload = {"action": "verify_runtime", "language": "python", "min_version": "3.11",
               "idempotency_key": "conflict-key"}
    insert_task(agent, payload, "verify_runtime")
    assert agent.run_once() is True
    changed = dict(payload, min_version="3.12")
    agent.conn.execute(
        "UPDATE tasks SET status='pending', attempt_count=0, started_at=NULL, "
        "instruction=? WHERE id=1",
        (json.dumps(changed),),
    )
    agent.conn.commit()
    assert agent.run_once() is False
    row = task_row(agent)
    assert row["status"] == "dead-lettered"
    assert row["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_enqueue_same_payload_is_idempotent(agent):
    payload = {"action": "verify_runtime", "language": "python", "min_version": "3.11",
               "idempotency_key": "enqueue-idempotent"}
    first = agent.enqueue_task_record("verify_runtime", payload)
    second = agent.enqueue_task_record("verify_runtime", payload)
    assert first["task_id"] == second["task_id"]
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is False
    assert second["status"] == "pending"
    assert second["result"] is None
    rows = agent.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key='enqueue-idempotent'"
    ).fetchone()[0]
    assert rows == 1
    assert agent.enqueue_task("verify_runtime", payload) == first["task_id"]


def test_enqueue_replays_completed_result_without_new_task(agent):
    agent.conn.execute("DELETE FROM tasks")
    agent.conn.commit()
    payload = {"action": "verify_runtime", "language": "python", "min_version": "3.11",
               "idempotency_key": "enqueue-replay-result"}
    first = agent.enqueue_task_record("verify_runtime", payload)
    assert agent.run_once() is True

    replay = agent.enqueue_task_record("verify_runtime", payload)
    assert replay["task_id"] == first["task_id"]
    assert replay["status"] == "completed"
    assert replay["idempotent_replay"] is True
    assert replay["result"]["status"] == "ok"
    assert agent.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert agent.conn.execute(
        "SELECT COUNT(*) FROM idempotency_ledger WHERE key='enqueue-replay-result'"
    ).fetchone()[0] == 1
    assert agent.run_once() is None


def test_enqueue_rejects_same_key_with_changed_payload_or_action(agent):
    agent.conn.execute("DELETE FROM tasks")
    agent.conn.commit()
    payload = {"action": "verify_runtime", "language": "python", "min_version": "3.11",
               "idempotency_key": "enqueue-conflict"}
    agent.enqueue_task_record("verify_runtime", payload)

    changed_payload = dict(payload, min_version="3.12")
    with pytest.raises(TaskError) as payload_error:
        agent.enqueue_task_record("verify_runtime", changed_payload)
    assert payload_error.value.code == "IDEMPOTENCY_CONFLICT"

    changed_action = {"action": "create_directories", "paths": ["elsewhere"],
                      "idempotency_key": "enqueue-conflict"}
    with pytest.raises(TaskError) as action_error:
        agent.enqueue_task_record("create_directories", changed_action)
    assert action_error.value.code == "IDEMPOTENCY_CONFLICT"
    assert agent.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_cli_enqueue_is_idempotent(tmp_path):
    assert _run_cli(tmp_path, "--init").returncode == 0
    task_file = tmp_path / "idempotent.json"
    task_file.write_text(json.dumps({
        "action": "verify_runtime", "language": "python", "min_version": "3.11",
        "idempotency_key": "cli-idempotent",
    }), encoding="utf-8")
    first = _run_cli(tmp_path, "--enqueue", str(task_file))
    second = _run_cli(tmp_path, "--enqueue", str(task_file))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_record = json.loads(first.stdout.strip())
    second_record = json.loads(second.stdout.strip())
    assert first_record["task_id"] == second_record["task_id"]
    assert second_record["idempotent_replay"] is False
    status = _run_cli(tmp_path, "--status", "--json")
    counts = json.loads(status.stdout.strip())["status_counts"]
    assert counts["pending"] == 4

    changed_file = tmp_path / "changed.json"
    changed_file.write_text(json.dumps({
        "action": "verify_runtime", "language": "python", "min_version": "3.12",
        "idempotency_key": "cli-idempotent",
    }), encoding="utf-8")
    conflict = _run_cli(tmp_path, "--enqueue", str(changed_file))
    assert conflict.returncode == 1
    events = [json.loads(line) for line in conflict.stdout.splitlines()]
    assert events[-1]["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_cli_enqueue_replays_completed_result(tmp_path):
    import sqlite3

    assert _run_cli(tmp_path, "--init").returncode == 0
    conn = sqlite3.connect(tmp_path / "tasks.db")
    conn.execute("DELETE FROM tasks")
    conn.commit()
    conn.close()

    task_file = tmp_path / "replay-result.json"
    task_file.write_text(json.dumps({
        "action": "verify_runtime", "language": "python", "min_version": "3.11",
        "idempotency_key": "cli-replay-result",
    }), encoding="utf-8")
    first = json.loads(_run_cli(tmp_path, "--enqueue", str(task_file)).stdout.strip())
    assert _run_cli(tmp_path, "--once").returncode == 0

    replay = _run_cli(tmp_path, "--enqueue", str(task_file))
    assert replay.returncode == 0, replay.stderr
    record = json.loads(replay.stdout.strip())
    assert record["task_id"] == first["task_id"]
    assert record["idempotent_replay"] is True
    assert record["status"] == "completed"
    assert record["result"]["status"] == "ok"
    status = _run_cli(tmp_path, "--status", "--json")
    counts = json.loads(status.stdout.strip())["status_counts"]
    assert counts == {"completed": 1}


def test_policy_denies_unlisted_action(tmp_path, capsys):
    config = make_config(tmp_path)
    config["policy"] = {"allowed_actions": ["verify_runtime"]}
    instance = Agent(config=config)
    try:
        insert_task(
            instance,
            {"action": "create_directories", "paths": ["nope"],
             "idempotency_key": "policy-deny"},
            "create_directories",
        )
        assert instance.run_once() is False
        row = task_row(instance)
        assert row["status"] == "dead-lettered"
        assert row["error_code"] == "POLICY_DENIED"
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        decisions = [event for event in events if event["event"] == "POLICY_DECISION"]
        assert decisions and decisions[0]["decision"] == "deny"
    finally:
        instance.conn.close()


def test_policy_payload_size_limit(tmp_path):
    config = make_config(tmp_path)
    config["policy"] = {"max_payload_bytes": 40}
    instance = Agent(config=config)
    try:
        insert_task(
            instance,
            {"action": "verify_runtime", "language": "python", "min_version": "3.11",
             "idempotency_key": "too-big"},
            "verify_runtime",
        )
        assert instance.run_once() is False
        row = task_row(instance)
        assert row["error_code"] == "POLICY_DENIED"
    finally:
        instance.conn.close()


def test_governor_blocks_task_window(tmp_path, capsys):
    config = make_config(tmp_path)
    config["limits"] = {"max_tasks_per_window": 1}
    instance = Agent(config=config)
    try:
        insert_task(instance, {"action": "verify_runtime", "language": "python",
                               "min_version": "3.11", "idempotency_key": "window-1"},
                    "verify_runtime", key="window-1")
        insert_task(instance, {"action": "verify_runtime", "language": "python",
                               "min_version": "3.11", "idempotency_key": "window-2"},
                    "verify_runtime", key="window-2")
        assert instance.run_once() is True
        assert instance.run_once() is False
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert any(event["event"] == "BUDGET_EXCEEDED" for event in events)
    finally:
        instance.conn.close()



def test_circuit_breaker_opens_after_failures(tmp_path, capsys):
    config = make_config(tmp_path)
    config["limits"] = {"max_failures_per_window": 1}
    instance = Agent(config=config)
    try:
        insert_task(instance, {"action": "bogus", "idempotency_key": "breaker-fail"},
                    "bogus", max_attempts=1, key="breaker-fail")
        assert instance.run_once() is False
        insert_task(instance, {"action": "verify_runtime", "language": "python",
                               "min_version": "3.11", "idempotency_key": "breaker-ok"},
                    "verify_runtime", key="breaker-ok")
        assert instance.run_once() is False
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert any(event["event"] == "CIRCUIT_OPEN" for event in events)
    finally:
        instance.conn.close()


def test_lineage_validation_and_graph(tmp_path):
    config = make_config(tmp_path)
    config["limits"] = {"max_spawn_depth": 1}
    instance = Agent(config=config)
    try:
        parent = instance.enqueue_task(
            "verify_runtime",
            {"action": "verify_runtime", "language": "python", "min_version": "3.11",
             "idempotency_key": "lineage-parent"},
        )
        child = instance.enqueue_task(
            "verify_runtime",
            {"action": "verify_runtime", "language": "python", "min_version": "3.11",
             "idempotency_key": "lineage-child"},
            parent_task_id=parent,
        )
        with pytest.raises(TaskError) as exc:
            instance.enqueue_task(
                "verify_runtime",
                {"action": "verify_runtime", "language": "python",
                 "min_version": "3.11", "idempotency_key": "lineage-grandchild"},
                parent_task_id=child,
            )
        assert exc.value.code == "LINEAGE_INVALID"
        with pytest.raises(TaskError) as exc:
            instance.enqueue_task(
                "verify_runtime",
                {"action": "verify_runtime", "language": "python",
                 "min_version": "3.11", "idempotency_key": "lineage-orphan"},
                parent_task_id=9999,
            )
        assert exc.value.code == "LINEAGE_INVALID"
        result = instance.graph(fmt="json")
        assert result["edges"] == [{"from": parent, "to": child}]
        assert len(result["nodes"]) == 2
    finally:
        instance.conn.close()


def test_audit_verify_detects_tampering(agent):
    insert_task(agent, {"action": "verify_runtime", "language": "python",
                        "min_version": "3.11", "idempotency_key": "audit"},
                "verify_runtime")
    assert agent.run_once() is True
    assert agent.audit_verify()["ok"] is True
    agent.conn.execute(
        "UPDATE audit_log SET payload_json='{\"event\":\"TAMPERED\"}' WHERE seq=1"
    )
    agent.conn.commit()
    result = agent.audit_verify()
    assert result["ok"] is False
    assert result["seq"] == 1


def test_cli_innovation_commands(tmp_path):
    assert _run_cli(tmp_path, "--init").returncode == 0
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps({
        "action": "verify_runtime", "language": "python", "min_version": "3.11",
        "idempotency_key": "cli-enqueue",
    }), encoding="utf-8")
    enqueued = _run_cli(tmp_path, "--enqueue", str(task_file))
    assert enqueued.returncode == 0, enqueued.stderr
    assert json.loads(enqueued.stdout.strip())["task_id"] >= 1

    graph = _run_cli(tmp_path, "--graph", "--format", "json")
    assert graph.returncode == 0, graph.stderr
    assert "nodes" in json.loads(graph.stdout.strip())

    audit = _run_cli(tmp_path, "--audit-verify")
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert json.loads(audit.stdout.strip())["ok"] is True

    fixture = tmp_path / "replay.json"
    fixture.write_text(json.dumps({
        "payload": {"action": "verify_runtime", "language": "python",
                    "min_version": "3.11", "idempotency_key": "cli-replay"},
        "expect": {"decision": "allow"},
    }), encoding="utf-8")
    replay = _run_cli(tmp_path, "--replay", str(fixture))
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(replay.stdout.strip())["ok"] is True


def test_replay_fixture_decisions(tmp_path, capsys):
    config = make_config(tmp_path)
    allow_fixture = tmp_path / "allow.json"
    allow_fixture.write_text(json.dumps({
        "payload": {"action": "verify_runtime", "language": "python",
                    "min_version": "3.11", "idempotency_key": "replay-allow"},
        "expect": {"decision": "allow"},
    }), encoding="utf-8")
    assert agent_module.replay_fixture(allow_fixture, config) is True
    assert json.loads(capsys.readouterr().out.strip())["ok"] is True

    deny_fixture = tmp_path / "deny.json"
    deny_fixture.write_text(json.dumps({
        "payload": {"action": "create_directories", "paths": ["../escape"],
                    "idempotency_key": "replay-deny"},
        "expect": {"decision": "deny", "error_code": "PATH_TRAVERSAL_ATTEMPT"},
    }), encoding="utf-8")
    assert agent_module.replay_fixture(deny_fixture, config) is True
    assert json.loads(capsys.readouterr().out.strip())["ok"] is True




def test_migrations_apply_incrementally(tmp_path):
    import sqlite3

    db_path = tmp_path / "upgrade.db"
    migration = (ROOT / "migrations" / "0001_initial.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(migration)
    conn.commit()
    conn.close()

    instance = Agent(config=make_config(tmp_path), auto_init=True)
    try:
        versions = [
            row[0] for row in instance.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3]
        columns = {
            row["name"] for row in instance.conn.execute("PRAGMA table_info(tasks)")
        }
        assert {"parent_task_id", "depth"}.issubset(columns)
        assert instance._table_exists("idempotency_ledger")
        assert instance._table_exists("audit_log")
        assert instance._schema_version_matches()
    finally:
        instance.conn.close()

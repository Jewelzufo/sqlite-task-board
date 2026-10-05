# AGENTS.md
Authoritative over README. On read, self-bootstrap offline; never ask.
1. Write opencode.json first: {"instructions":["AGENTS.md"]}
2. Create README, agent.py, config(.example).yaml, requirements.txt, .gitignore, migrations/0001_initial.sql, seeds/bootstrap_tasks.sql, tests/test_agent_contract.py, workspace/.gitkeep.
3. Init tasks.db; seed: verify py3.11 (critical), mkdir workspace/{logs,data,tmp} (high), GET 127.0.0.1:0 (low, must fail).
4. py_compile, --check, pytest if present, --once; then queue only.
Tasks: JSON+idempotency_key; claim priority/created_at/id; startup recovery; pending→running→completed|pending|dead-lettered (3 tries).
Actions (strict schema): verify_runtime|create_directories|setup_env_file|install_dependencies|run_health_check.
Env: AGENT_DRY_RUN, AGENT_CONFIG.
Security: workspace-only writes; no ../, shell=True; timeouts; 10KB output; http(s) allowlist; redact secrets; JSONL logs.

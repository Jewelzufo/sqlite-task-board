# Changelog

## Unreleased

- Made enqueue idempotent end-to-end. Repeating the same key, action, and canonical
  payload returns the existing task id instead of inserting a duplicate, and a completed
  key returns its stored `result` with `idempotent_replay: true` without re-executing.
  Reusing a key with a different action or payload still fails with
  `IDEMPOTENCY_CONFLICT`.
- Added regression coverage for same-payload enqueue, completed-result replay, payload/
  action conflicts, and the `--enqueue` CLI output contract.

## 0.3.0

- Enforced idempotency at execution time with an `idempotency_ledger`; successful
  payloads replay their stored result and key reuse with a different payload fails
  with `IDEMPOTENCY_CONFLICT`.
- Added a hash-chained `audit_log` and `--audit-verify` to detect tampering.
- Added deny-by-default policy-as-code (`policy:`), `POLICY_DECISION` traces, and
  deterministic budget/circuit-breaker limits (`limits:`, `BUDGET_EXCEEDED`,
  `CIRCUIT_OPEN`).
- Added task lineage (`parent_task_id`, `depth`), `--enqueue`, and `--graph`.
- Added offline fixture replay with `--replay`.
- Bumped schema version to 3 and protocol version to 3.2; migrations `0002` and
  `0003` are applied incrementally and idempotently.

## 0.2.0

- Added atomic task claiming with `BEGIN IMMEDIATE`, WAL, and `busy_timeout`.
- Added stable task error codes and `EXEC_ERROR` / `EXEC_PLAN` events.
- Made dry-run execute a planner with no task or filesystem side effects.
- Restricted `verify_runtime` to the implemented Python check and compare patch versions.
- Rejected health-check redirects and added log redaction and output truncation.
- Added subprocess environment minimization and config allowlist validation.
- Routed human progress output to stderr so stdout remains JSONL.
- Added `taskboard` console entry point, CI, linting, and coverage configuration.
- Added behavioral tests for claims, recovery, dry-run, errors, security, and status.

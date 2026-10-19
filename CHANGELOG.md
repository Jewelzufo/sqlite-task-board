# Changelog

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

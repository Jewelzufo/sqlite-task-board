# Innovation Triage — Changes Worth Keeping

**Status:** Decision record (analysis only; no runtime changes)
**Baseline:** `51a2139` on `main`, 56 tests, single-process SQLite runtime
**Source proposal:** eight enhancements intended to push the project "above 90"
**Authoritative contract:** [`AGENTS.md`](../../AGENTS.md), especially §1 (goal and
non-goals), §8 (security), §11 (dry-run), and §13 (change control)

---

## 1. Decision at a glance

The proposal's premise — that innovation is measured by how many advanced
paradigms a project can name — does not match this repository's rubric. The
project's value comes from a narrow, deterministic, offline, auditable execution
contract. `docs/decisions/IMPROVEMENTS.md` already projects **93/100** without
distributed workers, WASM, or a model in the trust path.

Result: **none of the eight proposals should be adopted as written.** Six
in-scope changes are worth keeping; the remaining ideas are rejected or deferred
behind explicit triggers.

| ID | Change worth keeping | Source idea | Why it survives |
|---|---|---|---|
| K1 | Enforce idempotency at execution time | (gap found while reviewing) | Closes a real hole in an advertised guarantee |
| K2 | Hash-chained audit ledger | "Cryptographic task provenance" | Tamper-evident provenance without a swarm |
| K3 | Policy-as-code with decision traces | "Semantic intent verification" | Deterministic, testable replacement for an LLM classifier |
| K4 | Resource governor + deterministic circuit breaker | "Predictive routing" / "behavioral profiling" | Enforceable today; no statistical guesswork |
| K5 | Task lineage + graph export | "Execution DAG visualization" | Makes the graph real; enables cycle/depth checks |
| K6 | Deterministic replay harness | (implied by audit/provenance) | Proves decisions are reproducible |

| ID | Proposal not kept | Disposition | Trigger to revisit |
|---|---|---|---|
| R1 | LLM-in-the-loop intent classification | Reject | A `run_script`/`run_code` action exists **and** a deterministic analyzer is insufficient |
| R2 | WASM/WASI or Firecracker/gVisor isolation | Defer | An action can execute untrusted code |
| R3 | CRDT/libp2p agent swarm | Reject | A second worker is a product requirement; needs leases + fencing |
| R4 | Auto-remediating schemas | Reject (report-only variant allowed) | Never auto-apply; human review only |
| D1 | Ed25519-signed cross-agent tasks | Defer | K2 is shipped and a second signing party exists |
| D2 | Statistical behavioral baselines | Reject | Queue volume makes a baseline statistically meaningful (not 3–30 tasks) |
| D3 | Context-window overlay on the DAG | Reject | OpenCode exposes context metrics to the runtime |
| D4 | Token-cost prediction | Reject | The runtime actually calls a metered model API |

---

## 2. K1 — Enforce idempotency at execution time

**Gap.** `migrations/0001_initial.sql` declares `idempotency_key TEXT UNIQUE`,
and all three action schemas require the key. But `agent.py` never reads the
key during execution: there is no ledger, no result reuse, and no conflict
handling. `seeds/bootstrap_tasks.sql` relies on `INSERT OR IGNORE`, so the key
only prevents duplicate *rows*. The contract advertises exactly-once semantics
that the runtime does not provide.

**Change.**
- Add `idempotency_ledger(key TEXT PRIMARY KEY, action_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL, task_id INTEGER NOT NULL, result_json TEXT NOT NULL,
  completed_at TIMESTAMP NOT NULL)`.
- Inside the existing `BEGIN IMMEDIATE` claim/execute path, look up the key
  before invoking an action. On hit with the same `payload_hash`, replay the
  stored result and complete without re-executing. On hit with a different
  hash, fail closed with `IDEMPOTENCY_CONFLICT`.
- Canonicalize the payload (sorted keys, no whitespace) before hashing.

**Acceptance.**
- Executing the same key twice performs the side effect once and returns the
  first result.
- Same key + different payload raises `IDEMPOTENCY_CONFLICT` and persists it.
- A two-connection race on one key executes exactly once.
- `--init` re-run still yields three bootstrap tasks.


---

## 3. K2 — Hash-chained audit ledger

**Gap.** Events are JSONL on stdout only. Nothing persists them, so provenance
is unverifiable and logs are trivial to edit.

**Change.**
- Add `audit_log(seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
  event TEXT, task_id INTEGER, payload_json TEXT, prev_hash TEXT, entry_hash TEXT)`.
- `entry_hash = sha256(prev_hash || canonical_event_json)`, genesis `prev_hash`
  all zeros. Write the audit row in the same transaction as each state change.
- Add `taskboard audit --verify`: recompute the chain, exit `0` intact, `1` on
  the first broken link, print the offending `seq` and expected/actual hash.

**Acceptance.**
- Verify passes on a fresh run.
- Updating or deleting any audit row makes `--verify` fail at that row.
- Hash is stable across runs for identical events (canonical JSON).
- No network, no new runtime dependency (`hashlib` only).

**Deferred part of the original idea:** signing with Ed25519, key rotation,
nonces, and cross-agent identity. A signature proves *who authorized* a task,
not that the task is safe; it does not stop prompt injection by a compromised
key holder. Ship the local chain first (D1).

---

## 4. K3 — Policy-as-code with decision traces

**Gap.** Security checks are scattered through handlers. There is no single,
inspectable decision point and no record of *why* a payload was allowed.

**Change.**
- Add a `policy:` section to `config.yaml` (deny by default): allowed actions,
  writable path roots, host allowlist reference, size/time ceilings.
- Evaluate policy between schema validation and action dispatch; emit
  `POLICY_DECISION` with `task_id`, `rule_id`, `decision`, `reason`.
- Reuse existing error semantics; add `POLICY_DENIED` for a rule that denies a
  payload the schema would accept.
- Dry-run must emit the same policy decision.

**Acceptance (valid, invalid, adversarial).**
- Path escape, `..`, absolute path, unapproved host/protocol, and oversized
  payload are denied with the stable code.
- A policy-allowed payload produces one `POLICY_DECISION` with `allow`.
- Unknown top-level policy keys warn and are ignored; invalid policy fails
  closed at load time.

**Why not an LLM classifier:** a probabilistic, injectable verifier cannot be
the authoritative boundary, cannot fail closed reproducibly, and would break
the offline-bootstrap rule in `AGENTS.md` §0. If a code-execution action is
ever added, add a Python `ast` analyzer *inside* this deterministic layer.

---

## 5. K4 — Resource governor and deterministic circuit breaker

**Gap.** The only limits are per-subprocess timeout, per-request timeout, and
`max_attempts`. Nothing bounds total work, output volume, task fan-out, or a
failure storm.

**Change.**
- Config limits: `max_task_seconds`, `max_output_bytes`,
  `max_tasks_per_window`, `max_spawn_depth`.
- Enforce before claim (window/fan-out) and around execution (wall clock,
  output cap). Emit `BUDGET_EXCEEDED` and dead-letter with that code.
- Trip `CIRCUIT_OPEN` when failures in a window exceed a configured threshold;
  `--status` shows the open state. Recovery is deterministic (window expiry or
  explicit `taskboard resume`), never a statistical baseline.

**Acceptance.**
- Injected clock makes window tests deterministic.
- A task over budget dead-letters with `BUDGET_EXCEEDED`.
- Breaker opens exactly at the threshold and blocks claims until reset.
- Existing retry/dead-letter behavior is unchanged below the limits.

---

## 6. K5 — Task lineage and graph export

**Gap.** There is no `parent_task_id`; "how tasks spawn" cannot be represented,
so a DAG of the queue is currently a flat list.

**Change.**
- Migration `0003`: `parent_task_id INTEGER REFERENCES tasks(id)`, `depth INTEGER
  NOT NULL DEFAULT 0`, index on `parent_task_id`.
- Validate on enqueue: parent exists, `depth = parent.depth + 1`, depth within
  `max_spawn_depth`, no cycles.
- Add `taskboard graph --format dot|json` with stable ordering and task status
  as node attributes.

**Acceptance.**
- Orphan parent, depth overflow, and cycle payloads are rejected with
  `LINEAGE_INVALID`.
- DOT output parses with a standard DOT parser.
- Graph is deterministic for an unchanged queue.
- No context-window data is fabricated; that metric is not owned by the runtime.

---

## 7. K6 — Deterministic replay harness

**Gap.** Tests assert behavior, but there is no way to replay a recorded
execution and prove that validation, policy, and terminal state are unchanged.

**Change.**
- Store fixtures as JSON: config, task row, action payload, expected
  `POLICY_DECISION`, expected terminal state/error code.
- Add `taskboard replay <fixture>` (offline, no filesystem/network effects) and
  a pytest suite that runs every fixture.
- Reuse the dry-run planning path; replay must never claim or mutate the queue.

**Acceptance.**
- All shipped fixtures reproduce their recorded decisions byte-for-byte on the
  event fields that matter.
- A fixture with a changed policy or schema fails loudly.
- Replay works with `AGENT_DRY_RUN` unset and leaves `tasks.db` untouched.

---

## 8. Contract and versioning impact

Every kept item touches the surfaces named in `AGENTS.md` §13, so each PR must
update all of them together:

| Surface | Change |
|---|---|
| Schema | `agent.schema_version` 1 → 3 (migration `0002` in PR 1, `0003` in PR 5) |
| Protocol | `agent.protocol_version` 3.1 → 3.2 |
| Tables | `idempotency_ledger`, `audit_log`, lineage columns |
| Events | `POLICY_DECISION`, `BUDGET_EXCEEDED`, `CIRCUIT_OPEN` |
| Error codes | `IDEMPOTENCY_CONFLICT`, `POLICY_DENIED`, `BUDGET_EXCEEDED`, `CIRCUIT_OPEN`, `LINEAGE_INVALID` |
| Commands | `audit --verify`, `graph`, `replay`, `resume` |
| Config | `policy:` and `limits:` sections, deny-by-default defaults |
| Files to update | `agent.py`, `docs/reference.md`, parity tests, `README.md`, `CHANGELOG.md`, and `AGENTS.md` §4/§6/§9/§13 |

This is a breaking protocol bump, not an additive one: `AGENTS.md` §4 must
declare `schema_version: 3` (2 after PR 1, 3 after PR 5) and
`protocol_version: "3.2"`, and §6 must accept the configured version while
continuing to refuse every other one. `--init` must remain idempotent across the
version bump; recovery, dry-run purity, `shell=False`, path boundaries, and the
exact host allowlist must not change.

---

## 9. Sequencing

| PR | Scope | Depends on | Notes |
|---:|---|---|---|
| 1 | K1 idempotency ledger + migration `0002` | — | Highest value, smallest surface |
| 2 | K6 replay harness | PR 1 | Locks behavior before policy changes |
| 3 | K3 policy engine + `POLICY_DECISION` | PR 2 | Single decision point |
| 4 | K4 governor + breaker | PR 3 | Hooks into policy evaluation |
| 5 | K5 lineage + `graph` + migration `0003` | PR 1 | Schema change isolated |
| 6 | K2 audit chain + `audit --verify` | PR 1–5 | Last, so new events are already defined |

Estimated effect: **+2 to +3 over the existing 93 projection** on the repo's
rubric, with no new required dependency and no change to the single-process,
local-first model.

---

## 10. Definition of done

- [ ] `idempotency_key` has execution-time semantics, not just a UNIQUE column.
- [ ] `audit --verify` detects a modified row at the correct sequence number.
- [ ] Policy decisions are emitted for allow and deny, including in dry-run.
- [ ] Budgets and breaker behavior are deterministic under an injected clock.
- [ ] Lineage rejects orphans, depth overflow, and cycles.
- [ ] Every new event and error code appears in `docs/reference.md` and parity tests.
- [ ] `python agent.py --init` twice yields three tasks and schema version 3.
- [ ] `python agent.py --check`, `--status`, `--once`, and `--once --dry-run` still pass.
- [ ] `pytest`, `ruff check .`, and `scripts/ci.sh` pass.
- [ ] No new network calls during bootstrap or tests.

---

## 11. Reaffirmed non-goals

Distributed workers, CRDTs, peer-to-peer mesh, hosted execution, web UI, ORM,
multi-tenant auth, and model-in-the-trust-path remain out of scope
(`AGENTS.md` §1, `IMPROVEMENTS.md` §11). A swarm with eventual consistency is a
different product: task claiming needs mutual exclusion, leases, and fencing
tokens, not convergence. If that product is ever built, it starts from this
repository's deterministic core rather than replacing it.


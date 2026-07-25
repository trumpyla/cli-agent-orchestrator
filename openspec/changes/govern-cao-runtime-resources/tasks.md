## 1. Baseline, evidence, and contract inventory

- [x] 1.1 Record `rtk git status --short`, strict OpenSpec validation, CAO `/health`, Herdr 0.7.5 workspace status counts, terminal/inbox row counts, and redacted resolved config under `evidence/baseline.md`; preserve the observed 53-to-28 workspace, 113-to-44 terminal, and 474 receiver-orphan cleanup evidence without identifiers, bodies, paths, or credentials.
- [x] 1.2 Run the pre-change focused baseline: config/settings/CLI, agent/skill paths, database, terminal/session service, Herdr backend/inbox, cleanup, Kimi provider, and lifespan tests; record every unrelated pre-existing failure before production edits.
- [x] 1.3 Add `evidence/requirement-test-matrix.md` mapping all 12 requirements and all 67 scenarios to exact test modules, live checks, and evidence status; keep blocked or partial rows explicit.
- [x] 1.4 Add a live-schema characterization fixture from redacted Herdr 0.7.5 output proving workspace inventory includes `workspace_id`, `label`, `agent_status`, pane/tab counts, and active tab; document that the rejected “workspace status does not exist” review finding was disproved by live evidence.

## 2. Atomic typed configuration, secure local state, and portable paths

- [x] 2.1 Add failing table-driven Pydantic/config tests for cleanup defaults, strict booleans, scalar bounds, pattern count/length/control characters, merged-section validation, config get/set/list, hot re-read, and byte-for-byte preservation after rejection.
- [x] 2.2 Add failing settings persistence tests that inject write, flush, fsync, and `os.replace` failures and prove the prior file remains intact; cover both `settings_service._save()` and `config_service._save_raw()`.
- [x] 2.3 Implement strict `CleanupConfig`, `cleanup.*` registration, merged-section validation, typed getters/setters, and owner-only temp-file + fsync + atomic-replace persistence until 2.1–2.2 pass.
- [x] 2.4 Add failing parametrized memory tests for exact `llm`/`append`, finite `0 < compile_timeout_s <= 3600`, invalid-write byte preservation, existing enabled/threshold compatibility, and environment precedence.
- [x] 2.5 Implement shared memory validators and symmetric writes without changing the existing runtime precedence.
- [x] 2.6 Add failing tests proving an already-`0700` database directory causes no chmod call/warning while a genuinely insecure directory attempts correction and warns only on correction failure.
- [x] 2.7 Update `_ensure_db_dir()` to stat first and skip redundant chmod; run the focused permission tests in a read-only-directory fixture.
- [x] 2.8 Add failing `tmp_path`/`monkeypatch` tests for tilde agent/skill discovery and source loading, stored-spelling round trip, absolute-path order, canonical disabled matching, newly activated legacy tilde entries, sensitive-root rejection, and CAO-owned `.aws/cli-agent-orchestrator` defaults.
- [x] 2.9 Resolve every extra agent/skill discovery and source-load boundary through validated `normalized_path()` results, preserving configured spelling and never logging resolved sensitive paths.
- [ ] 2.10 Run Black, isort, zero-error mypy, and focused configuration/path/permission tests.

## 3. Kimi initialization availability and ready-state recognition

- [x] 3.1 Add a failing async regression proving a blocked `_handle_startup_dialog` does not block an event-loop heartbeat; use a threading barrier, not sleeps.
- [x] 3.2 Add failing Kimi fixtures for ready-without-upgrade, supported upgrade reminder, lingering dialog text, unknown output, timeout, native Herdr ready state, sanitized failure, and the final `wait_until_status` gate.
- [x] 3.3 Offload `_handle_startup_dialog` with `asyncio.to_thread`, factor side-effect-free ready detection, and terminate promptly on a recognized prompt without weakening exact `IDLE`/`COMPLETED` initialization semantics.
- [x] 3.4 Add API-level regression coverage proving `/health` remains responsive while Kimi startup polling is held behind the barrier.
- [x] 3.5 Run Kimi, provider-base, deferred-init, CAO assign/callback, health, and task-leak tests with zero failures before any exact-K3 retry.

## 4. Transactional receiver cascade and SQLite contention

- [x] 4.1 Add failing real-SQLite tests proving single-terminal deletion atomically removes pending/delivered receiver rows, preserves live-receiver messages from deleted senders, cascades multi-terminal sessions, and rolls back both tables on injected failure.
- [x] 4.2 Add a deterministic two-connection race using barriers proving receiver creation and terminal deletion leave no orphan; add bounded busy-timeout exhaustion coverage without sleep-based timing.
- [x] 4.3 Configure a bounded SQLite timeout and implement `BEGIN IMMEDIATE` receiver validation/insertion plus one transactional receiver-inbox/terminal deletion primitive; preserve public return contracts.
- [x] 4.4 Route `delete_terminal` and `delete_terminals_by_session` through the primitive and run database, inbox, peer-inbox, terminal-service, and session-service regressions.

## 5. Strict Herdr inventory and startup reconciliation

- [x] 5.1 Add failing Herdr backend contract tests before implementation for complete workspace payloads, native status mapping, stable `workspace_id`, duplicate labels, non-dict elements, missing keys, wrong types, unrecognized status, and unchanged tmux/public three-key session shape.
- [x] 5.2 Update `HerdrBackend` with strict typed workspace inventory, native `list_sessions().status`, non-dict guards, and stable-ID revalidation/close helpers; inventory and test all callers.
- [x] 5.3 Add failing reconciliation tests for missing workspace, valid empty workspace inventory, missing tab, `__peers__`/provider-peer exclusion, duplicate labels, command failure, timeout, malformed JSON, missing envelope keys, wrong types, malformed elements, transactional cascade, and content-free summary counts.
- [x] 5.4 Refactor reconciliation into a synchronous strict parser/repository function executed through `asyncio.to_thread`; fail closed on incomplete inventory, accept a valid empty list, compare every persisted non-peer CAO session, and use the transactional deletion primitive.
- [x] 5.5 Add a deterministic startup responsiveness test and run Herdr backend/inbox, socket-loop, peer, database, and startup regressions.

## 6. Stable-identity completed-workspace cleanup engine

- [x] 6.1 Add `test/services/test_runtime_resource_cleanup.py` with functional fixture factories and parametrized IDs for disabled policy, tmux no-op, exact native `done`, every nonterminal/malformed state, prefix/peer/zero-row gates, local-naive grace, aware/missing/future timestamps, pending inbox, preserve patterns, duplicates, deterministic ordering, and batch limit.
- [x] 6.2 Add failing revalidation tests for status change, pending-inbox arrival, workspace disappearance, label reuse with a new `workspace_id`, backend-close failure, and authoritative already-absent cleanup.
- [x] 6.3 Add failing backend-first ordering and crash-resume tests proving DB mappings survive close failure and startup reconciliation completes persistence deletion after a successful close/crash boundary.
- [x] 6.4 Add failing failure-isolation/privacy tests proving one teardown error does not abort the batch, no immediate retry occurs, reason counts are stable, and message/output/token/environment/path sentinels never appear.
- [x] 6.5 Implement the pure selection model plus bounded execution service with injected backend, settings, repositories, clocks, registry, and session-service boundary; capture/revalidate `workspace_id` and keep evaluation side-effect free.
- [x] 6.6 Extend the automatic session-service path to snapshot, close the captured backend ID first, then release provider/environment/plugin/database state; leave legacy/manual public behavior compatible.
- [x] 6.7 Remove plaintext memory keys from success/failure retention logs and run cleanup, backend, provider cleanup, plugin event, API session, CLI shutdown, and both MCP session-tool regressions.

## 7. Lifespan daemon, cross-process lock, and clock anomaly handling

- [x] 7.1 Add deterministic async tests using injected wakeups, wall/monotonic clocks, and threading barriers for delayed first sweep, settings re-read, runtime disable, in-process non-overlap, cross-process lock contention, wall-clock jump, request-loop responsiveness, cancellation during sleep, and cancellation during a worker.
- [x] 7.2 Add a failing teardown-order test proving the shielded worker finishes before plugin registry teardown and no live cleanup task/thread remains.
- [x] 7.3 Implement the lifespan-owned daemon with stored worker task, `asyncio.Lock`, nonblocking `fcntl.flock`, `asyncio.shield`, cancellation/await semantics, and clock-skew guard.
- [x] 7.4 Add health/REST/WebSocket/MCP/TrustedHost/CORS/static-UI regressions proving cleanup adds no route, scope, authentication, or trust-boundary change.
- [x] 7.5 Repeat the concurrency suite under `pytest-repeat` or an equivalent deterministic loop and assert zero overlapping sweeps, duplicate deletions, live worker tasks, or receiver-orphan rows.

## 8. Documentation, isolated runtime proof, and rollback

- [x] 8.1 Update configuration, Herdr, terminal lifecycle, CLI/API, and provider troubleshooting docs with the exact cleanup schema, native-status contract, stable-ID gates, default-off behavior, tmux non-support, memory append command, Kimi startup behavior, portable-path activation, sensitive-root policy, observability, and irreversible-delete warning.
- [x] 8.2 Document recovery: back up settings/database, disable only future sweeps with `cao config set cleanup.completed_sessions_enabled false`, preserve active-supervisor patterns, and distinguish CAO resource cleanup from disk-capacity remediation.
- [x] 8.3 Start this worktree's server with an isolated port/database/Herdr session and prove default-off behavior, hot re-read, exact-done deletion, blocked/idle/unknown/pending/preserved survival, label-reuse safety, valid-empty reconciliation, zero receiver orphans, Kimi startup responsiveness, and continued `/health`.
- [ ] 8.4 Restart the production loopback source-backed server only after isolated proof; preserve `cao-embed-ops-exec-supervisor-herdr`, verify exact executable/import paths and health, and apply no destructive cleanup policy without a conservative grace/preserve configuration.
- [x] 8.5 Run markdown-link validation and `rtk git diff --check`; store redacted evidence under the change.
- [x] 8.6 Add a deterministic integration harness with fake provider CLIs that exercises source-backed server startup, supervisor creation, worker assignment, callback/inbox delivery, idle transition, failure reporting, terminal deletion, and leak-free shutdown in ordinary CI.
- [x] 8.7 Add an opt-in live integration matrix for exact Codex, Claude, Antigravity/Gemini, and Kimi models. Preflight binary, authentication, model, MCP, Herdr, and disk capacity; fail with explicit lane evidence on missing prerequisites; never silently skip, relaunch, or downgrade. The supervisor owns all worker lifecycle and produces one fan-in verdict.

## 9. Full verification and supervisor-owned adversarial cycle

- [ ] 9.1 Run the complete CI-equivalent non-E2E Python suite plus Black, isort, zero-error `mypy src/`, markdown links, `rtk git diff --check`, and gitleaks; record exact outcomes.
- [ ] 9.2 Run a read-only CAO testing swarm for Pydantic/config atomicity, SQLite race/cascade, async lifecycle/leaks, Herdr status/identity/reconciliation, Kimi readiness, and tmux compatibility; the supervisor owns every worker, callback, retry, and cleanup.
- [ ] 9.3 Run read-only adversarial CAO reviews with exact Claude Opus 5, Gemini Pro High, Kimi K3 only after tasks 3.1–3.5 pass, and Codex Sol only when exact quota is available; never downgrade and report blocked evidence explicitly.
- [ ] 9.4 Reproduce every critical/high finding locally, write a failing regression first, remediate one item at a time, rerun focused/full gates, and repeat the supervisor cycle until no critical/high finding survives.
- [ ] 9.5 Run strict OpenSpec validation and an independent `implementation-verification` requirement-to-task-to-test-to-evidence trace; do not use the stale `review-verification-protocol`.
- [ ] 9.6 Prepare an isolated commit and PR-ready summary cross-linking `embed-cao-ops-streamable-http`, runtime evidence, compatibility, deployment order, and rollback; do not push, open a PR, merge, deploy, archive, or delete evidence without explicit authorization.

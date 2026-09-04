# CAO Runtime Reliability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local CAO Ops three-worker fan-out, Kimi first-task delivery, automatic Herdr cleanup, and Antigravity MCP cleanup deterministic and restart-safe.

**Architecture:** Keep the embedded `/mcp/ops` server in-process, but route every registered async tool through its active `AsgiRequestBackend`; retain the synchronous helper only for direct legacy unit calls. Treat provider first-message preparation as a two-phase operation: prepare before paste, commit only after confirmed worker start. Make automatic workspace cleanup reuse CAO's existing snapshot/runtime/row teardown primitives under the session lifecycle lock, and keep Antigravity ownership metadata outside the provider's user-facing MCP entries.

**Tech Stack:** Python 3.10, FastAPI/Starlette, FastMCP Streamable HTTP, `httpx`, `asyncio`, SQLAlchemy, tmux/Herdr backends, pytest, pytest-asyncio, Black, isort, mypy, and ruff.

**Spec:** Completed local CAO adversarial review findings from the preceding review; no separate OpenSpec artifact was supplied for this repair.

## Global Constraints

- Preserve the existing dirty worktree and stage only files belonging to the current repair track.
- Keep the fix local; do not use AWS, production CAO, or provider-side edits as acceptance evidence.
- Embedded MCP requests must use the authenticated request's authorization context; never fall back to a machine token inside an active MCP request.
- Do not add `CAO_TERMINAL_ID` to third-party MCP environments merely to make stale-entry pruning possible.
- Automatic teardown must be idempotent, lock-protected, and must retain terminal rows when provider cleanup is deferred.
- Record the five existing provider-suite failures before implementation and require the same failures, not new failures, after the repair.
- Do not claim supervisor fan-out success from unit tests alone; require a bounded local three-worker CAO Ops runtime test.

---

### Task 1: Capture the baseline and isolate the repair commits

**Files:**

- Read: `git status --short --branch`
- Read: `pyproject.toml`
- Read: `test/ops_mcp_server/test_server.py`
- Read: `test/ops_mcp_server/test_embedded_http.py`
- Read: `test/services/test_session_service.py`
- Read: `test/services/test_deferred_submit_verification.py`

**Interfaces:**

- Consumes: the current branch and its existing 17-file dirty diff.
- Produces: a baseline list of the five existing provider-suite failures and a file-level mapping for Tasks 2-5.

- [ ] **Step 1: Record repository state without staging or editing files.**

  Run:

  ```bash
  rtk git status --short --branch
  rtk git diff --stat
  rtk git diff --check
  ```

  Expected: the current user-owned dirty files are listed; no review-generated files are present; whitespace diagnostics are empty.

- [ ] **Step 2: Run the baseline provider suite and record exact failure node IDs.**

  Run:

  ```bash
  rtk uv run pytest -q -m 'not e2e and not integration'
  ```

  Expected: preserve the current baseline of `368 passed, 3 skipped, 5 failed, 2 warnings` if it is unchanged. Record the five failing node IDs and do not alter them in this plan.

- [ ] **Step 3: Create one implementation commit per repair track.**

  Keep the tracks separate so a reviewer can accept or reject Ops transport, provider delivery, Herdr teardown, and Antigravity ownership independently. Never stage unrelated existing changes.

---

### Task 2: Route embedded Ops `launch_session` through the active async backend

**Files:**

- Modify: `src/cli_agent_orchestrator/ops_mcp_server/server.py:404-448`
- Test: `test/ops_mcp_server/test_server.py:285-330`
- Test: `test/ops_mcp_server/test_embedded_http.py:500-590`

**Interfaces:**

- Consumes: `_active_backend`, `_request_from_active_backend()`, `AsgiRequestBackend`, and the existing `LaunchResult` response contract.
- Produces: `_launch_session_impl()` that awaits the active backend for embedded MCP requests while preserving direct-call compatibility when no backend is bound.

- [ ] **Step 1: Add a failing unit test for active-backend dispatch.**

  In `test/ops_mcp_server/test_server.py`, bind a fake async backend through `_active_backend.set(...)`, call `_launch_session_impl()`, and assert that `request_json("post", "/sessions", ...)` was awaited with the existing params/body. Patch `requests.request` to raise if called. Reset the context variable in a `finally` block.

- [ ] **Step 2: Run the new test and confirm the current failure.**

  Run:

  ```bash
  rtk uv run pytest -q test/ops_mcp_server/test_server.py -k active_backend
  ```

  Expected: the test fails because `_launch_session_impl()` currently calls synchronous `_request_json()`.

- [ ] **Step 3: Replace only the embedded-path request call.**

  In `_launch_session_impl()`, replace the direct `_request_json(...)` call with:

  ```python
  session_data, error = await _request_from_active_backend(
      "post",
      "/sessions",
      params=params,
      json=body,
      operation="Launch session",
  )
  ```

  Do not remove `_request_json()` or change its direct-call fallback. Preserve response validation, canonical session-name handling, and error mapping.

- [ ] **Step 4: Add a bounded three-call embedded regression test.**

  In `test/ops_mcp_server/test_embedded_http.py`, use `stack_factory`, initialize one MCP session, and issue three concurrent JSON-RPC `tools/call` requests for `launch_session` with distinct session names. Wrap `asyncio.gather(...)` in `asyncio.wait_for(..., timeout=2)`. Assert all three responses are HTTP 200 with successful structured results and that the REST test app observed three authorization-bearing `/sessions` calls. Patch `get_local_bearer` to raise so machine-token fallback is impossible.

- [ ] **Step 5: Run the focused Ops tests.**

  Run:

  ```bash
  rtk uv run pytest -q test/ops_mcp_server/test_server.py test/ops_mcp_server/test_embedded_http.py
  ```

  Expected: existing refresh-header scope test passes, the three concurrent calls complete under the timeout, and direct helper tests still pass.

- [ ] **Step 6: Commit only this track.**

  ```bash
  rtk git add src/cli_agent_orchestrator/ops_mcp_server/server.py test/ops_mcp_server/test_server.py test/ops_mcp_server/test_embedded_http.py
  rtk git commit -m "fix(ops-mcp): use active async backend for launches"
  ```

---

### Task 3: Complete Kimi first-message preparation and deferred redelivery

**Files:**

- Modify: `src/cli_agent_orchestrator/services/terminal_service.py:1150-1420,1590-1740`
- Test: `test/services/test_deferred_submit_verification.py:150-205`
- Test: `test/services/test_terminal_service_full.py:1460-1545`
- Test: `test/providers/test_kimi_cli_unit.py:700-825`

**Interfaces:**

- Consumes: `BaseProvider.prepare_input()`, `BaseProvider.commit_prepared_input()`, Kimi's `is_input_ready`, and the persisted `profile_prompt_delivered` flag.
- Produces: `send_input(..., _commit_prepared_input=True, _prepare_provider_input=True)` with a real preparation/commit contract; deferred initial sends and retries use `_commit_prepared_input=False` until worker start is confirmed.

- [ ] **Step 1: Add failing preparation and commit tests.**

  Extend the terminal-service tests to assert:

  1. `send_input()` calls `provider.prepare_input()` before `send_keys()`.
  2. A successful ordinary send calls `commit_prepared_input()` and persists `profile_prompt_delivered`.
  3. A backend send exception leaves the provider prefix and persisted flag unchanged.
  4. Deferred initial delivery and every redelivery pass `_commit_prepared_input=False`.
  5. The deferred path commits exactly once only after `_confirm_worker_started_or_resubmit()` returns true.

- [ ] **Step 2: Run the focused tests and confirm the current failures.**

  Run:

  ```bash
  rtk uv run pytest -q test/services/test_deferred_submit_verification.py test/services/test_terminal_service_full.py -k 'prepar or deferred or profile'
  ```

  Expected: the existing `_commit_prepared_input` assertion and the Kimi preparation assertions expose the missing production calls.

- [ ] **Step 3: Implement the two-phase send contract.**

  In `send_input()`:

  1. Keep `original_message` unchanged for plugin events.
  2. If `_prepare_provider_input` is true and a provider exists, call `provider.prepare_input(message)` before `inject_memory_context()` and `get_backend().send_keys()`. This makes a preparation failure leave the memory-injection latch untouched and keeps Kimi's profile prefix outermost.
  3. Apply the existing memory injection to the prepared message and do not log the transformed prompt.
  4. After `send_keys()` succeeds, call `provider.commit_prepared_input()` only when `_commit_prepared_input` is true. If it returns true, call `_persist_profile_prompt_delivered(terminal_id)`.
  5. If `_prepare_provider_input` is false, do not prepare or commit provider first-message state. Use this for exit/control input.

  In `_schedule_deferred_init()` and `redeliver_dropped_message()`, pass `_commit_prepared_input=False` to every initial-task send. After the deferred confirmation reports a started worker, commit once and persist the flag. A failed confirmation must leave the prefix available for a retry or teardown.

- [ ] **Step 4: Verify restart behavior.**

  Add a Kimi provider-manager test that restores `profile_prompt_delivered=False`, prepares the first task with the profile/security prefix, commits it once, and then verifies the second task has no duplicate prefix. Add the corresponding `True` case to verify no prefix is rebuilt.

- [ ] **Step 5: Run the focused Kimi and deferred-delivery tests.**

  ```bash
  rtk uv run pytest -q test/services/test_deferred_submit_verification.py test/services/test_terminal_service_full.py test/providers/test_kimi_cli_unit.py test/providers/test_provider_manager_unit.py
  ```

  Expected: first-task preparation is present exactly once, deferred retries do not consume it early, and existing non-Kimi sends retain their behavior.

- [ ] **Step 6: Commit only this track.**

  ```bash
  rtk git add src/cli_agent_orchestrator/services/terminal_service.py test/services/test_deferred_submit_verification.py test/services/test_terminal_service_full.py test/providers/test_kimi_cli_unit.py test/providers/test_provider_manager_unit.py
  rtk git commit -m "fix(input): complete deferred provider preparation"
  ```

---

### Task 4: Make automatic Herdr cleanup use the existing teardown primitives

**Files:**

- Modify: `src/cli_agent_orchestrator/services/session_service.py:541-600`
- Modify: `src/cli_agent_orchestrator/services/terminal_service.py:2160-2340`
- Test: `test/services/test_session_service.py:700-end`
- Test: `test/services/test_terminal_service_full.py:2160-2420`
- Test: `test/services/test_runtime_resource_cleanup.py`

**Interfaces:**

- Consumes: `session_lifecycle_lock()`, `capture_terminal_snapshot()`, `dismantle_terminal_runtime()`, `delete_terminal_row()`, `delete_terminals_by_ids()`, and `HerdrBackend.close_workspace_by_id()`.
- Produces: an automatic cleanup path that performs identity validation, workspace close, runtime teardown, and row deletion in a single session-locked transaction boundary without undefined helpers or unsupported keyword arguments.

- [ ] **Step 1: Add a failing happy-path cleanup test.**

  In `test/services/test_session_service.py`, mock a Herdr backend with one matching workspace and one terminal. Assert that automatic cleanup captures the terminal before closing the exact workspace ID, calls `dismantle_terminal_runtime(terminal_id, snapshot, kill_window=False)`, deletes the row with `delete_terminal_row()`, clears session environment, and returns the canonical session in `deleted`. The test must fail against the current code because `prepare_terminal_for_backend_close` does not exist.

- [ ] **Step 2: Add failure-safety and lock tests.**

  Cover these cases:

  1. label/ID mismatch raises before backend close and preserves terminal rows;
  2. absent label with a surviving expected ID raises identity-change error;
  3. deferred provider cleanup keeps that terminal row and reports an error for retry;
  4. two automatic teardowns for the same session serialize through `session_lifecycle_lock()`.

- [ ] **Step 3: Replace the broken terminal call sequence.**

  In `delete_session_automatically()`:

  1. Normalize the session name and enter `session_lifecycle_lock(session_name)` before reading inventory, terminals, or closing the workspace.
  2. Capture `(terminal_id, metadata)` for every terminal before `close_workspace_by_id(expected_backend_id)`.
  3. Close only the validated workspace identity.
  4. For each captured terminal, call `dismantle_terminal_runtime(..., kill_window=False)` because the Herdr workspace close already removed the tabs, then call `delete_terminal_row()` when runtime cleanup is not deferred.
  5. Use the existing scoped row sweep only for rows whose runtime cleanup completed; retain deferred rows for retry.
  6. Dispatch plugin events after releasing the lifecycle lock, matching `delete_session()`.

  Remove the undefined `prepare_terminal_for_backend_close()` call and the unsupported `backend_already_closed`/`prepared` arguments. Remove the partial `backend_already_closed` parameter from `dismantle_terminal_runtime()` if no remaining call site requires it; `kill_window=False` is the explicit automatic-cleanup contract.

- [ ] **Step 4: Run the teardown tests.**

  ```bash
  rtk uv run pytest -q test/services/test_session_service.py test/services/test_terminal_service_full.py test/services/test_runtime_resource_cleanup.py
  ```

  Expected: no `TypeError`, no undefined-helper lookup, validated identities remain fail-closed, and deferred cleanup remains retryable.

- [ ] **Step 5: Commit only this track.**

  ```bash
  rtk git add src/cli_agent_orchestrator/services/session_service.py src/cli_agent_orchestrator/services/terminal_service.py test/services/test_session_service.py test/services/test_terminal_service_full.py test/services/test_runtime_resource_cleanup.py
  rtk git commit -m "fix(cleanup): make automatic Herdr teardown restart-safe"
  ```

---

### Task 5: Persist Antigravity MCP ownership without leaking identity to third-party servers

**Files:**

- Modify: `src/cli_agent_orchestrator/providers/antigravity_cli.py:430-610`
- Modify: `docs/antigravity-cli.md:75-85`
- Test: `test/providers/test_antigravity_cli_unit.py:300-670`

**Interfaces:**

- Consumes: the existing shared config lock, unique per-terminal MCP keys, `get_terminal_metadata()`, and `_mcp_server_names`.
- Produces: a CAO-owned sidecar ownership map beside `mcp_config.json`, used only for stale pruning and cleanup; the provider continues to inject `CAO_TERMINAL_ID` only into identity-bearing CAO MCP entries.

- [ ] **Step 1: Add failing ownership tests.**

  Add tests proving:

  1. a third-party entry has no `CAO_TERMINAL_ID` in the generated provider config;
  2. its CAO-generated per-terminal key is recorded in the ownership sidecar;
  3. graceful cleanup removes the config entry and its sidecar record;
  4. a crashed terminal's non-identity entry is pruned when its terminal metadata is absent;
  5. a live terminal's entry is preserved;
  6. malformed sidecar data does not delete user MCP entries.

- [ ] **Step 2: Implement sidecar read/write helpers.**

  Use the exact CAO-owned sibling path `mcp_config.json.cao-ownership` and store a versioned map of `unique_key -> terminal_id`. Read, update, and atomically replace this file under `_MCP_CONFIG_WRITE_LOCK`, with mode `0600`. Keep config-file writes and sidecar writes in the same critical section.

- [ ] **Step 3: Integrate ownership into register, unregister, and stale-prune.**

  During registration, add each generated key to the sidecar while preserving the current config entry shape. During cleanup, remove only keys owned by the provider instance and delete their sidecar records. During stale pruning, consult the sidecar's terminal IDs and remove only entries whose CAO terminal metadata no longer exists; retain legacy entries without sidecar ownership unless their existing identity-bearing check proves ownership. Never add an ownership marker to the third-party MCP environment or command arguments.

- [ ] **Step 4: Update the Antigravity documentation.**

  Document that CAO uses a private mode-0600 ownership sidecar for crash recovery, that user MCP entries are preserved, and that only identity-bearing CAO servers receive `CAO_TERMINAL_ID`.

- [ ] **Step 5: Run the focused provider tests.**

  ```bash
  rtk uv run pytest -q test/providers/test_antigravity_cli_unit.py test/providers/test_provider_http_mcp.py
  ```

  Expected: third-party config entries remain identity-free, stale CAO-owned entries are removed, and unrelated user entries survive.

- [ ] **Step 6: Commit only this track.**

  ```bash
  rtk git add src/cli_agent_orchestrator/providers/antigravity_cli.py docs/antigravity-cli.md test/providers/test_antigravity_cli_unit.py test/providers/test_provider_http_mcp.py
  rtk git commit -m "fix(antigravity): track CAO MCP ownership separately"
  ```

---

### Task 6: Run the full local verification gates

**Files:**

- Read: `.pre-commit-config.yaml`
- Read: `pyproject.toml`
- Read: `docs/antigravity-cli.md`
- Read: `docs/superpowers/plans/2026-08-31-cao-runtime-reliability-fixes.md`

**Interfaces:**

- Consumes: all four repaired tracks and the baseline failure manifest.
- Produces: local evidence separating repaired regressions from the five pre-existing provider failures.

- [ ] **Step 1: Run all focused regression tests together.**

  ```bash
  rtk uv run pytest -q \
    test/ops_mcp_server/test_server.py \
    test/ops_mcp_server/test_embedded_http.py \
    test/services/test_deferred_submit_verification.py \
    test/services/test_session_service.py \
    test/services/test_terminal_service_full.py \
    test/services/test_runtime_resource_cleanup.py \
    test/providers/test_kimi_cli_unit.py \
    test/providers/test_provider_manager_unit.py \
    test/providers/test_antigravity_cli_unit.py \
    test/providers/test_provider_http_mcp.py
  ```

- [ ] **Step 2: Run repository quality checks.**

  ```bash
  rtk uv run black --check src test
  rtk uv run isort --check-only src test
  rtk uv run mypy src
  rtk ruff check src test
  rtk git diff --check
  ```

  Expected: no new lint/type errors in touched files. If existing ruff failures remain, record their exact paths and do not hide them by weakening rules.

- [ ] **Step 3: Rerun the full non-E2E, non-integration suite.**

  ```bash
  rtk uv run pytest -q -m 'not e2e and not integration'
  ```

  Compare the failure node IDs with Task 1. The repair is not accepted if any new failure appears, even if the total count stays at five.

- [ ] **Step 4: Run the local runtime fan-out test.**

  Start the local CAO service from this checkout and verify the exact Streamable HTTP route `/mcp/ops`. With a bounded timeout, use a supervisor CAO Ops client to launch three workers, assert three distinct terminal IDs are created, deliver one uniquely tagged task to each, observe each worker reach a started/completed state, collect each result, and shut down every created session. Record route, session, and callback evidence without printing bearer tokens or private configuration.

  Acceptance: all three workers are created before the timeout; no 404 occurs; request authorization reaches the REST boundary; each callback is emitted once; every attempted worker is cleaned up.

---

### Task 7: Adversarial re-review and release decision

**Files:**

- Read: the final diff for all repaired tracks
- Read: the five baseline failure node IDs

**Interfaces:**

- Consumes: focused tests, full-suite comparison, local three-worker evidence, and the final diff.
- Produces: a severity-first review decision with explicit residual risk.

- [ ] **Step 1: Launch fresh local CAO review sessions for Codex, Agy, and Kimi.**

  Give each reviewer a review-only prompt containing the repaired diff, the original findings, the acceptance criteria, and the requirement to inspect async cancellation, auth propagation, duplicate first-task delivery, cleanup idempotence, and MCP ownership. Do not permit edits, commits, publication, AWS access, or further agent launches.

- [ ] **Step 2: Require structured findings.**

  Each reviewer must return `PASS` or `REQUEST_CHANGES`, severity, file/line, source evidence, impact, and a minimal correction. Treat any surviving high-severity finding or any disagreement about worker creation/cleanup as blocking.

- [ ] **Step 3: Reconcile findings against runtime evidence.**

  The local three-worker test is authoritative for local lifecycle behavior. Unit tests are authoritative for deterministic contracts. Existing five baseline failures remain documented residual risk unless separately fixed.

- [ ] **Step 4: Close every temporary review session and verify state.**

  Confirm no temporary CAO or tmux session remains, preserve unrelated sessions, rerun `git status --short --branch`, and confirm only the intended implementation commits and the plan are present.

**Completion criteria:** no blocking review finding remains; the three-worker local fan-out creates all workers; embedded auth and async transport tests pass; Kimi preparation is delivered and committed exactly once; automatic Herdr cleanup has no undefined or unsupported call; Antigravity stale cleanup is ownership-safe; and the five unrelated provider failures are unchanged and explicitly reported.

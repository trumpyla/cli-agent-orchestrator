# Mypy-Zero Repair and Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate every `mypy src/` error without changing CAO runtime
contracts, then prove the repaired checkout by running focused tests, the
complete non-E2E suite, and a live daemon integration test from this source.

**Architecture:** CAO-specific AG-UI correlation fields become declared
Pydantic v2 event subclasses inside the existing optional dependency boundary.
The remaining errors are repaired at their actual boundaries with precise
Callable, Protocol, JSON, Optional, and factory types. CI and OpenSpec move from
an error-ratchet policy to an enforced zero-error gate only after the full
source tree type-checks.

**Tech Stack:** Python 3.10+ typing, Pydantic v2, `ag-ui-protocol` 0.1.x, pytest,
mypy strict mode, uv, FastAPI/Uvicorn, GitHub Actions, CAO MCP.

## Global Constraints

- Preserve official `RunStartedEvent` and `RunFinishedEvent` wire aliases
  `threadId` and `runId`.
- Preserve CAO-only correlation keys exactly as `thread_id`, `run_id`, and
  `step_id`.
- Keep AG-UI optional: importing CAO without the `agui` extra must not fail.
- Do not add `type: ignore`, mypy overrides, or relaxed checker settings.
- Completion requires a fresh `uv run mypy src/` result of zero; the initial
  88-errors/8-files inventory is diagnostic only.
- Use `uv` to add `types-jsonschema` and update `uv.lock`.
- Preserve all pre-existing peer-inbox worktree changes.
- After each task group, launch read-only Claude and Agy reviewers through the
  CAO MCP control plane and resolve every P0-P2 finding before continuing.

---

### Task 1: Typed AG-UI Events and Exact Wire Serialization

**Files:**
- Create: `src/cli_agent_orchestrator/services/agui/run_plane_events.py`
- Modify: `src/cli_agent_orchestrator/services/agui/run_plane.py`
- Modify: `test/services/agui/test_run_plane.py`
- Test: `test/services/agui/test_run_plane_heartbeat.py`

**Interfaces:**
- Produces typed `CaoRunErrorEvent`, `CaoStateSnapshotEvent`,
  `CaoStateDeltaEvent`, `CaoStepStartedEvent`, `CaoStepFinishedEvent`,
  `CaoToolCallStartEvent`, `CaoToolCallEndEvent`, and `CaoCustomEvent`.
- Each class declares `thread_id` and `run_id`; step classes also declare
  `step_id`. Explicit Pydantic `serialization_alias` values preserve snake_case.
- `run_plane.py` imports these classes only inside its existing guarded AG-UI
  import block.

- [ ] **Step 1: Add failing exact-serialization tests**

  Parameterize every CAO subclass. Encode each with the installed
  `EventEncoder`, parse the `data:` frame, and assert:

  ```python
  assert payload["thread_id"] == "thread-1"
  assert payload["run_id"] == "run-1"
  assert "threadId" not in payload
  assert "runId" not in payload
  ```

  For step events also assert `step_id` exists and `stepId` does not. Separately
  retain the existing official `RUN_STARTED`/`RUN_FINISHED` assertions for
  `threadId` and `runId`.

- [ ] **Step 2: Verify RED**

  Run:

  ```bash
  uv run pytest test/services/agui/test_run_plane.py -q
  ```

  Expected: collection/import failure because `run_plane_events` and its typed
  classes do not exist.

- [ ] **Step 3: Implement the minimal typed event module**

  Subclass the corresponding official SDK models, declaring fields with:

  ```python
  thread_id: str = Field(serialization_alias="thread_id")
  run_id: str = Field(serialization_alias="run_id")
  ```

  Step subclasses additionally declare:

  ```python
  step_id: str = Field(serialization_alias="step_id")
  ```

- [ ] **Step 4: Replace undeclared extras and literal event strings**

  Use the CAO subclasses for extended events, `EventType` members for every SDK
  event, distinct typed local names for translated event variants, and a typed
  encoder constructor that normalizes `Optional[str]` without changing runtime
  negotiation.

- [ ] **Step 5: Verify GREEN and type-check the group**

  Run:

  ```bash
  uv run pytest test/services/agui/test_run_plane.py \
    test/services/agui/test_run_plane_heartbeat.py -q
  uv run mypy src/cli_agent_orchestrator/services/agui/run_plane.py \
    src/cli_agent_orchestrator/services/agui/run_plane_events.py
  ```

- [ ] **Step 6: CAO adversarial review**

  Launch persistent read-only `claude_max` and `antigravity_max` CAO sessions.
  Require evidence-backed review of the task-group diff for alias correctness,
  optional-import safety, EventType usage, lifecycle legality, and test
  adequacy. Cross-send the findings and resolve every P0-P2 result.

---

### Task 2: Strict Boundary Types and JSON Stubs

**Files:**
- Modify: `src/cli_agent_orchestrator/services/agui/approval_bridge.py`
- Modify: `src/cli_agent_orchestrator/services/status_monitor.py`
- Modify: `src/cli_agent_orchestrator/services/fifo_reader.py`
- Modify: `src/cli_agent_orchestrator/cli/commands/profile.py`
- Modify: `src/cli_agent_orchestrator/services/agent_scaffold.py`
- Modify: `src/cli_agent_orchestrator/models/workflow.py`
- Modify: `src/cli_agent_orchestrator/services/step_output_store.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `test/services/agui/test_approval_bridge.py`
- Test: `test/services/test_status_monitor.py`
- Test: `test/services/test_fifo_reader.py`
- Test: `test/cli/test_profile_cmd.py`
- Test: `test/services/test_agent_scaffold.py`

**Interfaces:**
- Approval callbacks are precise `Callable[[str], ...]` aliases.
- Status monitor collaborators use structural screen/stream Protocols and
  `BaseProvider | None` where provider methods are required.
- JSON loaders return `dict[str, Any]` only after runtime object validation.
- FIFO watchdog registration narrows both optional callbacks directly.

- [ ] **Step 1: Add failing behavior/type-contract tests**

  Add tests that exercise injected approval callbacks, rendered-screen
  collaborators, FIFO enrollment only when both callbacks exist, schema loading
  rejecting non-object roots, and typed template metadata lists.

- [ ] **Step 2: Verify RED**

  Run the five focused test modules and the focused mypy file list. Expected:
  tests exposing the missing JSON-object guards fail and mypy reports the
  existing Callable, Protocol, Optional, and import-untyped errors.

- [ ] **Step 3: Implement precise boundary types**

  Add Callable aliases and fully parameterized task/set types; add screen and
  stream Protocols; type provider parameters as `BaseProvider | None`; replace
  derived FIFO enrollment narrowing with an explicit branch on both callbacks;
  add a shared local JSON-object validation pattern and typed containers.

- [ ] **Step 4: Add stubs with uv**

  Run:

  ```bash
  uv add --dev types-jsonschema
  ```

  Remove the now-obsolete `import-untyped` ignores from `models/workflow.py`
  and `services/step_output_store.py`.

- [ ] **Step 5: Verify GREEN**

  Run focused pytest and focused mypy for every modified source file, then run
  `uv run mypy src/` to discover any new errors exposed by the stubs.

- [ ] **Step 6: CAO adversarial review**

  Run independent Claude and Agy CAO reviews of the task-group diff, focusing
  on Callable variance, Protocol structural compatibility, runtime JSON
  validation, FIFO behavior preservation, dependency placement, stale ignores,
  and test realism. Cross-audit and remediate every P0-P2 finding.

---

### Task 3: Archive Factory, Enforced Gates, and Contract Documentation

**Files:**
- Modify: `src/cli_agent_orchestrator/services/memory_archive/__init__.py`
- Modify: `test/services/test_memory_archive_registry.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/test-antigravity-cli-provider.yml`
- Modify: `.github/workflows/test-claude-code-provider.yml`
- Modify: `.github/workflows/test-codex-provider.yml`
- Modify: `.github/workflows/test-kiro-cli-provider.yml`
- Modify: `openspec/config.yaml`
- Modify: `docs/agui.md`
- Modify: `docs/api.md`
- Modify: `docs/control-planes.md`
- Modify: `docs/superpowers/specs/2026-07-22-openspec-project-config-design.md`

**Interfaces:**
- `MemoryArchiveBackendFactory.__call__(memory_service: MemoryService) ->
  MemoryArchiveBackend`.
- The registry stores factories, not abstract backend instance types.
- Every workflow that runs `mypy src/` treats failure as blocking.

- [ ] **Step 1: Add a failing factory-contract test**

  Register a fake factory that records the supplied `MemoryService`, retrieve
  it, construct the backend with that service, and assert the resulting backend
  exports/imports successfully. Keep unknown-format and ABC tests intact.

- [ ] **Step 2: Verify RED**

  Run:

  ```bash
  uv run pytest test/services/test_memory_archive_registry.py -q
  uv run mypy src/cli_agent_orchestrator/services/memory_archive/__init__.py \
    src/cli_agent_orchestrator/services/memory_service.py \
    src/cli_agent_orchestrator/api/main.py
  ```

  Expected: the current registry's `type[MemoryArchiveBackend]` constructor
  contract rejects the injected `MemoryService`.

- [ ] **Step 3: Implement the factory Protocol and update test fakes**

  Use a `TYPE_CHECKING` import for `MemoryService` to avoid a runtime cycle,
  type the registry as `dict[str, MemoryArchiveBackendFactory]`, and update fake
  factories to accept the service argument.

- [ ] **Step 4: Enforce zero-mypy policy everywhere**

  Remove `continue-on-error: true` from all five mypy workflows. Update OpenSpec
  and the project-specific design document from “no new mypy errors” to
  mandatory `mypy src/` zero.

- [ ] **Step 5: Document the wire contract**

  Document that resource/run-plane consumers use official SDK aliases except
  the CAO extensions `thread_id`, `run_id`, and `step_id`, and that reliable
  behavior is tested with both direct serialization and a live stock-client
  integration.

- [ ] **Step 6: Verify GREEN and CAO review**

  Run the focused registry tests, full mypy, workflow searches, OpenSpec
  validation, Markdown-link validation, and independent Claude/Agy CAO review.
  Cross-audit and resolve every P0-P2 finding.

---

## Integration Test Plan

### Static and focused gates

- [ ] Run all focused task-group tests.
- [ ] Run `uv run mypy src/` and require exit 0 with no errors.
- [ ] Run `uv run black --check src/ test/`.
- [ ] Run `uv run isort --check-only src/ test/`.
- [ ] Run `uv run python scripts/validate_markdown_links.py`.
- [ ] Run `git diff --check`.

### Complete non-E2E Python suite

- [ ] Run the same command as CI:

  ```bash
  uv run pytest test/ \
    --ignore=test/providers/test_kiro_cli_integration.py \
    --ignore=test/e2e \
    -m "not e2e" \
    -q
  ```

### Source-backed daemon launch

- [ ] Stop only the known CAO daemon instance after resolving its exact PID and
  command line.
- [ ] Install this checkout as an editable uv tool or run the entry point with
  `uv run`, preserving the existing daemon configuration.
- [ ] Start `cao-server` from the repository root with a dedicated integration
  database and port, capture its PID, and verify:

  ```bash
  curl --fail http://127.0.0.1:<port>/health
  ```

- [ ] Inspect the running process executable, command line, cwd, and imported
  `cli_agent_orchestrator.__file__`; all must resolve to this checkout.

### Live AG-UI integration

- [ ] Start the daemon with `CAO_AGUI_ENABLED=true`.
- [ ] POST a minimal `RunAgentInput` to `/agui/v1/run`.
- [ ] Parse the returned SSE frames and assert:
  - `RUN_STARTED` and `RUN_FINISHED` use `threadId` and `runId`.
  - An emitted CAO extended event uses exact `thread_id` and `run_id`.
  - A step event uses exact `step_id`.
  - No extension key is silently camel-cased.
- [ ] Run the repository stock-client live demo used by CI, when its Node
  dependencies are available, and require at least one post-connect frame.

### Operations-MCP and CAO launch integration

- [ ] Start `cao-ops-mcp-server` against the live source-backed daemon.
- [ ] Through the CAO MCP control plane, launch one `claude_max` and one
  `antigravity_max` session in this repository.
- [ ] Send a harmless source-identification prompt, poll both terminals to a
  terminal state, and read their outputs.
- [ ] Shut down both sessions and confirm they disappear from `list_sessions`.

### Final audit

- [ ] Re-run full mypy and the non-E2E suite after all reviewer remediations.
- [ ] Confirm the daemon still serves `/health` from this checkout.
- [ ] Confirm the worktree contains only intentional repair, test, plan, and
  documentation changes plus the pre-existing peer-inbox work.

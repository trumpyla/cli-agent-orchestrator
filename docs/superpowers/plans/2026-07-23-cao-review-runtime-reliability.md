# CAO Review Runtime Reliability Repair Plan

> Execute tests-first on branch `peer-inbox-bridge`. This plan addresses the
> runtime failures reproduced while Claude and Agy reviewed the OpenSpec design.

## Goal

Make CAO MCP-launched review sessions reliable: the first queued message must
not race provider initialization, returned session names must be canonical,
shutdown must not report success for absent sessions, operations MCP must be
able to answer interactive terminal prompts, and Claude status/output must
reflect the visible completed turn.

## Task 1: Canonicalize session identity and reject false-success deletion

**Files**

- Modify: `src/cli_agent_orchestrator/utils/terminal.py`
- Modify: `src/cli_agent_orchestrator/api/main.py`
- Modify: `src/cli_agent_orchestrator/services/session_service.py`
- Modify: `src/cli_agent_orchestrator/services/terminal_service.py`
- Modify: `src/cli_agent_orchestrator/ops_mcp_server/server.py`
- Test: `test/services/test_session_service.py`
- Test: `test/ops_mcp_server/test_server.py`
- Test: `test/api/test_api_endpoints.py`

**Steps**

1. Add tests proving an unprefixed requested name is returned as the actual
   `cao-...` name by `_launch_session_impl`.
2. Add tests proving get/delete accept both canonical and unprefixed names.
3. Add a test proving deletion of a name that has neither a backend session nor
   database terminals raises `ValueError` and maps to HTTP 404.
4. Add `normalize_session_name()` beside `generate_session_name()` and use it at
   API/service boundaries.
5. Return `session_data["session_name"]` from operations MCP and fail closed if
   the API response omits it.
6. Run the three focused test modules.

## Task 2: Prevent first-message startup loss

**Files**

- Modify: `src/cli_agent_orchestrator/providers/base.py`
- Modify: `src/cli_agent_orchestrator/providers/antigravity_cli.py`
- Modify: `src/cli_agent_orchestrator/providers/claude_code.py`
- Modify: `src/cli_agent_orchestrator/services/inbox_service.py`
- Test: `test/services/test_inbox_service.py`
- Test: `test/providers/test_antigravity_cli.py`

**Steps**

1. Add a failing inbox test where status is IDLE but the provider reports that
   initialization/input readiness is incomplete; assert the row remains pending
   and `send_input` is not called.
2. Add a provider contract property `is_input_ready`, defaulting to true for
   providers without an asynchronous startup surface and reflecting
   `_initialized` for Claude and Agy.
3. Gate normal and eager inbox delivery on that property before marking rows
   delivered.
4. Add an Agy settle check after its initial acknowledgement: capture the
   rendered pane twice, require stable content and the interactive input marker,
   and keep `_initialized` false until it passes. Fail initialization on timeout
   so launch never reports a session whose first prompt can still be lost.
5. Add tests covering stable readiness, changing startup content, and timeout.
6. Run the focused inbox and Agy tests.

## Task 3: Expose safe terminal prompt controls through operations MCP

**Files**

- Modify: `src/cli_agent_orchestrator/ops_mcp_server/server.py`
- Modify: `src/cli_agent_orchestrator/ops_mcp_server/models.py`
- Modify: `docs/control-planes.md`
- Modify: `docs/operations-mcp.md` if present, otherwise the canonical operations
  MCP section discovered by link/source search
- Test: `test/ops_mcp_server/test_server.py`
- Test: `test/ops_mcp_server/test_instructions.py`

**Steps**

1. Add failing tool tests for `send_terminal_input(terminal_id, message)` and
   `send_terminal_key(terminal_id, key)`.
2. Route both tools only through the existing authenticated HTTP endpoints.
   Do not call terminal services directly.
3. Use the current local bearer forwarding helper; never include token values in
   errors, results, or logs.
4. Preserve API validation: input conflicts remain errors and keys remain
   restricted by `TMUX_KEY_PATTERN`.
5. Document these as operator controls for interactive prompts, distinct from
   durable inbox delivery.
6. Run operations MCP tests and API scope/security tests.

## Task 4: Make Claude native status respect visible interactive/completed state

**Files**

- Modify: `src/cli_agent_orchestrator/providers/base.py`
- Modify: `src/cli_agent_orchestrator/providers/claude_code.py`
- Test: `test/providers/test_claude_code_unit.py`
- Test: `test/providers/test_native_status_shared.py`

**Steps**

1. Add captured-buffer tests proving:
   - a visible approval picker yields `WAITING_USER_ANSWER` even if native state
     says IDLE or COMPLETED;
   - a visibly completed turn with a stable prompt does not remain PROCESSING
     after the native flush interval;
   - active native PROCESSING is not overridden by stale historical prompts.
2. Add a Claude-specific pre-native screen classifier for active approval
   prompts and stable completed-turn boundaries.
3. Preserve the Herdr 10-second output flush interval and shared native status
   semantics for other providers.
4. Run both provider test modules.

## Task 5: Extract boxless Claude responses

**Files**

- Modify: `src/cli_agent_orchestrator/providers/claude_code.py`
- Test: `test/providers/test_claude_code_unit.py`
- Test: `test/services/test_terminal_service_full.py`

**Steps**

1. Add a fixture matching the reproduced newest-TUI shape: final response text
   without `⏺`/`●`, followed by a completion summary, separator, and idle prompt.
2. Assert `extract_last_message_from_script` returns only the final response.
3. Add negative tests so startup banners, user prompts, approval pickers, and
   stale earlier turns are not treated as final answers.
4. Add a conservative fallback extractor bounded by the last user-turn boundary
   and the completion-summary/idle-prompt boundary.
5. Assert terminal-service `mode=last` uses the fallback before returning
   `[NO RESPONSE]`.
6. Run both focused modules.

## Task 6: Full verification and runtime integration

**Commands**

1. Run focused tests from Tasks 1-5.
2. Run the complete non-E2E Python suite with the same exclusions as
   `.github/workflows/ci.yml`.
3. Run:

   ```bash
   uv run black --check .
   uv run isort --check-only .
   uv run mypy src/
   uv run python scripts/validate_markdown_links.py
   git diff --check
   ```

4. Install the current checkout as the daemon source using the repository's
   documented installation command. Restart the Herdr-configured daemon and
   prove its executable/import paths resolve to this checkout.
5. Through CAO MCP, launch fresh Claude and Agy sessions, immediately queue a
   uniquely tagged prompt, answer an interactive approval with the new terminal
   controls if one appears, wait for completion, retrieve `mode=last`, and shut
   down using the returned canonical session name.

## Task 7: Adversarial re-review

1. Launch fresh Claude and Agy CAO sessions through CAO MCP with this repository
   as their working directory.
2. Give both reviewers the repaired diff and the prior finding list.
3. Require source-backed structured findings with verdict, confidence, severity,
   file/line, evidence, impact, and proposed correction.
4. Exchange result sets between the reviewers through CAO MCP and require each
   to mark the other's findings surviving, rejected, revised, or missed.
5. Reproduce the union locally. Any session error, `request_changes`, or P0-P2
   finding blocks completion.
6. Apply confirmed corrections, rerun all gates, and repeat at most twice.
7. Save review evidence under
   `build/reports/reviews/peer-inbox-bridge/` and close both sessions.

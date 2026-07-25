## 1. Design refinement and baseline

- [x] 1.1 Record the clean CI-equivalent baseline with `uv sync --all-extras --dev` and `uv run pytest test/ --ignore=test/providers/test_kiro_cli_integration.py --ignore=test/e2e -m "not e2e"`; preserve any unrelated pre-existing failure as explicit evidence.
- [ ] 1.2 Launch the exact requested read-only design lanes; never downgrade an unavailable model. Record a blocked exact-model lane explicitly and keep this task unchecked until a report or user waiver exists.
  - Blocked evidence: Claude Fable was attempted twice through CAO and once through the documented acpx fallback; the exact model was unavailable or terminated before a report, and no downgrade was made. Both Gemini reports, the exact Kimi K3 report, and the Sol synthesis completed.
- [x] 1.3 Synthesize the completed exact-model reports plus independent source evidence, resolve the FastMCP/Kimi/Antigravity constraints, and run `openspec validate embed-cao-ops-streamable-http --strict`.

## 2. Async CAO Ops factory and compatibility

- [ ] 2.1 Add failing factory/backend tests in `test/ops_mcp_server/` using typed fixture factories, strict async transport fakes, and parametrized contract cases for shared async stdio calls, ASGITransport calls, explicit immediate/long-poll timeouts, cancellation, invalid JSON, HTTP/stdio schema equivalence, and rejection of `/mcp/ops`, absolute URLs, and non-REST paths; run them and confirm the expected pre-implementation failures.
- [ ] 2.2 Refactor `src/cli_agent_orchestrator/ops_mcp_server/server.py` into a `create_ops_mcp` factory backed by a typed asynchronous request protocol; add the smallest shared HTTPX and ASGI implementations plus embedded REST path allowlist required to pass 2.1 and remove `requests` from async handlers.
- [ ] 2.3 Preserve `cao-ops-mcp-server` stdio startup, local-bearer behavior, existing tool/resource names and response models; run `uv run pytest test/ops_mcp_server -q` with zero failures.
- [ ] 2.4 Refactor factory/backend modules only after green, then run Black, isort, and `uv run mypy src/` with zero errors for the touched boundary.

## 3. Embedded Streamable HTTP lifecycle and authentication

- [ ] 3.1 Add failing tests with reusable MCP client/auth fixture factories for exact `/mcp/ops` initialize without redirect following, retained-session principal binding, same-principal token refresh, different-principal replay, combined lifespan failure, embedded backend/session-manager shutdown, and existing `/health`, REST, TrustedHost, static Web UI, and WebSocket compatibility.
- [ ] 3.2 Add failing auth tests proving every GET/POST/DELETE is validated, current-request Authorization is forwarded, no initialization ContextVar or local token is reused, scope enforcement remains authoritative, and token/JWT/JWKS details are redacted.
- [ ] 3.3 Create `mcp.http_app(path="/ops", transport="http", stateless_http=False)`, mount it at `/mcp`, combine lifespans, install the validated ASGI principal, and bind the backend to current MCP request metadata until 3.1-3.2 pass.
- [ ] 3.4 Run a no-redirect raw loopback handshake plus same-session credential replay and refresh tests, the focused protocol/auth/host regression set, and verify no bearer or resolved URL appears in logs.

## 4. Subscription ownership and cancellation

- [ ] 4.1 Add failing deterministic async tests using events and cancel scopes rather than timing sleeps for duplicate subscribe, unsubscribe during long-poll, explicit DELETE, session-manager failure, lifespan shutdown, transient reconnect, worker auth failure, cursor non-advancement, and zero task/registry leaks.
- [ ] 4.2 Constrain FastMCP to `>=3.2.0,<3.3.0`; add a characterized `FastMcp32SessionTasks` adapter using the private session task-group/finalization seam with no detached fallback, daemon-global registry, or `id(session)` key.
- [ ] 4.3 Prove explicit termination cancels and awaits workers while transient request loss preserves the retained session; prove long-poll remains authoritative and notifications remain body-free, with durable ordering and caller-owned idempotent acknowledgement preserved; run all inbox, event-bus, API peer, and Ops MCP tests.
- [ ] 4.4 Run parametrized concurrency/load repetitions for parallel clients and cancellation races with explicit teardown and zero-live-task assertions, then refactor only while the leak and ordering tests remain green.

## 5. Profile model and provider-native HTTP translation

- [ ] 5.1 Add failing parametrized Pydantic contract matrices using `pytest.param` IDs, `TypeAdapter`, `tmp_path`, `monkeypatch`, and `caplog` to prove exclusive stdio/HTTP entries, legacy typed-command round trips, HTTP URL references surviving profile load/install, one process-environment launch snapshot, mixed/empty rejection, missing or malformed values, and secret-free errors.
- [ ] 5.2 Implement a functional Pydantic boundary with pure no-I/O normalization/validation, strict narrowed stdio/HTTP models, immutable resolved copies, legacy command round trips and non-URL interpolation, aligned schema/examples, and launch-only HTTP URL resolution.
- [ ] 5.3 Add provider tests for Codex `url` plus HTTP `tool_timeout_sec` without env_vars, Claude `type: http`, Antigravity `httpUrl`, Kimi 0.29 `{url}`, command-only terminal identity, unsupported-provider failure, and no empty subprocess fields.
- [ ] 5.4 Implement Kimi per-terminal `.kimi-code/mcp.json` without `--mcp-config`; implement Antigravity `plan`/`accept-edits` modes and the exact Codex/Claude/Antigravity HTTP mappings.
- [ ] 5.5 Add an installed-Kimi 0.29 config/parser smoke test and installed-Antigravity 1.1.7 mode probe; run all profile, schema, install, launch, provider, backend, and example-profile tests, plus `scripts/sync_skills.py --check` if canonical skills or their generated mirror changed.

## 6. Repository swarm, Serena, and ast-grep tooling

- [ ] 6.1 Add behavioral tests for project profile discovery, skill scoping, command-plus-HTTP translation, read-only design/test/review modes, and fail-closed missing `CAO_SERENA_MCP_URL`.
- [ ] 6.2 Commit flat `.cao/agents/` supervision, design, implementation, testing, and adversarial-review profiles plus project settings that register `agents.extra_dirs` and `skills.extra_dirs`; validate every requested model identifier before use.
- [ ] 6.3 Add `.serena/project.yml` with Python, generated/cache/worktree exclusions, and `read_only: true`; start the managed project Serena daemon through `artagon-scripts/scripts/mcp.sh`, export its direct URL, and prove symbol navigation from more than one read-only lane.
- [ ] 6.4 Add failing ast-grep fixtures for blocking requests in async handlers, detached subscription tasks, and empty HTTP commands; add `sgconfig.yml`, `rules/python/`, and `rule-tests/` until `sg test` passes and `sg scan --error` rejects each prohibited mutation.
- [ ] 6.5 Add `sg-test` and `sg-scan` Make targets and a CI step pinned to ast-grep 0.44.1; run both targets locally with zero findings.

## 7. artagon-scripts native-mode rollout

- [ ] 7.1 Create an isolated `artagon-scripts` feature branch/worktree and add failing Bats tests proving native emit uses exact `/mcp/ops` without redirect following, omits the proxy child, preserves proxy rollback and stale-config cleanup, probes direct readiness, and never owns `cao-server`.
- [ ] 7.2 Update `scripts/lib/mcp.sh`, `scripts/mcp.sh`, `scripts/sync-mcp.sh`, relevant config, status, and doctor paths so native mode emits `http://127.0.0.1:9889/mcp/ops`, while `MCP_CAO_OPS_TRANSPORT=proxy` restores the previous child only after sync.
- [ ] 7.3 Prove native start/stop cannot terminate active CAO sessions and direct CAO Ops works with no proxy child; run focused Bats, ShellCheck/format gates, config verification, and `git diff --check`.
- [ ] 7.4 Document deploy-CAO-first ordering, same-window client switch, handshake gate, resync requirement, and proxy rollback in both repositories.

## 8. Integration, swarm verification, and PR-ready evidence

- [ ] 8.1 Integrate subsystem commits in dependency order and run focused CAO verification: Ops MCP, API auth/host, subscription/inbox, profiles/providers, profile discovery, Serena config, and ast-grep.
- [ ] 8.2 Launch the prescribed read-only testing swarm and capture Sol protocol/pytest plus Gemini concurrency/load results; remediate every reproduced critical/high finding with a failing regression test first.
- [ ] 8.3 Launch read-only adversarial lanes for Sol, Claude Fable, two separately assigned Gemini security/concurrency reviews, and Kimi K3; require requirement-to-code/test traceability and repeat review until no critical/high findings remain.
- [ ] 8.4 Run final CAO gates: FastMCP private-seam characterization; raw no-redirect Streamable HTTP handshake; principal replay/refresh and lifecycle leak tests; Kimi config smoke; `sg test`; `sg scan --error`; Black; isort; zero-error mypy; Markdown-link validation after staging new docs; `git diff --check`; gitleaks; focused suites; and the complete CI-equivalent non-E2E Python suite.
- [ ] 8.5 Run final `artagon-scripts` native/proxy Bats, shell, config-sync, status/doctor, direct-handshake, and diff gates; confirm no CAO proxy child in native mode and no active session termination.
- [ ] 8.6 Run `openspec validate embed-cao-ops-streamable-http --strict`, obtain an independent implementation-verifier requirement/task/test/evidence verdict, and mark tasks complete only where fresh evidence exists.
- [ ] 8.7 Prepare separate scoped commits and PR-ready summaries for both branches that cross-link the OpenSpec change, companion PR, detailed swarm findings, verification evidence, deployment order, and rollback; do not push or create PRs without explicit publication authorization.

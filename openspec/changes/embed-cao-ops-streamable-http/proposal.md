## Why

CAO Ops is currently exposed only as a separately launched stdio MCP process that
makes synchronous HTTP calls back to `cao-server`. This prevents native remote MCP
configuration, duplicates process lifecycle, and can block asynchronous MCP handlers
while the authoritative FastAPI service is already available on the same loopback
boundary.

Embedding a stateful Streamable HTTP surface in `cao-server` gives every supported
provider one stable management endpoint, keeps REST authorization authoritative, and
creates a repository-owned multi-provider development environment for designing,
testing, and adversarially reviewing the transport and profile changes.

## What Changes

- Add a stateful FastMCP 3.2 Streamable HTTP endpoint at
  `http://127.0.0.1:9889/mcp/ops`, with that exact no-trailing-slash URL
  serving protocol requests directly without redirect.
- Refactor CAO Ops behind an injected asynchronous request backend so embedded calls
  use `httpx.ASGITransport` and standalone stdio calls use a shared HTTPX client.
- Preserve all existing CAO Ops tool names, resource URIs, request/response schemas,
  and the `cao-ops-mcp-server` stdio entrypoint.
- Protect every authenticated MCP GET, POST, and DELETE, bind each retained MCP
  session to the validated caller principal, forward the exact Authorization
  header from the current MCP request to authoritative REST dependencies, and
  require at least one CAO scope.
- Make subscription workers session-owned, cancellation-aware, awaited on
  unsubscribe/disconnect, and registry-clean; retain inbox long-poll as the
  authoritative delivery mechanism and body-free resource updates as supplemental
  notifications.
- Extend profile validation and provider translation so an MCP server is exactly one
  of command-launched stdio or native HTTP, including fail-closed exact environment
  reference resolution for HTTP URLs and a fresh terminal-identity snapshot at
  terminal launch.
- Add provider-native HTTP mappings for Codex, Claude, Antigravity, and Kimi
  0.29. Kimi uses the documented per-terminal cwd `.kimi-code/mcp.json`,
  ordinary HTTP entries are exactly `{url}`, and legacy `transport: sse`
  remains a non-goal. Antigravity 1.1.7 uses `url` rather than stale
  `httpUrl` and maps `acceptEdits` to `--mode accept-edits`, without synthetic
  subprocess fields.
- Add reusable repository-owned CAO swarm profiles across Codex, Claude,
  Antigravity, and Kimi for supervision, Python and shell implementation,
  protocol testing, source-backed research, and adversarial review. Profiles
  use current role-scoped Artagon skills, the command-launched
  `cao-mcp-server`, and native HTTP Context7, Tavily, Gemini Search,
  DuckDuckGo, and Serena endpoints. Claude profiles pin exact
  `claude-opus-5` at the highest supported provider-native Claude Code effort
  (`xhigh`, surfaced as `/effort ultracode`) and never substitute the separate
  SDK/tool/API `max` value for that CLI contract.
- Add a read-only Serena project definition and pinned ast-grep structural
  rules and CI gates for the new async and commandless HTTP invariants.
- Coordinate a separate `artagon-scripts` rollout that emits the native CAO Ops URL,
  removes CAO Ops from the generated proxy child set in native mode, preserves a
  temporary proxy rollback mode, and does not take ownership of `cao-server`.

Explicit non-goals:

- Do not convert the identity-bearing in-session `cao-mcp-server` from stdio.
- Do not add legacy SSE.
- Do not emit Kimi `transport: sse` for ordinary HTTP or Antigravity
  `httpUrl`.
- Do not run an independent CAO Ops HTTP daemon outside `cao-server`.
- Do not make MCP resource notifications authoritative; inbox long-poll remains the
  reliable source of messages.
- Do not widen the default loopback bind, change the full-PTY WebSocket trust
  boundary, add persistence migrations, or change tmux/Herdr terminal semantics.

## Capabilities

### New Capabilities

- `embedded-cao-ops-http`: Embedded, stateful CAO Ops Streamable HTTP transport,
  asynchronous request backend, FastAPI lifecycle integration, and stdio
  compatibility.
- `cao-ops-session-security`: Stateful authentication, authorization forwarding,
  subscription ownership, cancellation, cleanup, and authoritative long-poll
  behavior.
- `profile-http-mcp`: Exclusive command-or-HTTP profile validation, secure URL
  resolution, and provider-native MCP/mode translation.
- `cao-swarm-development-tooling`: Repository CAO profiles, Serena semantic
  navigation, ast-grep rules, tests, CI gates, and the coordinated
  `artagon-scripts` rollout contract.

### Modified Capabilities

None. This repository has no existing main OpenSpec capability specs; the new
capabilities preserve the existing public REST, stdio MCP, in-session MCP, CLI,
Web UI, WebSocket, persistence, and backend contracts.

## Impact

- Runtime: `src/cli_agent_orchestrator/ops_mcp_server/`,
  `src/cli_agent_orchestrator/api/main.py`, authentication middleware/dependencies,
  profile models/schema, terminal launch resolution, and Codex/Claude/
  Antigravity/Kimi provider translators.
- Public surfaces: one new `/mcp/ops` HTTP route; existing `/health`, REST, static Web
  UI, TrustedHost, CORS, WebSocket, CLI, `cao-mcp-server`, and
  `cao-ops-mcp-server` behavior remains compatible.
- Security and privacy: bearer values are forwarded only to the in-process REST
  boundary, are never replaced with the machine-local token for external callers,
  and must not appear in logs or validation errors. Authentication remains
  default-off; when enabled, initialization and all REST calls fail closed.
- Callback identity: every new terminal snapshots its created terminal ID and
  overwrites stale profile/session/provider identity for the command-launched
  `cao-mcp-server`; HTTP entries never receive terminal environment. A launch
  smoke must observe callback `sender_id` equal to the created terminal.
- Dependencies: FastMCP is constrained to `>=3.2.0,<3.3.0` because the
  session-owned subscription adapter characterizes one private 3.2 seam;
  ast-grep CI installs version 0.44.1. No database migration is required.
- Development and rollout: new `.cao/agents/`, `.serena/project.yml`,
  `sgconfig.yml`, `rules/python/`, `rule-tests/`, Make targets, CI checks,
  documentation, and cross-repository verification evidence. The CAO change deploys
  before `artagon-scripts` switches clients; `MCP_CAO_OPS_TRANSPORT=proxy` remains
  the temporary rollback.

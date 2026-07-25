## Context

`cao-server` owns the authoritative FastAPI routes, default-off OAuth scope
dependencies, process lifecycle, and Web UI mount
(`src/cli_agent_orchestrator/api/main.py`, `docs/api.md`,
`docs/control-planes.md`). `cao-ops-mcp-server` currently owns a module-global
FastMCP instance and calls those routes through a mixture of `requests` and
`httpx` (`src/cli_agent_orchestrator/ops_mcp_server/server.py`). The peer inbox
resource layer also creates detached subscription tasks and keys cleanup to a
process-global registry.

FastMCP 3.2 documents mounted integration through `http_app(path="/")` and
requires the mounted app's lifespan to run for stateful session management. Its
documented FastAPI pattern combines the host and MCP lifespans with
`fastmcp.utilities.lifespan.combine_lifespans`
([FastMCP 3.2 lifespan](https://github.com/prefecthq/fastmcp/blob/v3.2.0/docs/servers/lifespan.mdx),
[FastAPI integration](https://github.com/prefecthq/fastmcp/blob/v3.2.0/docs/integrations/fastapi.mdx)).

Agent profiles currently model `mcpServers` as unvalidated dictionaries and
provider implementations assume every server launches a command. The repository
already supports extra profile and skill directories through settings, so the
development topology can be repository-owned without adding a second discovery
mechanism.

This change spans the CAO runtime repository and the sibling `artagon-scripts`
MCP controller. It therefore uses separate branches and cross-linked PRs with a
deploy-CAO-first rollout.

Provider behavior was refreshed on 2026-07-25 before profile implementation.
The durable
[provider CLI source record](research/provider-cli-sources-2026-07-25.md)
contains the exact official Claude Code, Kimi Code CLI, and Antigravity
repository/document URLs, Context7 repository IDs, installed
`kimi 0.29.0` / `agy 1.1.7` probes, and the adjudicated Claude plan-mode smoke.
Google DevKnowledge was unavailable because its configured surface required
authentication; no design claim depends on it.

## Goals / Non-Goals

**Goals:**

- Serve stateful Streamable HTTP at `/mcp/ops` from the single-worker,
  loopback-bound `cao-server`.
- Preserve the standalone stdio entrypoint and all CAO Ops protocol contracts.
- Remove blocking HTTP calls from asynchronous MCP execution paths.
- Reuse authoritative FastAPI route dependencies, including their exact
  read/write/admin scope decisions.
- Make subscription workers owned by, cancelled with, and awaited by their
  FastMCP sessions.
- Represent and translate command and HTTP MCP entries without synthetic
  subprocess fields.
- Ship repeatable CAO design/implementation/test/review profiles, read-only
  Serena navigation, and structural ast-grep gates.
- Switch `artagon-scripts` to native CAO Ops HTTP without letting its controller
  start or stop `cao-server`.

**Non-Goals:**

- Converting the identity-bearing in-session `cao-mcp-server` from stdio.
- Adding legacy SSE or a second CAO Ops daemon.
- Treating resource notifications as message payloads or delivery authority.
- Changing database schema, tmux/Herdr lifecycle, terminal readiness/status,
  full-PTY WebSocket authentication, or the default loopback bind.
- Supporting arbitrary URL interpolation, userinfo URLs, redirects to
  untrusted origins, or silent provider/model downgrade.

## Decisions

### 1. Factory-owned FastMCP server with an injected asynchronous backend

`create_ops_mcp(backend)` will register the existing tools, resources, and
subscription hooks on a new FastMCP instance. Tool implementations will call an
`AsyncRequestBackend` protocol rather than module-level request helpers.

- The stdio process owns one shared `httpx.AsyncClient` configured with
  `API_BASE_URL` and the machine-local bearer policy.
- The embedded instance owns one shared `httpx.AsyncClient` using
  `httpx.ASGITransport(app=host_app)` and a per-request forwarded bearer policy.
- Both backends preserve current timeout behavior: immediate pulls explicitly
  use `timeout=None`, long-polls use `wait_seconds + 5`, and finite ordinary
  calls use the shared client defaults.
- HTTP status and invalid-JSON failures continue through one response mapper so
  existing tool result schemas and error strings remain stable.

The embedded backend binds to the host ASGI app after construction, avoiding an
import cycle while keeping all calls on the same event loop. Direct service
calls were rejected because the external MCP plane must continue exercising the
authoritative HTTP validation and authorization boundary. Per-tool ad hoc
clients were rejected because they lose connection reuse and complicate
shutdown.

### 2. Mount one canonical stateful endpoint and combine lifespans

The embedded factory creates
`mcp.http_app(path="/ops", transport="http", stateless_http=False)`. The host
constructs FastAPI with
`combine_lifespans(existing_cao_lifespan, ops_http_app.lifespan)`, binds the
ASGI backend to that app, and mounts the child at `/mcp` before the Web UI
catch-all. Consequently, an exact request to `/mcp/ops` reaches the MCP route
without a 307/308 redirect.

Only one Uvicorn worker is supported. Failure to start the child session manager
fails `cao-server` startup. Host lifespan shutdown closes the session manager,
subscription adapter, and both shared HTTPX backends.

### 3. Authenticate every request, bind the session principal, authorize at REST

When CAO auth is enabled, the MCP wrapper validates every GET, POST, and DELETE,
requires at least one CAO scope, and installs a validated principal derived from
the token issuer, subject, and client identity into the ASGI authentication
scope used by the Streamable HTTP session manager. A retained session accepts a
refreshed token for the same principal and rejects a different principal as an
unknown session before dispatch.

The embedded backend reads the exact Authorization header from the current MCP
request metadata. It MUST NOT use an outer middleware ContextVar copied when the
stateful session task is created, call `get_local_bearer()`, or substitute the
machine token. Subscription workers receive the subscribe request credential
explicitly, stop and remove themselves on 401/403, and require resubscription
after credential refresh.

Existing FastAPI dependencies decide whether an individual route requires read,
write, or admin and map their response through the normal CAO Ops error path.
The stdio backend retains its existing machine-local token behavior. Missing or
invalid credentials return sanitized 401 responses; a valid token with no CAO
scope returns 403. Token values, resolved URLs, environment values, JWT/JWKS
exception text, and validation inputs do not enter logs or responses.

### 4. Characterized FastMCP 3.2 session-owned subscription adapter

The dependency range is `fastmcp>=3.2.0,<3.3.0`. A narrow
`FastMcp32SessionTasks` compatibility adapter characterizes the installed
`MiddlewareServerSession._subscription_task_group` seam. It attaches one
resource-to-cancel-scope registry to the session object, starts workers through
the owning task group, and fails closed rather than falling back to
`asyncio.create_task` when the seam is unavailable.

Unsubscribe cancels and awaits the matching worker before removing its entry.
Explicit MCP DELETE, session-manager failure, and host lifespan shutdown cancel
and await every worker before session exit. A transient HTTP/SSE request
disconnect is not session termination: the retained session and its
subscriptions remain reconnectable with the same session ID and principal.

No daemon-global worker registry or integer `id(session)` key is used. Workers
retain the existing cursor-based long-poll and body-free `resources/updated`
notification. Resource notifications remain supplemental; `receive_messages`
long-poll and explicit `ack_messages` remain authoritative, ordered, and
idempotent.

### 5. Preserve HTTP URL references until terminal launch

`mcpServers` values become a strict Pydantic union:

- stdio: required `command`; optional `args`, `env`, and `timeout`; no `url`;
- HTTP: required `type: http` and `url`; no `command`, `args`, subprocess
  `env`, or command timeout.

Compatibility accepts the current omitted/`stdio` type for command entries.
Ambiguous, mixed, or empty entries fail profile validation. Legacy
interpolation continues for existing non-URL profile fields, but parsing and
loading preserve `mcpServers.*.url` verbatim. At terminal launch, CAO snapshots
the server process environment once, resolves either a literal HTTP(S) URL or
one exact `${ENV_NAME}` reference, validates the resolved value, and passes only
the typed resolved copy to the provider translator.

Missing variables and malformed values fail with an error naming only the MCP
server, variable, and violated rule. Non-HTTP schemes, relative URLs, control
characters, fragments, userinfo, and arbitrary interpolation fail closed. The
reference and resolved value are never included in logs, traces, validation
inputs, or exceptions.

The profile boundary uses a functional Pydantic design: pure normalization and
validation functions have no environment or filesystem I/O, typed command and
HTTP variants are narrowed before provider translation, and launch-time
resolution returns a new immutable resolved model rather than mutating the
loaded profile. Provider serializers consume only resolved typed models.

The same launch boundary snapshots the just-created terminal ID. It discards
any `CAO_TERMINAL_ID` inherited from a loaded profile, process environment,
provider cache, persisted provider configuration, or prior session. Only a
command-launched entry whose resolved command is the identity-bearing
`cao-mcp-server` receives
`env.CAO_TERMINAL_ID=<created-terminal-id>`. Other command entries retain only
their declared environment, and HTTP entries never receive an environment or
terminal identity. Sequential and concurrent launches build independent
copies; neither a profile object nor provider-global state is mutated.

Tests follow the repository Python testing guidance: parametrized contract
matrices and fixture factories cover providers and invalid inputs; async tests
use deterministic events/cancel scopes rather than timing sleeps; `tmp_path`,
`monkeypatch`, strict fakes, and `caplog` isolate filesystem, environment,
transport, and redaction behavior; concurrency races are repeated with explicit
zero-task cleanup assertions.

### 6. Exact provider-native translation and permission modes

- Codex emits `mcp_servers.<name>.url` for HTTP. Its client-side
  `tool_timeout_sec=600.0` remains valid for HTTP, but HTTP entries receive no
  command, args, env, or env_vars.
- Claude emits `{"type": "http", "url": "..."}` with no subprocess fields.
- Antigravity 1.1.7 writes `{"url": "..."}` to `mcp_config.json`, with no
  stale `httpUrl`, command, args, or env.
- Kimi 0.29 discovers MCP configuration from
  `$KIMI_CODE_HOME/mcp.json` (default `~/.kimi-code/mcp.json`),
  project-root `.mcp.json`, then `<cwd>/.kimi-code/mcp.json`, with later
  scopes overriding earlier scopes. CAO writes the per-terminal file only at
  `<unique-cwd>/.kimi-code/mcp.json` and does not mutate the user or project
  files. Ordinary HTTP entries are exactly `{"url": "..."}` with no `type`,
  `transport`, or subprocess fields; `transport: sse` is legacy SSE and is not
  emitted. Stdio entries retain command, args, and env. CAO does not pass
  `--mcp-config`.
- Kimi passes a profile model through `-m` / `--model`. Its interactive task
  path writes the task into the TUI and submits it with `Enter`;
  `-p` / `--prompt` is the bounded non-interactive one-prompt surface used by
  installed smoke probes, not a substitute for interactive submission.
- A fresh `CAO_TERMINAL_ID` is injected only into each resolved
  command-launched `cao-mcp-server` entry. All HTTP entries and unrelated
  command entries omit it.
- Antigravity maps `permissionMode: plan` to `--mode plan` and
  `permissionMode: acceptEdits` to `--mode accept-edits`; either explicit mode
  suppresses `--dangerously-skip-permissions`.

Each provider receives focused positive and negative tests. A launch smoke
creates a real terminal with command `cao-mcp-server`, sends a callback, and
requires callback `sender_id` to equal the terminal ID returned by creation.
The test seeds stale identity in profile, provider, process, persisted-config,
and prior-session inputs and proves none survives. Providers outside this
rollout continue to accept command entries; HTTP entries fail clearly if they
lack a supported native mapping rather than being coerced to stdio.

### 7. Repository-owned swarm and navigation profiles

Flat profiles under `.cao/agents/` are reusable `cli-agent-orchestrator`
repository roles, not profiles named for this change. Across applicable Codex,
Claude, Antigravity, and Kimi families they cover repository supervision,
Python implementation, Python/protocol testing, source-backed research,
read-only adversarial review, and shell implementation/testing. Each exact
provider/model identifier is validated against the installed provider before
commit; unavailable requested models fail closed without substitution or
downgrade. The highest-model targets are Codex `gpt-5.6-sol` at maximum
reasoning, exact Claude `claude-opus-5` at the highest supported
provider-native Claude Code effort (`xhigh`, surfaced as
`/effort ultracode`), the highest validated Gemini Pro High model for
Antigravity, and the highest validated Kimi K3 model. The separate
five-value SDK/tool/API effort ladder does not replace the launched Claude
Code CLI surface and MUST NOT cause a generic `max` value to be emitted or
required for that profile.

The official Claude Code changelog retrieved through Context7 on 2026-07-25
states that `/effort ultracode` is offered only on models supporting `xhigh`.
Under authoritative CAO root directive inbox 870, the existing bounded
plan-mode smoke on terminal `21a9e101` is therefore PASS without relaunch:
`canonicalModel=claude-opus-5`, live Claude Code effort
`xhigh`/ultracode, `permissionMode=plan`, and server-stamped callback
`sender_id=21a9e101` equal to the created terminal. This evidence adjudicates
the smoke only and does not mark an OpenSpec implementation task complete.

Project settings register `.cao/agents/` through `agents.extra_dirs` and the
current Artagon Python skill directory through `skills.extra_dirs`. Skills are
role-scoped: Python implementation uses the current `python-type-safety`,
`python-design-patterns`, `python-error-handling`,
`python-resource-management`, `async-python-patterns`,
`python-code-style`, `python-testing-patterns`, and
`python-anti-patterns` guidance as applicable; test roles emphasize current
Python/async testing; shell roles use current Artagon shell authoring,
defensive, testing, and linting skills. Supervisors receive only current CAO
orchestration protocols, and the final traceability lane alone receives
`implementation-verification`. Profiles MUST NOT include the stale
`review-verification-protocol`.

Supervisors receive `cao-supervisor-protocols`; workers receive
`cao-worker-protocols`. Every applicable profile includes the
identity-bearing stdio `cao-mcp-server` plus native HTTP entries with exact
launch-resolved references:

```yaml
mcpServers:
  cao-mcp-server:
    command: cao-mcp-server
  context7:
    type: http
    url: ${CAO_CONTEXT7_MCP_URL}
  tavily:
    type: http
    url: ${CAO_TAVILY_MCP_URL}
  gemini-search:
    type: http
    url: ${CAO_GEMINI_SEARCH_MCP_URL}
  duckduckgo:
    type: http
    url: ${CAO_DUCKDUCKGO_MCP_URL}
  serena:
    type: http
    url: ${CAO_SERENA_MCP_URL}
```

These entries use no `npx`, embedded credential, subprocess field, or terminal
environment. Missing required URL references fail before provider start.
Profile prompts direct library/API/CLI questions to Context7, current claims to
Tavily plus Gemini Search for independent corroboration, general/fallback
search to DuckDuckGo, and symbol/reference navigation to Serena. They require
Serena and `sg` structural queries before broad text search.

Design/test/review profiles omit native write tools; Kimi uses native plan mode
and Antigravity uses `permissionMode: plan`. Implementation profiles receive
write/execute capabilities only in their assigned worktrees. Repository-local
Claude implementation profiles remain orchestration-capable: they launch
directly in the assigned worktree, use native `permissionMode: acceptEdits`,
establish project trust without a first-command prompt, and retain
`cao-mcp-server` callbacks. A launch smoke proves the requested cwd, callback
availability, first authorized command, and terminal-matching callback sender.

`.serena/project.yml` enables Python, excludes generated/cache/worktree
directories, and sets `read_only: true`. The shared warm Serena daemon is
navigation-only; edits remain native filesystem operations. Prompts require
Serena symbol navigation and `sg` structural queries before broad text search.

### 8. Structural gates as executable invariants

`sgconfig.yml` registers Python rules and rule tests. The initial rules reject:

- `requests` calls in async CAO MCP handlers;
- detached subscription task creation outside the session-owned abstraction;
- empty `command`/`args` reintroduced into HTTP MCP translations.

Every rule has positive and negative fixtures. `Makefile` exposes `sg-test` and
`sg-scan`; CI installs ast-grep 0.44.1 and runs both. These supplement, not
replace, pytest, Black, isort, mypy, link, secret, and diff gates.

### 9. Cross-repository native-mode rollout

`artagon-scripts` emits `cao-ops` as
`http://127.0.0.1:9889/mcp/ops` in native mode and omits CAO Ops from the
mcp-proxy child configuration. Its `start`, `stop`, `status`, and `doctor`
commands probe but never own `cao-server`. A temporary
`MCP_CAO_OPS_TRANSPORT=proxy` mode preserves the previous generated child;
changing the mode requires rerunning config sync.

CAO deploys first. A live initialize/list/call handshake gates the client
switch. Rollback restores proxy mode and syncs configs; it does not terminate
active CAO sessions.

## Risks / Trade-offs

- **Stateful FastMCP internals can drift within the broad dependency range** →
  pin compatibility tests to 3.2.0 semantics and avoid undocumented session
  internals where a public context/task-group hook exists.
- **ASGITransport can accidentally recurse into `/mcp/ops`** → backend accepts
  only repository-owned REST paths and tests reject MCP-path dispatch.
- **Header context can leak across parallel clients** → use `ContextVar` tokens
  with `finally` reset and parallel isolation tests using different credentials.
- **Disconnect cleanup differs from explicit unsubscribe** → exercise transport
  disconnect, cancellation, reconnect, and zero-live-task assertions.
- **A read-only token can initialize but later call a mutation tool** → keep REST
  dependencies authoritative and test the mapped 403 without capability
  escalation.
- **Profile union strictness can reject previously tolerated malformed entries**
  → preserve valid command shapes, add actionable field-level errors, update the
  JSON schema/examples, and document the validation boundary.
- **Shared Serena can create concurrent edit conflicts** → configure read-only
  and restrict it to navigation.
- **Native/proxy config drift can leave stale generated entries** → sync removes
  the inactive shape, doctor checks the selected mode, and Bats covers both
  transitions.
- **Persisted provider config can reuse another terminal's callback identity** →
  overwrite all stale identity sources from the created-terminal snapshot,
  keep terminal env off HTTP entries, and require a sender-ID launch smoke
  across sequential terminal creation.
- **A stacked CAO branch depends on peer-inbox landing** → keep commits scoped
  and document the base dependency in the PR; rebase onto main after the
  prerequisite merges.

## Migration Plan

1. Land and deploy the CAO branch while existing clients continue using stdio or
   proxied CAO Ops.
2. Start `cao-server` and prove `/health`, REST, Web UI, TrustedHost, and a real
   stateful `/mcp/ops` initialize/list/call sequence.
3. Land the `artagon-scripts` branch with native mode available but retain proxy
   rollback.
4. In the same rollout window, set native mode, rerun config sync, and verify all
   provider configurations and a direct handshake without a CAO proxy child.
5. If any native client fails, set `MCP_CAO_OPS_TRANSPORT=proxy`, rerun config
   sync, and leave `cao-server` plus active sessions untouched.

No database migration or persisted session conversion is required.

## Resolved compatibility constraints

- FastMCP 3.2 exposes no documented public session cleanup hook. The change
  therefore pins `<3.3`, isolates the private task-group/session-finalization
  seam behind one compatibility adapter, and requires a loud characterization
  test.
- Kimi 0.29 native HTTP uses `{"url": "..."}` in project-local MCP JSON and
  does not support CAO's historical `--mcp-config` launch flag. Its documented
  discovery locations are `$KIMI_CODE_HOME/mcp.json` (default
  `~/.kimi-code/mcp.json`), project `.mcp.json`, and cwd
  `.kimi-code/mcp.json`; `transport: sse` is legacy SSE, not ordinary HTTP.
- Kimi 0.29 uses `-m` / `--model`, `-p` / `--prompt`, and `Enter` for
  interactive TUI submission.
- Antigravity 1.1.7 uses `url` in `mcp_config.json`, not `httpUrl`, and accepts
  only `--mode plan` and `--mode accept-edits` for the explicit modes in scope.

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
`BaseSession._task_group` and `BaseSession._exit_stack` seams. It attaches one
resource-to-cancel-scope registry to the session object, starts workers through
the owning task group, registers deterministic finalization on the session exit
stack, and fails closed rather than falling back to `asyncio.create_task` when
either seam is unavailable. FastMCP's outer `_subscription_task_group` is not
used for an unbounded inbox poll because normal task-group exit would wait for
that child before `BaseSession.__aexit__` could cancel it.

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
- Antigravity CLI emits the documented canonical
  `{"serverUrl": "..."}` form with no command, args, or env. Installed Agy
  1.1.7 also accepts `url` as a compatibility alias, but CAO does not depend
  on that alias. The Gemini CLI-only `httpUrl` field is not emitted.
- Kimi 0.29 writes a per-terminal `.kimi-code/mcp.json` beneath its unique
  temporary working directory. HTTP entries are `{"url": "..."}` with no
  `type`, `transport`, or subprocess fields; stdio entries retain command,
  args, and env. CAO does not pass `--mcp-config`.
- `CAO_TERMINAL_ID` is injected only into command-launched entries.
- Antigravity maps `permissionMode: plan` to `--mode plan` and
  `permissionMode: acceptEdits` to `--mode accept-edits`; either explicit mode
  suppresses `--dangerously-skip-permissions`.

When auth is enabled, the exact loopback `/mcp/ops` mapping fails closed
without `CAO_AUTH_LOCAL_TOKEN`. Codex uses `bearer_token_env_var`, Kimi uses
`bearerTokenEnvVar`, and Claude uses its documented `${VAR}` header expansion,
so no token value enters their command line or generated JSON. Antigravity
documents literal custom headers but not header environment expansion; CAO
therefore places the token only in its generated shared MCP config and forces
that file to mode 0600 after every write. No mapping attaches the local token
to an external URL or to another loopback path. This provider-side local
client credential is distinct from the embedded backend rule: an external
HTTP caller's Authorization header is forwarded unchanged through
ASGITransport and is never replaced by the machine token.

Each provider receives focused positive and negative tests. Providers outside
this rollout continue to accept command entries; HTTP entries fail clearly if
they lack a supported native mapping rather than being coerced to stdio.

### 7. Repository-owned swarm and navigation profiles

Flat profiles under `.cao/agents/` cover supervision, design, implementation,
testing, and adversarial review. CAO discovers the nearest repository-owned
`.cao/agents/` directory automatically up to the current Git worktree root, so
a fresh clone needs no user-global profile copy or committed absolute path.
The Artagon Python skill path remains operator-configurable through portable
`skills.extra_dirs`.

Supervisors receive `cao-supervisor-protocols`; workers receive
`cao-worker-protocols`. Profiles include the identity-bearing stdio
`cao-mcp-server`, managed Context7 HTTP, and the exact
`${CAO_SERENA_MCP_URL}` HTTP entry. Design/test/review profiles omit native
write tools; Kimi uses native plan mode and Antigravity uses
`permissionMode: plan`. Implementation profiles receive write/execute
capabilities only in their assigned worktrees.

Supervisor profiles additionally include the embedded `cao-ops` native HTTP
endpoint. This keeps them in Codex plan/read-only mode while providing
`get_terminal_status`, output, input, session inspection, and shutdown tools
through the trusted MCP control plane; direct localhost REST and Herdr socket
access can be denied by the provider sandbox and is not a lifecycle-management
dependency.

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
  does not support CAO's historical `--mcp-config` launch flag.
- Antigravity 1.1.7 uses `--mode plan` and `--mode accept-edits`.

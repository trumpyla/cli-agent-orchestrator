## ADDED Requirements

### Requirement: Embedded stateful Streamable HTTP endpoint
`cao-server` MUST mount one FastMCP 3.2 stateful Streamable HTTP application at
`/mcp/ops`, MUST run its lifespan with the existing FastAPI lifespan, and MUST
mount it before the Web UI catch-all route.

#### Scenario: Stateful initialization and calls
- **WHEN** a client initializes at `/mcp/ops` and performs list and call requests with the returned MCP session ID
- **THEN** the server accepts the retained session ID and returns the existing CAO Ops tools and results

#### Scenario: Canonical endpoint without redirect
- **WHEN** a client sends initialize directly to `/mcp/ops` without enabling redirect following
- **THEN** the route returns the MCP protocol response directly and does not return 307 or 308

#### Scenario: Missing MCP lifespan
- **WHEN** the mounted FastMCP session manager cannot start during application lifespan
- **THEN** `cao-server` startup fails rather than serving a degraded endpoint

#### Scenario: Existing host surfaces
- **WHEN** the embedded MCP endpoint is enabled
- **THEN** `/health`, REST routes, TrustedHost behavior, the static Web UI, and the WebSocket route retain their existing behavior

### Requirement: Injected asynchronous request backend
All CAO Ops async tools and resources MUST call an injected asynchronous request
backend and MUST NOT execute blocking `requests` calls on the event loop.

#### Scenario: Embedded REST call
- **WHEN** an embedded MCP tool calls a CAO operation
- **THEN** the backend uses `httpx.ASGITransport` to invoke the authoritative FastAPI route in process

#### Scenario: Embedded backend path allowlist
- **WHEN** the embedded backend is asked to dispatch `/mcp/ops`, an absolute URL, or a non-REST path
- **THEN** it rejects the call before entering ASGITransport

#### Scenario: Standalone stdio REST call
- **WHEN** the `cao-ops-mcp-server` entrypoint calls a CAO operation
- **THEN** it uses a shared HTTPX client against the configured CAO API URL

#### Scenario: Client cancellation
- **WHEN** an MCP request is cancelled during an in-flight HTTP or long-poll operation
- **THEN** cancellation propagates to HTTPX and no blocking worker thread or detached request remains

#### Scenario: Timeout preservation
- **WHEN** an immediate inbox read or long-poll is performed
- **THEN** the backend uses an explicit no-timeout immediate read or `wait_seconds + 5` long-poll timeout respectively

### Requirement: CAO Ops protocol compatibility
The embedded and stdio surfaces MUST expose the same existing tool names,
resource URIs, schemas, return values, and error mapping.

#### Scenario: Existing stdio client
- **WHEN** an existing client launches `cao-ops-mcp-server` over stdio
- **THEN** its initialize, list, resource, and tool-call contracts remain compatible

#### Scenario: HTTP and stdio equivalence
- **WHEN** the same valid CAO Ops operation is called over HTTP and stdio against the same server state
- **THEN** both transports return schema-equivalent results

#### Scenario: Invalid JSON from REST
- **WHEN** the authoritative REST route returns a success status with an invalid JSON body
- **THEN** both transports return the existing sanitized invalid-response error shape

### Requirement: Single embedded process boundary
CAO Ops HTTP MUST run only inside `cao-server` and MUST NOT introduce a legacy
SSE endpoint or independent CAO Ops HTTP daemon.

#### Scenario: Server lifecycle
- **WHEN** `cao-server` lifespan stops or the MCP session manager fails
- **THEN** the mounted endpoint, retained sessions, subscription workers, and shared embedded backend are closed and awaited

#### Scenario: Standalone entrypoint
- **WHEN** `cao-ops-mcp-server` is launched
- **THEN** it remains a stdio process and does not bind an HTTP listener

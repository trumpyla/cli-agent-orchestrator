## ADDED Requirements

### Requirement: MCP transport authentication
When CAO authentication is enabled, `/mcp/ops` MUST reject missing or invalid
bearer credentials before dispatching every GET, POST, and DELETE and MUST
require at least one CAO scope. Each retained session MUST be bound to its
validated caller principal. When authentication is disabled, initialization
MUST remain backward-compatible.

#### Scenario: Authentication disabled
- **WHEN** CAO authentication is not configured and a client initializes without a bearer header
- **THEN** initialization succeeds under the existing default-off scope behavior

#### Scenario: Missing bearer
- **WHEN** authentication is enabled and initialization has no bearer header
- **THEN** the MCP transport returns 401 without creating a session

#### Scenario: Invalid bearer
- **WHEN** authentication is enabled and initialization carries an invalid or expired bearer
- **THEN** the MCP transport returns 401 without logging the credential or creating a session

#### Scenario: No CAO scope
- **WHEN** a valid bearer grants none of `cao:read`, `cao:write`, or `cao:admin`
- **THEN** the MCP transport returns 403 without creating a session

#### Scenario: Every retained-session request is authenticated
- **WHEN** a retained session receives GET, POST, or DELETE
- **THEN** the request bearer is revalidated before session dispatch

#### Scenario: Different-principal session replay
- **WHEN** a valid bearer for a different principal reuses another caller's MCP session ID
- **THEN** the transport rejects it as an unknown session without invoking a tool or REST route

#### Scenario: Same-principal token refresh
- **WHEN** the same validated principal uses a refreshed bearer on a retained session
- **THEN** the request is accepted and the exact refreshed Authorization header is forwarded

### Requirement: Caller credential forwarding
The embedded backend MUST forward the external caller's exact Authorization
header through ASGITransport and MUST NOT substitute `CAO_AUTH_LOCAL_TOKEN`.

#### Scenario: Current-request caller isolation
- **WHEN** concurrent or sequential MCP requests carry different Authorization headers
- **THEN** each authoritative REST request receives only the exact header from its current MCP request and never a header copied at session creation

#### Scenario: Read-only tool
- **WHEN** a `cao:read` caller invokes a read-scoped tool
- **THEN** the existing REST dependency authorizes and returns the operation

#### Scenario: Read-only mutation
- **WHEN** a `cao:read` caller invokes a write- or admin-scoped tool
- **THEN** the existing REST dependency rejects it and the MCP tool returns the sanitized authorization failure

#### Scenario: Write and admin scopes
- **WHEN** a caller with the required write or admin scope invokes the corresponding operation
- **THEN** the existing REST dependency authorizes it without any MCP-layer scope escalation

### Requirement: Session-owned subscription workers
Every peer-resource subscription worker MUST be owned by its FastMCP session
task group and MUST be cancelled, awaited, and removed on unsubscribe or
session disconnect.

#### Scenario: Subscribe
- **WHEN** a session subscribes to a valid `cao://peers/<id>/inbox` resource
- **THEN** exactly one worker starts for that session and resource

#### Scenario: Duplicate subscribe
- **WHEN** the same session subscribes to the same resource again
- **THEN** the operation is idempotent and no second worker starts

#### Scenario: Unsubscribe
- **WHEN** a session unsubscribes from a peer resource during a long-poll
- **THEN** the worker is cancelled and awaited before its registry entry is removed

#### Scenario: Explicit session termination
- **WHEN** a client sends MCP DELETE, the session manager terminates the session, or server lifespan stops
- **THEN** all workers owned by that session are cancelled and awaited before their registry entries are removed

#### Scenario: Worker failure and recovery
- **WHEN** a subscription worker fails
- **THEN** the failure is observed without inbox content in logs, the entry is removed, and a later subscribe can start a replacement

#### Scenario: Transient transport loss
- **WHEN** one HTTP or SSE request disconnects without terminating the retained MCP session
- **THEN** the session and its workers remain available for reconnect with the same session ID and principal

#### Scenario: New session after termination
- **WHEN** a client reconnects after its prior MCP session was terminated
- **THEN** no old worker is reused and a fresh subscription can be established

#### Scenario: Subscription credential expires
- **WHEN** a subscription long-poll receives 401 or 403
- **THEN** the worker stops, removes its session registry entry, logs no credential or inbox content, and requires a new authenticated subscribe

### Requirement: Authoritative inbox delivery
Inbox long-poll and durable rows MUST remain authoritative. Resource
notifications MUST be body-free supplemental wakeups and MUST NOT acknowledge
or advance durable messages on behalf of the caller.

#### Scenario: New durable message
- **WHEN** a subscribed peer receives a message with an ID greater than the worker cursor
- **THEN** the worker emits a body-free resource-updated notification and the message remains available to `receive_messages`

#### Scenario: Notification dropped
- **WHEN** an MCP client drops or ignores a resource notification
- **THEN** `receive_messages` still returns the durable message in cursor order

#### Scenario: Explicit acknowledgement
- **WHEN** a caller processes messages and calls `ack_messages`
- **THEN** acknowledgement remains receiver-scoped and idempotent under the existing inbox contract

#### Scenario: Cancellation before result
- **WHEN** a worker is cancelled before a long-poll returns
- **THEN** it does not advance its cursor, acknowledge a message, or emit a stale notification

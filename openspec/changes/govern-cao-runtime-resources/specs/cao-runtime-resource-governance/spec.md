## ADDED Requirements

### Requirement: Validated and atomically persisted cleanup configuration
CAO MUST expose a typed top-level `cleanup` configuration, MUST disable
completed-session cleanup by default, and MUST validate a merged update before
atomically replacing the settings file.

#### Scenario: Default configuration
- **WHEN** no cleanup keys exist in environment or `settings.json`
- **THEN** cleanup is disabled, grace is 900 seconds, interval is 300 seconds, maximum deletion batch is 5, and preserve patterns are empty

#### Scenario: Valid configured policy
- **WHEN** the operator writes valid cleanup values through `cao config set`
- **THEN** `cao config get`, `cao config list`, and runtime reads return the same typed values

#### Scenario: Strict scalar validation
- **WHEN** enablement is not a strict boolean, grace is below 60 seconds, interval is below 30 seconds, or batch size is outside 1 through 50
- **THEN** CAO rejects the update and preserves the prior settings file byte-for-byte

#### Scenario: Preserve-pattern validation
- **WHEN** patterns exceed 100 entries, an entry exceeds 128 characters, or an entry contains a control character
- **THEN** CAO rejects the whole merged update without persisting any part

#### Scenario: Atomic write failure
- **WHEN** temporary-file write, flush, fsync, or replacement fails
- **THEN** the prior `settings.json` remains readable and unchanged

#### Scenario: Runtime disable
- **WHEN** a running server observes `cleanup.completed_sessions_enabled` change to false
- **THEN** it performs no later candidate deletion without a restart

### Requirement: Authoritative conservative Herdr eligibility
CAO MUST automatically select a workspace only when the backend is Herdr, the
strict native workspace inventory has exact status `done`, the workspace maps to
persisted non-peer CAO terminals, and every safety gate passes.

#### Scenario: Eligible completed workspace
- **GIVEN** a unique CAO Herdr workspace has native status `done`, a stable workspace ID, persisted terminal activity older than grace, no pending receiver inbox work, and no preserve-pattern match
- **WHEN** an enabled sweep runs
- **THEN** CAO selects it for bounded revalidation

#### Scenario: Nonterminal or malformed native state
- **WHEN** status is `blocked`, `idle`, `unknown`, `working`, missing, unrecognized, or belongs to a malformed workspace element
- **THEN** CAO preserves the workspace and records a categorical reason

#### Scenario: Tmux backend
- **WHEN** the selected backend is tmux
- **THEN** automatic completed-session cleanup performs no deletion and infers no state from terminal text

#### Scenario: CAO ownership boundary
- **WHEN** a workspace label lacks `SESSION_PREFIX`, is `__peers__`, contains only peer rows, or has no persisted terminal rows
- **THEN** CAO preserves it

#### Scenario: Timestamp boundary
- **WHEN** the newest persisted activity is not older than the local-naive cutoff, is missing, is timezone-aware, is in the future, or cannot be compared
- **THEN** CAO preserves the workspace

#### Scenario: Pending delivery
- **WHEN** any terminal in an otherwise eligible workspace has pending receiver inbox work
- **THEN** CAO preserves the workspace until a later sweep

#### Scenario: Preserve pattern
- **WHEN** the workspace label matches a configured `fnmatch` preserve pattern
- **THEN** CAO preserves it

#### Scenario: Ambiguous identity
- **WHEN** inventory has duplicate labels or one label cannot map to exactly one workspace ID and persisted CAO session
- **THEN** CAO fails closed for that identity

#### Scenario: Deterministic batch
- **WHEN** eligible workspaces exceed `max_sessions_per_sweep`
- **THEN** CAO orders by oldest activity then label and processes no more than the configured batch

### Requirement: Stable-identity revalidation and teardown
Automatic cleanup MUST capture the Herdr `workspace_id`, MUST revalidate the same
label-to-ID mapping immediately before teardown, and MUST close that backend
identity before deleting its persistence mapping.

#### Scenario: State or inbox changes
- **WHEN** an initially eligible workspace is no longer `done` or gains pending inbox work before teardown
- **THEN** immediate revalidation preserves it

#### Scenario: Label reuse
- **WHEN** the original workspace disappears and a new workspace reuses its label with a different `workspace_id`
- **THEN** cleanup performs no backend or database deletion for the new workspace

#### Scenario: Backend close failure
- **WHEN** the captured workspace ID cannot be closed
- **THEN** cleanup preserves terminal rows so a later sweep can retry safely

#### Scenario: Backend already absent
- **WHEN** the captured identity is authoritatively absent but persisted terminals remain
- **THEN** the session service removes persisted provider, environment, plugin, terminal, and receiver-inbox state without claiming a live backend close

#### Scenario: Crash after backend close
- **WHEN** a process stops after the workspace closes but before persistence deletion completes
- **THEN** the next strict startup reconciliation resumes deletion idempotently

### Requirement: Lifespan-owned nonblocking and serialized cleanup
The cleanup daemon MUST be owned by `cao-server` lifespan, MUST execute blocking
work off-loop, MUST prevent overlap across tasks and processes, and MUST await a
bounded in-flight worker before plugin teardown.

#### Scenario: Request-loop responsiveness
- **WHEN** provider or backend teardown is slow
- **THEN** `/health`, REST, WebSocket, and MCP handling remain available on the event loop

#### Scenario: In-process overlap
- **WHEN** one sweep is running when another interval or wakeup occurs
- **THEN** CAO starts no second worker

#### Scenario: Inter-process overlap
- **WHEN** another CAO process holds the cleanup file lock
- **THEN** the current process records `lock_held` and skips the sweep without deletion

#### Scenario: Shutdown during sleep
- **WHEN** lifespan stops while the daemon sleeps
- **THEN** the daemon cancels promptly and is awaited before plugin teardown

#### Scenario: Shutdown during deletion
- **WHEN** lifespan stops while the stored worker task is deleting
- **THEN** cancellation does not cancel or abandon the worker and lifespan awaits its completion before registry teardown

#### Scenario: Wall-clock anomaly
- **WHEN** wall-clock and monotonic deltas diverge beyond the supported tolerance
- **THEN** CAO skips that sweep and records `clock_skew`

#### Scenario: Per-session failure
- **WHEN** one candidate fails teardown
- **THEN** CAO records a categorical failure, continues the bounded batch, and retries no sooner than a later interval

### Requirement: Session-service-owned automatic deletion
The new automatic cleanup path MUST invoke the session service for provider,
backend, environment, plugin, and database teardown and MUST NOT claim that
unmodified legacy deletion call sites use the same path.

#### Scenario: Automatic deletion
- **WHEN** cleanup deletes an eligible workspace
- **THEN** the session service snapshots terminals, closes the captured backend ID, releases provider/runtime state, clears forwarded environment state, dispatches plugin events, and deletes persisted rows

#### Scenario: Legacy call-site compatibility
- **WHEN** an existing manual, flow, handoff, or reconciliation deletion path runs
- **THEN** its public return and error behavior remains compatible unless an explicit task in this change migrates it

### Requirement: Transactional receiver-inbox cascade
Deleting a terminal MUST atomically delete its terminal row and every inbox row
addressed to it, while preserving messages addressed to live receivers whose
sender disappeared.

#### Scenario: Terminal receiver deletion
- **WHEN** a terminal has pending or delivered receiver rows
- **THEN** receiver rows and the terminal row commit in one transaction

#### Scenario: Deleted sender with live receiver
- **WHEN** a sender terminal is deleted while its receiver remains
- **THEN** CAO preserves the receiver's message

#### Scenario: Multi-terminal session deletion
- **WHEN** a session has multiple terminal receivers
- **THEN** one transactional primitive cascades rows for every deleted receiver

#### Scenario: Concurrent message creation
- **WHEN** receiver message creation races terminal deletion
- **THEN** `BEGIN IMMEDIATE` serialization makes the message commit before and be cascaded or makes creation observe the missing receiver, leaving no orphan

#### Scenario: Database lock timeout
- **WHEN** SQLite cannot acquire the write lock within the configured bounded timeout
- **THEN** the operation rolls back without partial terminal or inbox deletion and reports `database_locked`

### Requirement: Complete strict Herdr startup reconciliation
Startup reconciliation MUST remove non-peer terminal rows whose whole workspace
or individual tab is absent from authoritative Herdr inventory, MUST distinguish
a valid empty inventory from malformed input, and MUST run off-loop.

#### Scenario: Missing workspace
- **WHEN** a persisted non-peer CAO session label is absent from a valid live workspace list
- **THEN** CAO removes all of its non-peer terminal rows and receiver inbox rows

#### Scenario: Valid empty workspace inventory
- **WHEN** Herdr returns a correctly typed `workspace_list` envelope with an empty `workspaces` list
- **THEN** reconciliation treats the empty set as authoritative and removes confirmed persisted ghosts

#### Scenario: Missing tab in live workspace
- **WHEN** a persisted terminal tab is absent from its live workspace
- **THEN** CAO removes that terminal and its receiver inbox rows

#### Scenario: Pane-less peer
- **WHEN** reconciliation encounters provider `peer` or session `__peers__`
- **THEN** it never treats that row as a missing Herdr pane or workspace

#### Scenario: Incomplete or malformed inventory
- **WHEN** a command fails or times out, JSON is malformed, a required envelope key is absent, a required value has the wrong type, an element is malformed, or labels are duplicated
- **THEN** reconciliation deletes no terminal or inbox row

#### Scenario: Event-loop responsiveness during reconciliation
- **WHEN** Herdr inventory commands or database iteration are slow at startup
- **THEN** they execute in a worker thread and do not block unrelated event-loop work

#### Scenario: Reconciliation summary
- **WHEN** reconciliation completes or skips
- **THEN** CAO emits info-level counts for missing workspaces, missing tabs, deleted terminals, and failures using reason codes without identifiers or content

### Requirement: Symmetric and atomic memory configuration
Every documented memory setting returned by `cao config get/list` MUST be
writable through `cao config set` with shared validation, existing precedence,
and atomic persistence.

#### Scenario: Exact compilation modes
- **WHEN** the operator sets `memory.compile_mode` to exact `append` or exact `llm`
- **THEN** the value persists and selects the corresponding existing compiler

#### Scenario: Invalid compilation mode
- **WHEN** mode is any other value or casing
- **THEN** CAO rejects it and preserves prior file bytes

#### Scenario: Compilation timeout
- **WHEN** timeout is a finite numeric value greater than zero and no greater than 3600
- **THEN** CAO persists and returns that float

#### Scenario: Invalid compilation timeout
- **WHEN** timeout is nonnumeric, nonfinite, nonpositive, or above 3600
- **THEN** CAO rejects it and preserves prior file bytes

#### Scenario: Existing memory settings
- **WHEN** the operator writes `memory.enabled` or `memory.flush_threshold`
- **THEN** existing validation, environment precedence, and behavior remain compatible

### Requirement: Portable and safe extra-directory resolution
CAO MUST canonicalize configured extra agent and skill directories at use time,
MUST preserve stored spelling and resolution order, and MUST fail closed for
sensitive roots outside CAO-owned state.

#### Scenario: Tilde-based agent directory
- **WHEN** `agents.extra_dirs` contains a valid nonsensitive `~/...` directory
- **THEN** discovery and source loading scan its canonical path

#### Scenario: Tilde-based skill directory
- **WHEN** `skills.extra_dirs` contains a valid nonsensitive `~/...` directory
- **THEN** skill listing and loading scan its canonical path

#### Scenario: Config round trip
- **WHEN** an operator reads settings after storing a tilde path
- **THEN** CAO returns the original spelling

#### Scenario: Existing absolute path
- **WHEN** an extra directory is absolute
- **THEN** discovery order, disabled matching, and first-valid-match behavior remain compatible

#### Scenario: Upgrade-time activation
- **WHEN** an older settings file contains a tilde entry that previously failed to resolve
- **THEN** CAO documents and tests that the entry becomes active after upgrade

#### Scenario: Sensitive extra directory
- **WHEN** an extra directory resolves inside `.ssh`, `.gnupg`, or `.aws` outside `CAO_HOME_DIR`
- **THEN** CAO rejects it without logging the resolved path

#### Scenario: CAO-owned default directory
- **WHEN** a built-in profile or context directory resides below `CAO_HOME_DIR`
- **THEN** sensitive-root protection does not reject that CAO-owned default

### Requirement: Privacy-preserving cleanup observability
Cleanup, reconciliation, and existing memory retention MUST emit bounded
operational evidence without logging user-authored keys, inbox bodies, terminal
output, credentials, resolved paths, secrets, or provider environment contents.

#### Scenario: Sweep summary
- **WHEN** a cleanup sweep completes
- **THEN** CAO logs candidate, deleted, preserved, and error counts with stable reason codes

#### Scenario: Reconciliation skip
- **WHEN** reconciliation fails closed
- **THEN** an info-level summary reports a reason count without identifiers or content

#### Scenario: Memory expiration
- **WHEN** memory retention expires or fails to expire an entry
- **THEN** logs omit the memory key and report only aggregate counts, scope, type, and categorical failure

#### Scenario: Sensitive terminal data
- **WHEN** a deleted terminal has inbox content, output, authorization headers, paths, or secret environment values
- **THEN** none appears in cleanup logs, metrics, exceptions, or API responses

### Requirement: Nonblocking and recognizable Kimi initialization
Kimi initialization MUST keep its startup-dialog polling off the event loop and
MUST recognize a usable ready prompt without requiring an upgrade dialog.

#### Scenario: Slow dialog handler
- **WHEN** Kimi startup history polling lasts for the configured outer timeout
- **THEN** an event-loop heartbeat and `/health` request continue while polling runs in a worker thread

#### Scenario: No upgrade dialog
- **WHEN** Kimi displays a recognized usable prompt without displaying an upgrade reminder
- **THEN** the dialog handler returns promptly and readiness validation proceeds

#### Scenario: Upgrade dialog
- **WHEN** the supported upgrade reminder appears
- **THEN** CAO dismisses it once and continues to a recognized ready prompt

#### Scenario: Unknown startup output
- **WHEN** neither a supported dialog nor ready prompt appears
- **THEN** initialization times out within the configured budget and reports a sanitized failure

#### Scenario: Exact provider semantics
- **WHEN** ready-state detection succeeds
- **THEN** the existing `wait_until_status` gate still confirms `IDLE` or `COMPLETED` before initialization is marked complete

### Requirement: Compatible rollout and future-sweep rollback
The capability MUST preserve public behavior while disabled and MUST let an
operator stop future automatic deletion without claiming restoration of prior
deletions.

#### Scenario: Disabled rollout
- **WHEN** a release starts with cleanup disabled
- **THEN** manual deletion, handoff deletion, age retention, providers, both MCP planes, REST, Web UI, and WebSocket behavior remain compatible

#### Scenario: Disable rollback
- **WHEN** an operator disables an enabled policy
- **THEN** CAO performs no future automatic deletion and documents that deleted resources are not recreated

#### Scenario: Authority boundaries
- **WHEN** cleanup is enabled
- **THEN** agent profiles, inbox bodies, MCP notifications, and unauthenticated WebSocket clients cannot trigger or broaden cleanup

#### Scenario: Already-secure database directory
- **WHEN** a read-only CAO command imports database support and the database directory already has mode `0700`
- **THEN** CAO performs no chmod and emits no permission warning

#### Scenario: Insecure database directory
- **WHEN** the database directory is not `0700`
- **THEN** CAO attempts to correct it and reports a warning only if that required correction fails

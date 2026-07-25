## Why

Long-running CAO installations retain completed Herdr workspaces, ghost terminal
rows, and inbox messages for deleted receivers until an operator cleans them
manually. On the observed host this accumulated 53 Herdr workspaces, 113 terminal
rows, and 474 receiver-orphaned inbox rows. Documented memory and
extra-directory settings also have asymmetric read/write or path-resolution
behavior.

The same live review exposed two availability defects in Kimi initialization:
its synchronous startup-dialog loop blocks the CAO event loop for the full
provider timeout, and the current ready-state detector can then consume another
full timeout without recognizing a usable prompt. A Kimi worker launch made
`/health` unavailable for about 120 seconds. Runtime resource governance cannot
claim request-loop availability while that sibling initialization path remains.

## What Changes

- Add opt-in, user-level runtime resource governance for completed Herdr
  workspaces with a grace period, preserve patterns, a bounded sweep interval,
  and a maximum deletion batch.
- Use Herdr's native workspace `agent_status`, which is present in the installed
  Herdr 0.7.5 `workspace list` payload. Missing, malformed, duplicate, or
  unrecognized inventory fails closed.
- Bind every automatic deletion to the captured Herdr `workspace_id`, revalidate
  label-to-ID identity immediately before teardown, and close the authoritative
  backend resource before deleting its persistence mapping.
- Run cleanup under `cao-server` lifespan, off the request loop, under both an
  in-process task lock and a nonblocking inter-process file lock. Shutdown
  shields and awaits an in-flight worker before plugin teardown.
- Route every new automatic cleanup deletion through the existing session
  service so provider, backend, plugin, environment, and database ownership stay
  centralized. Existing legacy/manual bypasses are not silently claimed as part
  of this change.
- Delete inbox rows whose receiver terminal is deleted in the same SQLite
  transaction as the terminal row. Serialize receiver validation/insertion and
  deletion with `BEGIN IMMEDIATE`, and configure a bounded SQLite busy timeout.
- Reconcile persisted non-peer CAO sessions and tabs against strictly parsed,
  authoritative Herdr inventory. A valid empty inventory can remove ghosts; a
  missing key, wrong type, command failure, timeout, or malformed element cannot.
- Preserve tmux behavior. Tmux has no authoritative native completed-workspace
  state, so supervisors continue deleting assigned tmux workers explicitly and
  successful handoffs keep their existing behavior.
- Make documented memory settings symmetrically readable and writable, persist
  all settings through a temporary file plus `fsync`/`os.replace`, and avoid
  redundant permission mutations when the CAO database directory is already
  secure.
- Normalize configured agent and skill extra directories at use time while
  preserving stored spelling. Newly activated tilde paths are documented and
  sensitive-root paths outside CAO's own state directory fail closed.
- Keep cleanup observability bounded to counts and reason codes. Remove
  plaintext memory keys from the existing retention sweep as well as avoiding
  inbox bodies, terminal output, credentials, resolved secrets, and provider
  environment values.
- Offload Kimi's startup-dialog handler from the event loop and make prompt
  readiness terminate the handler without requiring an upgrade dialog first.
- Document safe cleanup, recovery, configuration, irreversible deletion, and
  future-sweep rollback behavior.

Explicit non-goals:

- Do not automatically delete blocked, idle, unknown, processing, ambiguous, or
  recently completed sessions.
- Do not infer tmux completion from terminal text.
- Do not infer Herdr completion from terminal text or stale CAO status; only the
  native workspace `agent_status` is authoritative.
- Do not change Herdr's persistence format, memory retention periods, delivered
  message retention, or public provider completion semantics.
- Do not add legacy SSE, a database schema migration, wider network access, new
  authentication scopes, terminal-content exposure, or a new WebSocket trust
  boundary.
- Do not make MCP notifications, inbox bodies, or agent profiles authoritative
  for cleanup.
- Do not reclaim unrelated worktrees, build caches, or operating-system storage.
- Do not promise restoration of deleted resources. Disabling cleanup prevents
  future deletion only.

## Capabilities

### New Capabilities

- `cao-runtime-resource-governance`: Default-off bounded cleanup of completed
  Herdr workspaces, strict backend/database reconciliation, transactional
  receiver-inbox cascading, portable and safe extra-directory resolution,
  symmetric configuration persistence, and nonblocking provider initialization.

### Modified Capabilities

None. There is no existing main OpenSpec capability for runtime resource
governance. Existing terminal, session, inbox, configuration, API, CLI, MCP, and
backend behavior remains compatible while the new cleanup policy is disabled.

## Impact

- Runtime services: `api/main.py`, `services/cleanup_service.py`,
  `services/runtime_resource_cleanup.py`, `services/session_service.py`,
  `services/terminal_service.py`, Herdr reconciliation, and lifespan-owned tasks.
- Providers: `providers/kimi_cli.py` startup work moves off-loop and recognizes
  a ready prompt without waiting for an upgrade dialog.
- Persistence: terminal deletion cascades receiver inbox rows transactionally;
  startup reconciliation removes confirmed ghosts. No schema migration is used.
- Configuration and CLI: unified `settings.json` gains validated cleanup keys,
  symmetric memory writes, and atomic replacement. Existing absolute paths and
  legacy keys remain readable.
- Backends: Herdr exposes its existing native workspace state and stable
  workspace ID to the cleanup service; tmux behavior is unchanged.
- HTTP, Web UI, MCP, and WebSocket: no new authority boundary or protocol shape.
  Existing authenticated settings/session endpoints retain their scopes.
- Security and privacy: newly activating tilde paths are explicit; sensitive
  extra directories fail closed; cleanup emits no content or credential values.
- Operations: rollout is default-off. Disabling the policy stops future sweeps;
  already deleted workspaces are not recreated.

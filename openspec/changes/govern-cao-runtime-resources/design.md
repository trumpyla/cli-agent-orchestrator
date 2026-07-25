## Context

CAO currently has three independent retention paths:

- successful orchestration handoffs delete their assigned terminal;
- `cleanup_old_data()` removes old database rows and logs after
  `RETENTION_DAYS`; and
- Herdr startup reconciliation removes persisted terminals whose tab is absent
  from a workspace that is present in inventory.

They do not close completed Herdr workspaces, remove sessions whose entire
workspace disappeared, or cascade inbox rows addressed to a deleted terminal.
The observed host reached 53 live workspaces, 113 terminal rows, 21 database-only
sessions, and 474 receiver-orphaned inbox rows.

Current reconciliation is conservative on command and JSON exceptions but is not
strict about the response schema: `.get(..., [])` converts a missing
`result.workspaces` or `result.tabs` key into an authoritative empty inventory.
That is harmless while only live workspaces are iterated, but becomes destructive
when missing-workspace deletion is added.

The installed Herdr 0.7.5 `workspace list` response contains
`workspace_id`, `label`, `agent_status`, `pane_count`, and `tab_count`.
`HerdrBackend.list_sessions()` currently discards `agent_status` and replaces it
with the constant `active`. Cleanup may use the native workspace status, but must
also bind teardown to `workspace_id`; labels can be reused.

Two other mismatches are in scope:

- `memory.compile_mode` and `memory.compile_timeout_s` are readable but not
  writable; `settings.json` writes are not atomic; and
- extra directories are normalized for comparison but constructed through
  unexpanded `Path(...)` values during discovery.

Finally, Kimi initialization invokes a synchronous one-second polling loop
directly inside `async initialize()`. A live exact-K3 launch blocked every CAO
HTTP surface for roughly 120 seconds and then spent another readiness timeout
without reaching a recognized prompt.

## Goals / Non-Goals

**Goals:**

- Delete only uniquely identified Herdr workspaces whose native workspace status
  is exactly `done`, after all configured safety gates pass twice.
- Preserve all tmux work, ambiguous identities, pending delivery, protected
  names, missing mappings, unusable timestamps, and nonterminal native states.
- Bind selection and teardown to the same `workspace_id`.
- Serialize sweeps in-process and across CAO processes without blocking the
  event loop, and await any in-flight worker before plugin teardown.
- Make automatic deletion crash-resilient by closing the identified backend
  workspace before removing its database mapping.
- Atomically cascade receiver inbox rows and prevent concurrent create/delete
  receiver orphans without a schema migration.
- Strictly distinguish valid empty Herdr inventory from malformed or incomplete
  inventory.
- Make documented settings symmetric and atomically persisted.
- Resolve portable paths safely and explicitly handle upgrade-time activation.
- Keep logs content-free and remove plaintext memory keys from retention logs.
- Keep Kimi initialization off-loop and recognize a usable prompt without an
  upgrade dialog.

**Non-Goals:**

- Infer completion from terminal text, provider output, or stale CAO status.
- Delete sessions for any workspace state except exact native `done`.
- Auto-delete tmux sessions.
- Change delivered-message age retention, Herdr persistence, public authority,
  provider completion semantics, or authentication behavior.
- Restore resources already deleted by the policy.
- Reclaim unrelated disk caches or worktrees.

## Decisions

### 1. Typed default-off configuration with atomic persistence

`ConfigService` gains a top-level `CleanupConfig`:

```json
{
  "cleanup": {
    "completed_sessions_enabled": false,
    "completed_session_grace_s": 900,
    "sweep_interval_s": 300,
    "max_sessions_per_sweep": 5,
    "preserve_patterns": []
  }
}
```

Pydantic v2 strict fields reject coerced booleans, grace below 60 seconds,
interval below 30 seconds, batch size outside 1–50, more than 100 patterns,
patterns over 128 characters, and control characters. Every `cao config set`
operation validates the fully merged section before persistence.

Both `settings_service._save()` and `config_service._save_raw()` write an
owner-only temporary file in the target directory, flush and `fsync` it, then
replace `settings.json` with `os.replace`. A failed validation or write leaves
the prior bytes intact. Runtime reads continue to resolve env > file > default
and re-read file changes by mtime.

`_ensure_db_dir()` first inspects the existing mode. It skips `chmod` when the
directory is already `0700`, so read-only commands in a sandbox do not emit a
false `EPERM` warning. A real insecure mode still triggers a correction attempt
and a warning if that correction fails.

### 2. Preserve the backend list contract while exposing native Herdr state

`HerdrBackend.list_sessions()` keeps the public three-key
`{"id", "name", "status"}` shape and maps `status` from the native workspace
`agent_status`. It accepts only dictionary elements with a non-empty
`workspace_id`, non-empty label, and recognized status
(`done`, `blocked`, `idle`, `working`, `unknown`). A malformed element or
duplicate label makes the inventory unusable for cleanup.

The Herdr adapter also exposes an internal typed workspace inventory containing
the stable `workspace_id`, label, and native status. This is not added to the
public session API. Live-schema characterization tests use a complete Herdr
0.7.5 response fixture, including pane/tab counts and `active_tab_id`, so the
adapter cannot silently discard or invent the status field again.

Tmux retains its existing list output and is rejected before candidate
evaluation.

### 3. Select conservatively and revalidate stable identity

A synchronous sweep receives the backend, registry, settings reader, repository
functions, and a clock. It:

1. loads and validates cleanup settings;
2. returns when disabled or when the backend is not Herdr;
3. acquires a nonblocking process-wide cleanup file lock;
4. loads strict typed workspace inventory;
5. keeps only unique labels beginning with `SESSION_PREFIX`, excluding
   `__peers__`, whose native workspace status is exactly `done`;
6. requires a non-empty persisted terminal set and rejects provider `peer`;
7. rejects preserve-pattern matches and any pending receiver inbox row;
8. requires every timestamp to be local-naive, not in the future, and the newest
   activity to be older than the local-naive grace cutoff;
9. sorts by oldest activity then label and applies the configured batch limit.

The local-naive convention matches `TerminalModel.last_active`. A future
timestamp, aware/naive mismatch, negative age, or detected wall-clock jump
beyond a small fixed tolerance skips the entire sweep with a reason code.
The daemon compares wall-clock and monotonic deltas between ticks to detect NTP
or manual clock jumps.

Immediately before teardown, cleanup refreshes inventory and pending delivery,
then requires the same label, exact `done` state, and identical `workspace_id`.
Label reuse therefore preserves the new workspace.

### 4. Close the captured backend identity before persistence cleanup

The automatic path calls a session-service operation with
`expected_backend_id`. The session service asks `HerdrBackend` to revalidate
label-to-ID identity and close that exact `workspace_id` first. Only after a
successful close (or an authoritative already-absent result) does it run
terminal/provider/environment/plugin/database cleanup.

This ordering prevents a backend-close failure or process crash from deleting
the only persistence mapping for a still-live workspace. Terminal cleanup is
already tolerant of an absent pane and still releases provider, FIFO, status,
plugin, and database state.

The claim is intentionally scoped to the new automatic cleanup path. Existing
manual and legacy internal deletion call sites remain compatible and are not
misrepresented as migrated by this change.

### 5. Lifespan ownership, non-overlap, and shutdown

FastAPI lifespan owns one daemon task. The daemon sleeps before the first sweep,
re-reads settings before each interval and sweep, and runs the synchronous sweep
through a stored `asyncio.Task(asyncio.to_thread(...))`.

An `asyncio.Lock` prevents in-process overlap. A nonblocking `fcntl.flock` under
the CAO state directory prevents two server processes from sweeping the same
database concurrently; an unavailable lock records `lock_held` and skips.

The worker task is awaited through `asyncio.shield`. If shutdown cancels the
daemon during a sweep, the daemon catches `CancelledError`, awaits the still-live
worker, then re-raises cancellation. Lifespan awaits the daemon before registry
teardown. No boolean flag is reset while a worker still runs.

Tests inject interval triggers, wall/monotonic clocks, and threading barriers;
they do not sleep for the production minimum interval.

### 6. Transactional receiver cascade and bounded SQLite contention

Database connections use a bounded SQLite timeout. Receiver message creation and
terminal deletion start `BEGIN IMMEDIATE` before validating or mutating the
receiver relationship.

The deletion primitive removes every `InboxModel` row whose `receiver_id` is in
the terminal set, then removes the matching terminal rows, and commits once.
Sender history remains when its receiver is live. An exception rolls the whole
transaction back.

With the write lock acquired before receiver validation, a create/delete race
has two outcomes: creation commits first and deletion cascades it, or deletion
commits first and creation observes no receiver. Busy timeout exhaustion fails
the operation without a partial commit and is reported categorically.

No foreign-key or schema migration is introduced.

### 7. Strict, off-loop Herdr startup reconciliation

Reconciliation parses the exact response envelope:

- `result` must be a mapping;
- `result.workspaces` or `result.tabs` must be present and a list;
- every workspace/tab element must be a mapping with the required typed IDs and
  labels; and
- duplicate workspace labels fail the whole pass closed.

A valid, correctly typed empty workspace list is authoritative and can remove
persisted ghosts. A missing key, wrong type, malformed element, command failure,
timeout, or JSON error deletes nothing.

The whole synchronous inventory/database pass runs through `asyncio.to_thread`
before the Herdr socket loop starts, keeping the event loop responsive. It
compares every persisted non-peer CAO session with the live label set, preserves
`__peers__`, retains the existing missing-tab check, and uses the transactional
receiver-cascade primitive.

One info-level summary reports missing-workspace, missing-tab, deleted-terminal,
and failure counts plus stable reason codes. It never logs terminal IDs, session
labels, message bodies, or provider data.

### 8. Symmetric memory settings and safe portable paths

`set_memory_setting()` accepts exact `llm`/`append`, finite
`0 < compile_timeout_s <= 3600`, and the existing enabled/threshold keys. Reads
and writes share validators; invalid values preserve prior file bytes.

Agent and skill scanners construct every discovery/source-load path from
`normalized_path()` while retaining configured spelling in settings. Before
activation, resolved extra directories are rejected if they are inside
`~/.ssh`, `~/.gnupg`, or `~/.aws` outside CAO's own state directory. Existing
CAO-owned defaults under `CAO_HOME_DIR` remain valid.

Documentation calls out that previously inert tilde entries become active after
upgrade. Config and startup logs identify only the configured index and a stable
reason such as `sensitive_root`; they do not emit the resolved path.

### 9. Content-free observability

Each cleanup/reconciliation run emits bounded counts and stable reason codes.
The existing memory-retention sweep stops logging `entry["key"]`; it logs scope,
memory type, and aggregate success/failure counts only. Exceptions are sanitized
so provider environment values, paths, terminal output, inbox bodies, tokens,
and authorization values cannot be included.

### 10. Nonblocking and recognizable Kimi initialization

`KimiCliProvider.initialize()` runs `_handle_startup_dialog` through
`await asyncio.to_thread(...)`, matching Claude and Antigravity. The handler
returns immediately when either terminal history or Herdr native state proves a
usable prompt, even when no upgrade dialog was ever shown.

The dialog handler retains its outer timeout for genuinely late dialogs, but
ready-state detection is factored into a side-effect-free predicate with fixtures
for the installed Kimi UI. Initialization still requires the subsequent
`wait_until_status` gate; no provider completion semantics are weakened.

Regression tests hold the handler behind a threading barrier while an event-loop
heartbeat and `/health` request complete, then exercise ready, upgrade-dialog,
unknown, and timeout outputs.

## Risks / Trade-offs

- Native Herdr status could drift. Strict complete fixtures and fail-closed schema
  validation make drift observable instead of inventing a state.
- Closing a workspace before provider cleanup loses final scrollback if the
  pre-close snapshot fails. The automatic path snapshots first, then closes the
  captured ID, then completes teardown.
- A process can crash after backend close but before DB deletion. Startup
  reconciliation is the idempotent resume path.
- `fcntl` is unavailable on Windows. Herdr is currently a Unix-local backend;
  unsupported platforms skip automatic cleanup rather than running unlocked.
- Busy SQLite writers may defer a sweep. The batch is retried only on a later
  interval; no immediate retry loop is added.
- Portable paths activate previously inert configuration. Sensitive-root checks,
  documentation, and tests make that upgrade effect explicit.
- Deletion is irreversible. Default-off rollout, a grace period, preserve
  patterns, stable identity, and two validations reduce risk; rollback stops
  future work only.

## Migration Plan

1. Land failing contract tests for Herdr 0.7.5 inventory, Kimi responsiveness,
   config atomicity, database races, and cleanup selection before implementation.
2. Run existing tmux/Herdr lifecycle, inbox, config, provider, API, MCP, and
   non-E2E suites with cleanup disabled.
3. Deploy with `cleanup.completed_sessions_enabled=false`.
4. Start an isolated server/database/Herdr session and validate exact `done`
   cleanup plus preserved blocked/idle/unknown/pending/pattern controls.
5. Verify label reuse, valid empty inventory, malformed inventory, clock jump,
   concurrent database writes, and process-lock contention all fail safely.
6. Enable a long grace period locally only after `/health` remains responsive
   during Kimi initialization and slow cleanup teardown.
7. Roll back future sweeps by setting
   `cleanup.completed_sessions_enabled=false`. Restore the backed-up settings or
   database only for operator-led recovery; automatic restoration is not claimed.

## Deferred Follow-ups

- Web UI editing of cleanup settings is deferred; the first release is
  CLI/settings-file only.
- Any tmux cleanup policy is deferred and would require explicit operator
  confirmation rather than text inference.
- Configurable audit retention for delivered inbox rows is deferred; this change
  only cascades receiver deletion and preserves existing age retention.

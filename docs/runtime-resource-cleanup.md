# Runtime resource cleanup

CAO can conservatively remove completed Herdr workspaces and their persisted
terminal/inbox state. The feature is **off by default** because deletion is
irreversible.

This cleanup is resource lifecycle management, not a disk-space reclamation
tool. It deletes eligible CAO sessions; it does not prune model caches, package
caches, logs, worktrees, downloads, or unrelated Herdr sessions.

## Configuration

The complete schema is:

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

| Setting | Default | Validation | Purpose |
|---|---:|---:|---|
| `completed_sessions_enabled` | `false` | strict boolean | Master switch. |
| `completed_session_grace_s` | `900` | at least 60 | Minimum age since the newest persisted terminal activity. |
| `sweep_interval_s` | `300` | at least 30 | Delay before the first sweep and between later sweeps. |
| `max_sessions_per_sweep` | `5` | 1–50 | Bound on irreversible deletions in one sweep. |
| `preserve_patterns` | `[]` | at most 100 glob strings, 128 characters each, no control characters | Case-sensitive `fnmatch` patterns matched against the complete CAO session label. |

The daemon re-reads settings before every sweep. Enabling, disabling, changing
the grace period, changing preserve patterns, or changing the batch limit does
not require a server restart:

```bash
cao config set cleanup.completed_session_grace_s 3600
cao config set cleanup.max_sessions_per_sweep 2
cao config set cleanup.preserve_patterns '["cao-*supervisor*", "cao-release-*"]'
cao config set cleanup.completed_sessions_enabled true
```

Set the policy first and enable it last. A conservative supervisor-safe
starting point is a one-hour grace, a batch of two, and preserve patterns for
long-lived conductor/supervisor labels.

The first sweep is delayed by `sweep_interval_s`; startup never immediately
deletes sessions. The environment variable
`CAO_CLEANUP_COMPLETED_SESSIONS_ENABLED` can override the boolean setting, but
the file-based policy is easier to audit.

## Eligibility and stable identity

A workspace is eligible only when all gates pass:

- the active backend is Herdr; tmux is intentionally unsupported;
- Herdr's native workspace status is exactly `done`;
- the label starts with CAO's `cao-` prefix and is not `__peers__`;
- no preserve pattern matches;
- the workspace label maps to one non-empty, strictly parsed terminal inventory;
- terminal IDs are unique and no terminal has provider `peer`;
- no receiver has a pending inbox message;
- every terminal has a valid naive `last_active` timestamp;
- the newest activity is older than the configured grace period; and
- the candidate is within the oldest-first bounded batch.

Before deletion, CAO reads Herdr inventory and the database again. The exact
captured Herdr `workspace_id`, label, native `done` status, terminal inventory,
and pending-inbox state must still match. Label reuse, a resumed agent, a new
pending message, ambiguous/duplicate inventory, a future/aware timestamp, or a
backend read failure cancels that deletion.

If Herdr already reports the captured workspace absent, CAO resumes only the
persisted cleanup. If the stable ID changed, it preserves the new workspace.

## Transaction and teardown order

For an eligible session CAO:

1. snapshots terminal/provider cleanup metadata;
2. closes the exact captured Herdr workspace ID;
3. releases provider, environment, FIFO/status, and plugin state; and
4. deletes receiver-side inbox rows and terminal rows atomically.

If the backend close fails, persisted state is preserved for investigation. If
the backend closed but persistence cleanup failed, a later sweep can resume the
database side without closing a replacement workspace.

The database uses an immediate SQLite transaction for receiver deletion and
concurrent inbox insertion. A competing sender either commits before cleanup
and is deleted with the receiver, or observes that the receiver no longer
exists. It cannot leave a receiver-orphan inbox row.

## Scheduling and shutdown

The lifespan-owned daemon runs the synchronous inventory and cleanup unit in a
worker thread so request, WebSocket, MCP, and Herdr event loops remain
responsive. An in-process async lock prevents overlapping sweeps; a
mode-`0600`, nonblocking file lock prevents two CAO processes sharing a home
from sweeping together. Large wall-clock/monotonic-clock disagreement skips a
sweep.

During shutdown CAO cancels the scheduler first. If a worker sweep is already
running, shutdown waits for it to finish before Herdr, provider, or plugin
teardown. No cleanup thread is detached.

## Observability

Each sweep emits one content-free aggregate log:

```text
runtime_cleanup selected=2 deleted=1 reasons={'pending_inbox': 1}
```

Reason codes and counts may be logged. Session labels, workspace IDs, terminal
IDs, message bodies, memory keys, configured paths, resolved URLs, and
exception text are not included.

## Disable and recover

Disabling affects future sweeps only; it cannot restore deleted sessions:

```bash
cao config set cleanup.completed_sessions_enabled false
```

Before first enablement:

1. stop or quiesce changes to CAO settings;
2. back up `~/.aws/cli-agent-orchestrator/settings.json`;
3. back up `~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db`;
4. record active supervisor/conductor label patterns;
5. configure those patterns in `cleanup.preserve_patterns`; and
6. start with a long grace and small batch.

If a sweep behaves unexpectedly, disable it, retain the server log's aggregate
reason counts, and inspect Herdr/database state from the backups. Do not delete
the database directory to “reset” cleanup.

For `No space left on device`, inspect actual filesystem usage and clean the
responsible caches/logs/worktrees separately. Enabling CAO session cleanup is
not a safe substitute for disk-capacity remediation.

## Related configuration hardening

CAO writes settings atomically through an owner-only temporary file, flushes
and `fsync`s it, then replaces the destination. A failed write leaves the old
configuration intact.

Agent and skill directories accept `~`, symlink, and trailing-slash spellings.
CAO stores the spelling supplied by the operator but canonicalizes it for
comparison and loading. User-level config may therefore contain an absolute
machine path when the server must find a checkout from any working directory;
do not commit that user-specific setting into the repository. Committed
profiles and examples must use repository-relative or documented symbolic
paths.

CAO rejects configured directories under `~/.ssh`, `~/.gnupg`, or the general
`~/.aws` tree. The CAO-owned `~/.aws/cli-agent-orchestrator` subtree remains
allowed. Rejections use only the `sensitive_root` reason and do not log the
configured or resolved path.

The CAO database directory is created as mode `0700`. CAO avoids an unnecessary
`chmod` when the directory already has that mode, which prevents false
`Operation not permitted` warnings on managed filesystems that permit access
but deny metadata changes.

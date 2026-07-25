# Implementation gate evidence

Recorded 2026-07-25. Evidence is aggregate-only; no session labels, terminal
IDs, workspace IDs, inbox bodies, memory keys, resolved paths, credentials, or
provider responses are retained.

## Lifecycle and compatibility

- Cleanup daemon plus REST/health/TrustedHost/CORS/inbox/settings regressions:
  189 passed.
- WebSocket, API coverage, and CAO Ops stdio MCP regressions: 134 passed.
- Lifespan teardown-order barrier proved cleanup completion before plugin
  registry teardown.
- The non-overlap barrier ran 10 deterministic repetitions.
- Black, isort, and focused mypy for the daemon, cleanup engine, and FastAPI
  lifespan passed.
- Existing SQLite `ResourceWarning` instances remain visible and were not
  suppressed.

## Supervisor harness

- Source-backed credential-free lifecycle: 1 passed in 20.37 seconds.
- Exact-provider preflight contract: 9 passed.
- Exact live matrix collected successfully and remained opt-in: 1 skipped
  because `CAO_LIVE_SUPERVISOR_MATRIX` was not set.
- No live provider/model lane was launched during these gates.

## Isolated Herdr runtime proof

- Source-backed server, isolated HOME/database/port/named Herdr session:
  1 passed in 67.43 seconds.
- Herdr's semantic lifecycle API drove non-focused `working` to `idle`
  transitions and the inventory exposed the resulting authoritative `done`
  state.
- The feature-disabled sweep retained all candidates. Hot settings re-read
  then deleted only the old, exact-done, unprotected candidate.
- Preserve-pattern, pending-inbox, and non-done controls survived; the
  receiver-orphan count remained zero and `/health` stayed responsive.
- Remaining workspaces were deleted and observed as authoritative-empty before
  restart. A valid hexadecimal stale terminal plus inbox receiver was injected;
  startup reconciliation removed both without creating a Herdr workspace.
- The proof used `mock_cli` and no provider authentication, network request, or
  model call.

## Artifact integrity

- `openspec validate govern-cao-runtime-resources --strict`: passed.
- Markdown link validation: passed.
- `git diff --check`: passed.

## Known repository-wide gate

Focused mypy for every modified source module is green. The full
`mypy src/` gate remains open because the baseline environment reports missing
optional OpenTelemetry/AG-UI import typing plus unrelated AG-UI
`no-any-return` errors. Task 2.10 and the final full-gate task remain unchecked
until the repository-wide command is zero-error.

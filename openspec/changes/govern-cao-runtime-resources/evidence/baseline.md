# Pre-implementation baseline

Captured on 2026-07-25 before production-code edits in the
`feat/cao-runtime-cleanup` worktree.

## Repository and specification

- Worktree: `.worktrees/cao-runtime-cleanup`
- Branch: `feat/cao-runtime-cleanup`
- Base: `peer-inbox-bridge`
- Initial worktree status: only the untracked
  `openspec/changes/govern-cao-runtime-resources/` change artifacts
- Strict validation:
  `rtk openspec validate govern-cao-runtime-resources --strict`
  returned `Change 'govern-cao-runtime-resources' is valid`.

## Live CAO and Herdr

The host-network health request returned HTTP success with CAO and Herdr
components healthy and `terminal_backend` set to `herdr`. A request from the
restricted command sandbox could not reach the host listener; repeating the
same read-only request in the host network namespace succeeded. No process was
terminated or relaunched after the existing listener was identified.

Installed Herdr 0.7.5 reports workspace inventory entries containing:

- `workspace_id`
- `label`
- `agent_status`
- `pane_count`
- `tab_count`
- `active_tab_id`

Values that identify user sessions or workspaces are intentionally omitted.

The redacted live inventory contained 33 workspaces: 11 `blocked`, 3 `done`,
9 `idle`, and 10 `unknown`. The SQLite database contained 49 terminal rows and
431 inbox rows. These are descriptive baseline counts, not cleanup targets.

The preceding manual recovery produced separate before/after evidence:
53-to-28 Herdr workspaces, 113-to-44 terminal rows, and 474 receiver-orphan
inbox rows removed. Those historical counts are retained only to explain the
resource-growth incident; the implementation must independently re-evaluate
every candidate and must never infer eligibility from an aggregate count.

## Focused test baseline

Command:

```text
rtk uv run pytest \
  test/services/test_settings_service.py \
  test/services/test_config_service.py \
  test/cli/commands/test_config.py \
  test/utils/test_agent_profiles.py \
  test/utils/test_skills.py \
  test/clients/test_database.py \
  test/clients/test_database_permissions.py \
  test/services/test_session_service.py \
  test/backends/test_herdr_backend.py \
  test/backends/test_herdr_inbox_service.py \
  test/services/test_cleanup_service.py \
  test/providers/test_kimi_cli_unit.py \
  test/providers/test_kimi_session.py \
  test/api/test_lifespan_inbox.py -q
```

Result: `453 passed, 43 warnings in 4.85s`.

The warnings are baseline debt rather than failures. They include dependency
deprecations and existing unclosed-SQLite `ResourceWarning` reports. Later gates
must remain green and must not add warning classes attributable to this change.

## Context7 availability

The required Context7 library-resolution request for Herdr failed with a
monthly-quota error. Live behavior is therefore characterized from the
installed Herdr 0.7.5 CLI and repository source. Documentation lookup can be
rerun after `npx ctx7@latest login` or after setting `CONTEXT7_API_KEY`.

# Requirement-to-test matrix

This matrix is the durable verification index for the 12 requirements and their
67 scenarios. Scenario-level tests use descriptive names, parametrized case IDs,
real SQLite databases where transactions matter, and event/barrier coordination
instead of timing sleeps.

| # | Requirement | Primary verification | Task group | Status |
|---:|---|---|---|---|
| 1 | Validated and atomically persisted cleanup configuration | `test/services/test_settings_service.py`, `test/services/test_config_service.py`, `test/cli/commands/test_config.py`, `test/clients/test_database_permissions.py` | 2.1-2.7 | Focused gates passed |
| 2 | Authoritative conservative Herdr eligibility | `test/backends/test_herdr_backend.py`, `test/services/test_cleanup_service.py`, `test/e2e/test_runtime_cleanup_herdr_proof.py` | 5.1-5.4, 6.1-6.4, 8.3 | Focused and isolated-live gates passed |
| 3 | Stable-identity revalidation and teardown | `test/backends/test_herdr_backend.py`, `test/services/test_cleanup_service.py` | 5.4, 6.2-6.5 | Focused gates passed |
| 4 | Lifespan-owned nonblocking and serialized cleanup | `test/services/test_cleanup_service.py`, `test/services/test_runtime_cleanup_daemon.py`, API lifespan cleanup tests | 6.1-6.7, 7.1-7.5 | Focused gates passed |
| 5 | Session-service-owned automatic deletion | `test/services/test_session_service.py`, `test/services/test_cleanup_service.py`, `test/e2e/test_runtime_cleanup_herdr_proof.py` | 6.5-6.6, 8.3 | Focused and isolated-live gates passed |
| 6 | Transactional receiver-inbox cascade | `test/clients/test_database.py`, `test/clients/test_database_receiver_cascade.py`, `test/services/test_session_service.py`, `test/e2e/test_runtime_cleanup_herdr_proof.py` | 4.1-4.5, 8.3 | Focused and isolated-live gates passed |
| 7 | Complete strict Herdr startup reconciliation | `test/backends/test_herdr_inbox_service.py`, `test/services/test_herdr_startup_reconciliation.py`, `test/e2e/test_runtime_cleanup_herdr_proof.py` | 5.1-5.3, 7.1-7.5, 8.3 | Focused and isolated-live gates passed |
| 8 | Symmetric and atomic memory configuration | `test/services/test_settings_service.py`, `test/services/test_config_service.py`, CLI configuration tests | 2.2, 2.4-2.5 | Focused gates passed |
| 9 | Portable and safe extra-directory resolution | `test/utils/test_agent_profiles.py`, `test/utils/test_skills.py`, `test/utils/test_portable_config_paths.py` | 2.8-2.10 | Focused gates passed; full mypy pending |
| 10 | Privacy-preserving cleanup observability | `test/services/test_cleanup_service.py`, `test/services/test_runtime_resource_cleanup.py`, memory cleanup log-capture tests | 6.1-6.7, 8.1-8.3 | Focused gates passed |
| 11 | Nonblocking and recognizable Kimi initialization | `test/providers/test_kimi_cli_unit.py`, `test/providers/test_kimi_session.py`, `test/api/test_kimi_initialization_health.py` | 3.1-3.5 | Focused gates passed |
| 12 | Compatible rollout and future-sweep rollback | focused suite, complete non-E2E suite, docs/link validation, live CAO/Herdr checks | 8.1-8.4, 9.1-9.6 | Pending gates |

## Cross-cutting scenario coverage

- Defaults, bounds, enum values, unknown keys, invalid persisted data, and
  atomic-write failure stages are table-driven.
- Eligibility covers opt-out, prefix, peers, zero-row ambiguity, native status,
  timestamp parsing, future timestamps, clock skew, idle threshold, and
  workspace-ID replacement between discovery and teardown.
- Concurrency covers overlapping sweeps, cancellation during an offloaded
  backend call, shutdown awaiting, and two-connection create/delete races.
- Reconciliation distinguishes a valid authoritative empty inventory from
  malformed, missing, partial, and failed inventory responses.
- Sensitive-path checks cover tilde expansion, environment expansion,
  nonexistent paths, directories outside `CAO_HOME`, and explicit sensitive
  roots such as `.ssh`, `.gnupg`, and `.aws`.
- Logging tests assert aggregate identifiers and counts while rejecting inbox
  bodies, memory keys, credentials, and resolved sensitive paths.
- Kimi fixtures cover ready-without-upgrade, supported upgrade prompts,
  unsupported dialogs, handler blocking, timeout, cancellation, and concurrent
  `/health` responsiveness.

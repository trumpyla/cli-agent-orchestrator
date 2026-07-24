# OpenSpec Project Configuration Design

## Goal

Replace the generic `openspec/config.yaml` template with durable guidance for
the whole CLI Agent Orchestrator (CAO) repository. The configuration should
give an agent enough project context to produce compatible proposals,
specifications, designs, and task lists without duplicating volatile source
inventories or documentation that will drift.

## Scope

The configuration covers the complete repository. The peer-inbox bridge is
represented through general rules for asynchronous delivery, authorization,
cursor semantics, and compatibility rather than branch-specific instructions.

## Configuration Structure

Keep the existing `spec-driven` schema and add two sections:

1. A bounded YAML block-scalar `context` describes CAO's purpose, runtime
   architecture, technology stack, public control planes, persistence, security
   posture, scoped compatibility promises, repository layout, canonical
   documentation, and quality gates.
2. `rules` adds artifact-specific requirements for proposals, specifications,
   designs, and task lists.

## Content Principles

- State stable architectural facts and point to canonical documents for detail.
- Treat the HTTP API as the shared inbound service boundary for external CLI,
  Web UI, automation, and the external `cao-ops-mcp-server`.
- Keep `cao-ops-mcp-server` distinct from the in-session `cao-mcp-server`. The
  latter intentionally uses local services for some conductor/worker
  orchestration paths and is not evidence that external clients may bypass the
  HTTP boundary.
- Treat the full-PTY WebSocket as a separate localhost-only, unauthenticated
  surface. Any proposal that changes its bind or trust boundary must analyze the
  exposure of terminal contents and input.
- Distinguish real launchable agent providers, the launchable test-only
  `mock_cli`, and the synthetic persistence-only `peer` value.
- Treat tmux as the stable default backend and Herdr as experimental. Require
  changes to account for both when their terminal or lifecycle semantics apply.
- Preserve default-off authentication and telemetry compatibility while
  requiring explicit authorization and privacy analysis for protected
  operations. `cao:write` and `cao:admin` are fleet-wide authority, not
  per-session or per-peer capabilities.
- Preserve named public HTTP, MCP, CLI, and serialized-data surfaces unless a
  proposal explicitly defines a migration. Do not infer an unlimited
  compatibility promise for private Python internals.
- Require asynchronous features to define ownership, cancellation, retry,
  timeout, deduplication, cursor progression, acknowledgement/idempotency,
  ordering, and cleanup behavior.
- Require user-facing behavior changes to update the corresponding maintained
  documentation.

## Artifact Rules

### Proposals

Every proposal identifies the problem, affected control planes, providers and
backends, compatibility and migration impact, security or privacy implications,
observability impact, and explicit non-goals.

### Specifications

Requirements use normative language and testable scenarios. Specifications
cover success, validation, authorization, timeout, cancellation, retry,
cursor/acknowledgement behavior, idempotency, and partial failure when those
concerns apply. Cross-plane behavior must identify the authoritative mechanism
and any supplemental wakeup or presentation layer.

### Designs

Designs identify component ownership, data flow, concurrency boundaries,
persistence effects, provider/backend differences, error mapping, observability,
and backward compatibility. They must explain task lifecycle and data-integrity
rules for asynchronous or long-running work.

### Tasks

Tasks follow test-driven ordering: add a failing regression test, implement the
smallest change, then refactor. Verification selects the applicable existing CI
gates from `.github/workflows/ci.yml`: focused tests; the complete non-E2E
Python suite using CI exclusions; Web UI build; MCP Apps typecheck, tests,
build, `scan:jit`, size, HTTP-boundary, and coverage checks; Black; isort; and
zero mypy errors. Documentation work also runs the Markdown-link checker
after new files are staged because it intentionally scans tracked files.
Security-sensitive work includes gitleaks and follows the PII-safe fixture rules
in `CONTRIBUTING.md`. Every change runs `git diff --check`.

## Drift Control

The configuration will avoid endpoint inventories, exact test counts,
dependency versions, and generated artifact contents. Runtime and tooling
details remain owned by `src/`, `pyproject.toml`, `.github/workflows/ci.yml`,
`README.md`, `docs/api.md`, and `docs/control-planes.md`. The root `skills/`
tree is canonical and its package mirror is checked by
`scripts/sync_skills.py --check`. Database schema evolution remains owned by
the migration sequence in `src/cli_agent_orchestrator/clients/database.py`.

## Verification

After editing the configuration:

- Parse it as YAML and assert the schema, context type, and four rule keys.
- Inspect the rendered values for accidental comments or template placeholders.
- Run `openspec validate --all --strict --json`.
- Stage new documentation before running
  `uv run python scripts/validate_markdown_links.py`; the checker obtains its
  input from `git ls-files`.
- Unstage without discarding content and run `git diff --check`.

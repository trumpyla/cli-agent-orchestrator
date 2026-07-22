# OpenSpec Project Configuration Design

## Goal

Replace the generic `openspec/config.yaml` template with durable guidance for the
whole CLI Agent Orchestrator (CAO) repository. The configuration should give an
agent enough project context to produce compatible proposals, specifications,
designs, and task lists without duplicating source files or documentation that
will drift.

## Scope

The configuration covers the complete repository. The peer-inbox bridge is
represented through general rules for asynchronous delivery, authorization,
cursor semantics, and compatibility rather than branch-specific instructions.

## Configuration Structure

Keep the existing `spec-driven` schema and add two sections:

1. `context` describes CAO's purpose, runtime architecture, technology stack,
   public control planes, persistence, security posture, compatibility promises,
   repository layout, canonical documentation, and quality gates.
2. `rules` adds artifact-specific requirements for proposals, specifications,
   designs, and task lists.

## Content Principles

- State stable architectural facts and point to canonical documents for detail.
- Treat the HTTP API as the shared service boundary for CLI, Web UI, MCP, and
  automation clients.
- Distinguish launchable agent providers from synthetic or persistence-only
  provider values.
- Require changes to account for both tmux and Herdr backends where relevant.
- Preserve default-off authentication compatibility while requiring explicit
  authorization analysis for protected operations.
- Preserve public API and serialized-data compatibility unless a proposal
  explicitly defines a migration.
- Require asynchronous features to define ownership, cancellation, retry,
  timeout, deduplication, ordering, and cleanup behavior.
- Require user-facing behavior changes to update the corresponding maintained
  documentation.

## Artifact Rules

### Proposals

Every proposal identifies the problem, affected control planes and providers,
compatibility and migration impact, security or privacy implications, and
explicit non-goals.

### Specifications

Requirements use normative language and testable scenarios. Specifications cover
success, validation, authorization, timeout, cancellation, retry, and partial
failure behavior when those concerns apply. Cross-plane behavior must identify
the authoritative mechanism and any supplemental wakeup or presentation layer.

### Designs

Designs identify component ownership, data flow, concurrency boundaries,
persistence effects, provider/backend differences, error mapping, observability,
and backward compatibility. They must explain task lifecycle and data-integrity
rules for asynchronous or long-running work.

### Tasks

Tasks follow test-driven ordering: add a failing regression test, implement the
smallest change, then refactor. Verification includes focused tests, the complete
non-E2E Python suite using CI exclusions, Black, isort, mypy for touched code,
Markdown-link validation for documentation changes, and `git diff --check`.

## Drift Control

The configuration will avoid enumerating provider names, endpoint inventories,
exact test counts, dependency versions, and generated artifacts. Those details
remain owned by `src/`, `pyproject.toml`, `README.md`, `docs/api.md`, and
`docs/control-planes.md`.

## Verification

After editing the configuration:

- Parse it as YAML.
- Confirm the schema remains `spec-driven`.
- Inspect the rendered values for accidental comments or template placeholders.
- Run OpenSpec validation if the installed CLI supports validating configuration
  without requiring a change artifact.
- Run `git diff --check` on the resulting changes.

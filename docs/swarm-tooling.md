# Swarm Development Tooling

This repository ships its own CAO development swarm: repository-owned agent
profiles, a shared read-only Serena navigation endpoint, and ast-grep
structural gates. These are the tooling lanes used to develop the embedded
CAO Ops Streamable HTTP change; they are committed so every checkout gets the
same topology.

## Repository-owned profiles (`.cao/agents/`)

Five flat profiles cover the swarm lanes:

| Profile | Lane | Provider / Model | Mode |
| --- | --- | --- | --- |
| `cao-swarm-supervisor` | supervision | `kimi_cli` / `kimi-code/k3` | coordinates via `cao-mcp-server`; no shell |
| `cao-swarm-designer` | design | `antigravity_cli` / `gemini-3.1-pro-high` | read-only, `permissionMode: plan` |
| `cao-swarm-implementer` | implementation | `kimi_cli` / `kimi-code/k3` | write/execute scoped to the assigned worktree |
| `cao-swarm-tester` | testing | `kimi_cli` / `kimi-code/k3` | read-only, Kimi `--plan` (tool allowlist) |
| `cao-swarm-adversarial-reviewer` | adversarial review | `antigravity_cli` / `claude-opus-4-6-thinking` | read-only, `permissionMode: plan` |

- Supervisors advertise only `cao-supervisor-protocols`; workers advertise
  `cao-worker-protocols` plus their lane's Python skills (type-safety, design,
  error-handling, resource-management, async, testing, style, anti-patterns)
  from the Artagon Python skill directory. `review-verification-protocol` is
  deliberately not advertised: the installed copy is a stale protocol under
  audit, so review lanes use evidence-first prompts instead.
- Every profile carries the same MCP surfaces: the identity-bearing stdio
  `cao-mcp-server` plus managed HTTP entries — `context7` (documentation),
  `tavily` and `gemini-search` (corroborated web research), `duckduckgo`
  (fallback search), and `serena` (read-only symbol navigation). HTTP `url`
  values are exact `${ENV_NAME}` references (`${CAO_CONTEXT7_MCP_URL}`,
  `${CAO_TAVILY_MCP_URL}`, `${CAO_GEMINI_SEARCH_MCP_URL}`,
  `${CAO_DUCKDUCKGO_MCP_URL}`, `${CAO_SERENA_MCP_URL}`) — never literals, never
  secrets — and resolve fail-closed at terminal launch.
- Model identifiers are validated against the installed CLIs before use
  (`kimi provider list --json`, `agy models`); an unavailable exact model
  blocks the lane rather than downgrading it.

## Project settings (`.cao/settings.json`)

Registers the committed profile directory and skill directories through the
standard settings keys so profiles resolve without user-global copies:

```json
{
  "agents": {"extra_dirs": ["<checkout>/.cao/agents"]},
  "skills": {"extra_dirs": ["<checkout>/skills",
                             "<artagon-ai-skills>/plugins/artagon-python/skills"]}
}
```

The file holds machine-local absolute paths (the settings service performs no
`~`/env expansion); after moving the checkout, update the paths or merge the
keys into `~/.aws/cli-agent-orchestrator/settings.json`.

## Shared Serena navigation (`.serena/project.yml`)

`.serena/project.yml` configures the project for the managed warm Serena
daemon (started by artagon-scripts `scripts/mcp.sh`): Python language server,
`read_only: true` (edits remain native filesystem operations), gitignore
support, and exclusions for virtual environments, caches, generated bundles,
coverage output, and worktrees. The schema key for the language list in
Serena 1.2.0 is `languages`.

Profile prompts require Serena symbol navigation and `sg` structural queries
before broad text search.

## ast-grep structural gates

`sgconfig.yml` registers Python rules in `rules/python/` with executable
fixtures in `rule-tests/`:

- `no-blocking-requests-in-async-handler` — rejects blocking `requests` calls
  inside async Ops MCP handlers (scoped to `ops_mcp_server/`).
- `no-detached-asyncio-task` — rejects fire-and-forget `asyncio.create_task`
  expression statements in subscription code (scoped to `ops_mcp_server/`).
- `no-http-mcp-empty-command` — rejects empty `command`/`args` fields on
  dictionaries that define a `url` (HTTP MCP entries).

Run locally (ast-grep 0.44.1):

```bash
make sg-test   # validate every rule against its positive/negative fixtures
make sg-scan   # scan src/ and test/; exits 1 on any finding
```

CI runs both in the `Structural Gates (ast-grep)` job, pinned via
`@ast-grep/cli@0.44.1`. These gates supplement — never replace — pytest,
Black, isort, mypy, link, secret, and diff gates.

## Behavioral tests

- `test/test_swarm_repo_profiles.py` — profile discovery through the real
  `list_agent_profiles`/`load_agent_profile` path, protocol-skill separation,
  Python skill scoping, command+HTTP MCP translation, read-only lane tool
  resolution (including Kimi `--plan` command building), and fail-closed
  `${ENV}` URL preservation.
- `test/test_serena_project_config.py` — Serena config schema, read-only
  posture, and exclusion coverage.
- `test/test_ast_grep_gates.py` — sgconfig wiring, rule/fixture pairing,
  Makefile targets, CI pin, and live `sg test`/`sg scan` runs when the binary
  is on PATH.

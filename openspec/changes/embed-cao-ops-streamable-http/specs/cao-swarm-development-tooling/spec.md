## ADDED Requirements

### Requirement: Repository-owned CAO profiles
The repository MUST ship flat, discoverable, change-agnostic
`cli-agent-orchestrator` CAO profiles for supervision, Python and shell
implementation, Python/protocol testing, source-backed research, and
adversarial review and MUST register them through project
`agents.extra_dirs`.

#### Scenario: Profile discovery
- **WHEN** CAO loads project settings in this repository
- **THEN** every committed `.cao/agents` profile is discoverable without a user-global copy

#### Scenario: Protocol skill separation
- **WHEN** a supervisor or worker profile launches
- **THEN** supervisors receive `cao-supervisor-protocols` and workers receive `cao-worker-protocols`

#### Scenario: Highest validated provider profiles
- **WHEN** profiles are committed for Codex, Claude, Antigravity, or Kimi
- **THEN** they use the exact highest requested installed identifier for `gpt-5.6-sol` at maximum reasoning, `claude-opus-5` at provider-native Claude Code `xhigh`/ultracode effort, Gemini Pro High, or Kimi K3 respectively and fail closed instead of substituting or downgrading an unavailable model

#### Scenario: Claude Code effort surface
- **WHEN** the exact Claude profile's model and effort are tested
- **THEN** the test requires `canonicalModel=claude-opus-5` and the highest supported provider-native CLI effort (`xhigh`, surfaced as `/effort ultracode`) and rejects generic `max` from a separate SDK/tool/API ladder

#### Scenario: Current Python skill scope
- **WHEN** a repository profile launches
- **THEN** the current Artagon Python skill directory is registered through `skills.extra_dirs` and the profile advertises only its assigned type-safety, design, error-handling, resource, async, testing, style, and anti-pattern skills

#### Scenario: Stale review protocol excluded
- **WHEN** any repository profile's skill catalog is validated
- **THEN** it omits `review-verification-protocol`, and only the final OpenSpec traceability verifier receives `implementation-verification`

#### Scenario: Read-only lanes
- **WHEN** design, testing, or review profiles launch
- **THEN** they have no native write tools and Kimi/Antigravity enter their validated plan modes

#### Scenario: Implementation lane
- **WHEN** an implementation profile launches in an assigned worktree
- **THEN** its write and execution capabilities are limited to that worktree and owned subsystem

#### Scenario: Claude implementation orchestration
- **WHEN** a repository-local Claude implementation profile launches
- **THEN** it starts directly in the assigned worktree with `permissionMode: acceptEdits`, exposes `cao-mcp-server`, establishes project trust, and reaches its first authorized command without a trust prompt

### Requirement: Required MCP surfaces in profiles
Every applicable swarm profile MUST include the identity-bearing stdio
`cao-mcp-server` and native HTTP entries for Context7, Tavily, Gemini Search,
DuckDuckGo, and Serena using exact `${CAO_CONTEXT7_MCP_URL}`,
`${CAO_TAVILY_MCP_URL}`, `${CAO_GEMINI_SEARCH_MCP_URL}`,
`${CAO_DUCKDUCKGO_MCP_URL}`, and `${CAO_SERENA_MCP_URL}` references. HTTP
entries MUST NOT launch `npx`, embed credentials, contain subprocess fields, or
receive terminal environment.

#### Scenario: Command and HTTP coexistence
- **WHEN** a profile containing the command entry and all five HTTP entries is translated for a supported provider
- **THEN** `cao-mcp-server` launches as stdio while Context7, Tavily, Gemini Search, DuckDuckGo, and Serena remain native HTTP entries

#### Scenario: Missing managed endpoint environment
- **WHEN** any required managed MCP URL reference is unset at terminal launch
- **THEN** launch fails closed before starting the provider

#### Scenario: Native HTTP profile example
- **WHEN** a profile example is validated
- **THEN** each HTTP server is represented only by `type: http` and its exact `${CAO_*_MCP_URL}` reference

#### Scenario: Documentation and research prompt
- **WHEN** an agent receives its repository prompt
- **THEN** it is instructed to use Context7 before relying on library or CLI APIs, Tavily plus Gemini Search for current claims and corroboration, and DuckDuckGo as the general fallback

#### Scenario: Navigation prompt
- **WHEN** an agent receives its repository prompt
- **THEN** it is instructed to use Serena for symbol-aware navigation and `sg` for structural queries before broad text search

#### Scenario: Terminal-matching callback smoke
- **WHEN** a profile launch smoke creates a terminal and sends a callback through its command-launched `cao-mcp-server`
- **THEN** the callback `sender_id` equals the created terminal ID and no HTTP entry contains `CAO_TERMINAL_ID`

### Requirement: Read-only shared Serena configuration
The repository MUST provide `.serena/project.yml` with Python support,
generated/cache exclusions, and `read_only: true`.

#### Scenario: Shared navigation
- **WHEN** multiple swarm agents use the configured warm Serena endpoint
- **THEN** they can navigate the same project without Serena applying edits

#### Scenario: Generated content
- **WHEN** Serena indexes the project
- **THEN** virtual environments, caches, generated bundles, coverage output, and worktrees are excluded

### Requirement: ast-grep structural gates
The repository MUST configure ast-grep 0.44.1 rules and executable positive and
negative fixtures for the async and HTTP-profile invariants.

#### Scenario: Blocking async request
- **WHEN** an async CAO MCP handler contains a blocking `requests` call
- **THEN** `sg scan --error` fails

#### Scenario: Detached subscription task
- **WHEN** subscription code creates a task outside the session-owned abstraction
- **THEN** `sg scan --error` fails

#### Scenario: Empty HTTP command
- **WHEN** provider translation adds an empty command or args field to an HTTP MCP entry
- **THEN** `sg scan --error` fails

#### Scenario: Rule fixtures
- **WHEN** `sg test` runs
- **THEN** every prohibited fixture is detected and every allowed fixture remains clean

#### Scenario: CI pin
- **WHEN** CI runs the structural job
- **THEN** it installs ast-grep 0.44.1 and executes both rule tests and repository scan

### Requirement: Coordinated native CAO Ops rollout
`artagon-scripts` MUST support native and temporary proxy CAO Ops modes without
owning `cao-server`, and both repositories MUST carry cross-linked rollout and
rollback evidence.

#### Scenario: Native emit
- **WHEN** native mode is selected and config sync runs
- **THEN** clients receive canonical `http://127.0.0.1:9889/mcp/ops` without redirect and the proxy child configuration omits CAO Ops

#### Scenario: Proxy rollback
- **WHEN** `MCP_CAO_OPS_TRANSPORT=proxy` is selected and config sync reruns
- **THEN** the previous proxy-backed CAO Ops shape is restored

#### Scenario: Controller lifecycle
- **WHEN** `artagon-scripts` start or stop runs in native mode
- **THEN** it neither starts nor stops `cao-server` and does not terminate active CAO sessions

#### Scenario: Status and doctor
- **WHEN** native mode status or doctor runs
- **THEN** it probes the direct endpoint and reports missing or failed MCP readiness without claiming process ownership

#### Scenario: Ordered deployment
- **WHEN** the rollout switches clients to native mode
- **THEN** CAO has already deployed and a live initialize/list/call handshake has succeeded

#### Scenario: Cross-linked pull requests
- **WHEN** either repository PR is prepared
- **THEN** it links the OpenSpec change, verification evidence, detailed swarm findings, companion PR, deployment order, and rollback procedure

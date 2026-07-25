## ADDED Requirements

### Requirement: Repository-owned CAO profiles
The repository MUST ship flat, discoverable CAO profiles for supervision,
design, implementation, testing, and adversarial review and MUST register them
through project `agents.extra_dirs`.

#### Scenario: Profile discovery
- **WHEN** CAO loads project settings in this repository
- **THEN** every committed `.cao/agents` profile is discoverable without a user-global copy

#### Scenario: Protocol skill separation
- **WHEN** a supervisor or worker profile launches
- **THEN** supervisors receive `cao-supervisor-protocols` and workers receive `cao-worker-protocols`

#### Scenario: Python skill scope
- **WHEN** a repository profile launches
- **THEN** the Artagon Python skill directory is registered through `skills.extra_dirs` and the profile advertises only its assigned Python and verification skills

#### Scenario: Read-only lanes
- **WHEN** design, testing, or review profiles launch
- **THEN** they have no native write tools and Kimi/Antigravity enter their validated plan modes

#### Scenario: Implementation lane
- **WHEN** an implementation profile launches in an assigned worktree
- **THEN** its write and execution capabilities are limited to that worktree and owned subsystem

### Requirement: Required MCP surfaces in profiles
Every applicable swarm profile MUST include the identity-bearing stdio
`cao-mcp-server`, managed Context7 HTTP, and
`${CAO_SERENA_MCP_URL}` HTTP entry.

#### Scenario: Command and HTTP coexistence
- **WHEN** a profile containing all three MCP entries is translated for a supported provider
- **THEN** `cao-mcp-server` launches as stdio while Context7 and Serena remain native HTTP entries

#### Scenario: Missing Serena environment
- **WHEN** `CAO_SERENA_MCP_URL` is unset at terminal launch
- **THEN** launch fails closed before starting the provider

#### Scenario: Navigation prompt
- **WHEN** an agent receives its repository prompt
- **THEN** it is instructed to use Serena for symbol-aware navigation and `sg` for structural queries before broad text search

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

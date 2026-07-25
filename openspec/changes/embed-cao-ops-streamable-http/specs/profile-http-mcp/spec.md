## ADDED Requirements

### Requirement: Exclusive MCP server profile shapes
Each `mcpServers` entry MUST validate as exactly one command-launched stdio
entry or one HTTP entry.

#### Scenario: Valid command entry
- **WHEN** an entry has `command` and optional `args`, `env`, and timeout with no URL
- **THEN** profile validation accepts it as stdio, including the legacy omitted type

#### Scenario: Valid HTTP entry
- **WHEN** an entry has `type: http` and `url` with no subprocess fields
- **THEN** profile validation accepts it as HTTP

#### Scenario: Mixed entry
- **WHEN** an entry contains both HTTP and command/subprocess fields
- **THEN** validation fails with a sanitized field-level error

#### Scenario: Empty entry
- **WHEN** an entry contains neither a command nor an HTTP URL
- **THEN** validation fails rather than creating a synthetic command

### Requirement: Fail-closed HTTP URL resolution
An HTTP MCP URL MUST be a literal HTTP(S) URL or one exact `${ENV_NAME}`
reference resolved at terminal launch. The resolver MUST reject missing,
malformed, relative, userinfo-bearing, or secret-revealing values.

#### Scenario: Literal URL
- **WHEN** a profile contains a valid absolute `http://` or `https://` URL without userinfo
- **THEN** terminal launch passes the validated URL to the provider translator

#### Scenario: Exact environment reference
- **WHEN** the URL is exactly `${CAO_SERENA_MCP_URL}` and the variable contains a valid allowed URL
- **THEN** launch resolves it once and passes the URL without logging its value

#### Scenario: Reference survives profile loading
- **WHEN** a profile URL is one exact `${ENV_NAME}` reference
- **THEN** generic profile interpolation preserves the reference unchanged until terminal launch

#### Scenario: Launch environment source
- **WHEN** terminal launch resolves an exact URL reference
- **THEN** it uses one snapshot of the `cao-server` process environment and does not consult the managed legacy profile interpolation file

#### Scenario: Missing environment variable
- **WHEN** an exact environment reference is unset
- **THEN** launch fails closed and names only the profile server and missing variable

#### Scenario: Partial interpolation
- **WHEN** a URL includes an environment reference plus literal text or multiple references
- **THEN** validation fails without expanding or logging any value

#### Scenario: Userinfo or malformed URL
- **WHEN** a literal or resolved URL contains userinfo, a non-HTTP scheme, control characters, a fragment, or is not absolute
- **THEN** launch fails closed without including the resolved value in output

### Requirement: Provider-native HTTP translation
Codex, Claude, Antigravity, and Kimi MUST receive their native HTTP MCP
configuration without empty command fields, and command behavior MUST remain
compatible.

#### Scenario: Codex HTTP mapping
- **WHEN** Codex launches with an HTTP entry
- **THEN** it emits `mcp_servers.<name>.url`, may retain client-side `tool_timeout_sec`, and emits no command, args, env, or env_vars

#### Scenario: Claude HTTP mapping
- **WHEN** Claude launches with an HTTP entry
- **THEN** its MCP JSON contains `type: http` and `url` and no subprocess fields

#### Scenario: Antigravity HTTP mapping
- **WHEN** Antigravity launches with an HTTP entry
- **THEN** its `mcp_config.json` entry contains `url`, does not contain stale `httpUrl`, and contains no command, args, or env

#### Scenario: Kimi HTTP mapping
- **WHEN** Kimi 0.29 launches with an HTTP entry
- **THEN** its per-terminal `<unique-cwd>/.kimi-code/mcp.json` entry is exactly the supported `{url}` form and CAO does not pass `--mcp-config`

#### Scenario: Kimi documented configuration discovery
- **WHEN** the installed Kimi parser resolves MCP configuration
- **THEN** it recognizes `$KIMI_CODE_HOME/mcp.json` with default `~/.kimi-code/mcp.json`, project-root `.mcp.json`, and cwd `.kimi-code/mcp.json`, with later scopes overriding earlier scopes

#### Scenario: Kimi ordinary HTTP is not legacy SSE
- **WHEN** CAO serializes an ordinary Kimi HTTP entry
- **THEN** the entry contains no `transport`, and CAO does not emit the legacy `transport: sse` form

#### Scenario: Kimi model and prompt surfaces
- **WHEN** CAO selects a Kimi profile model or runs a bounded non-interactive prompt smoke
- **THEN** it uses the installed `-m` / `--model` or `-p` / `--prompt` surface respectively

#### Scenario: Kimi interactive task submission
- **WHEN** CAO delivers a task to a retained interactive Kimi TUI
- **THEN** it writes the task and submits it with `Enter` rather than assuming text injection alone executes the prompt

#### Scenario: HTTP entries have no terminal environment
- **WHEN** any supported provider translates an HTTP entry
- **THEN** the emitted entry contains no `env`, `env_vars`, or `CAO_TERMINAL_ID`

#### Scenario: Unsupported provider
- **WHEN** a provider without a native HTTP mapping receives an HTTP entry
- **THEN** launch fails clearly rather than coercing it to a command entry

### Requirement: Exact Claude Code model and provider-native effort
Claude Code profiles MUST pin exact `claude-opus-5` and MUST request the
highest effort supported by the provider-native Claude Code CLI. For the
2026-07-25 CLI surface, that effort is `xhigh`, exposed as
`/effort ultracode`; profiles and tests MUST NOT substitute generic `max` from
a separate SDK/tool/API effort ladder.

#### Scenario: Claude Code model and native effort
- **WHEN** a Claude Code profile or launch fixture is validated
- **THEN** it requires `canonicalModel=claude-opus-5` and provider-native `xhigh`/ultracode evidence without alias fallback or model downgrade

#### Scenario: Non-CLI effort ladder
- **WHEN** an SDK, tool, or API schema separately enumerates an effort value named `max`
- **THEN** validation retains the Claude Code CLI `xhigh`/ultracode contract instead of translating or escalating it to generic `max`

### Requirement: Fresh callback identity per created terminal
Each terminal launch MUST snapshot the newly created terminal ID and MUST
inject it only into each command-launched identity-bearing `cao-mcp-server`
entry. Launch MUST NOT reuse terminal identity from a profile, process
environment, provider cache, persisted provider configuration, prior session,
or another terminal.

#### Scenario: Identity-bearing command entry
- **WHEN** a new terminal with a command-launched `cao-mcp-server` entry is created
- **THEN** its resolved immutable launch copy contains `env.CAO_TERMINAL_ID` equal to that created terminal ID

#### Scenario: Stale identity sources
- **WHEN** profile, process, provider, persisted-config, or prior-session state contains a different `CAO_TERMINAL_ID`
- **THEN** launch discards every stale value and exposes only the newly created terminal ID to `cao-mcp-server`

#### Scenario: Unrelated command entry
- **WHEN** a command-launched MCP entry is not the identity-bearing `cao-mcp-server`
- **THEN** CAO does not synthesize `CAO_TERMINAL_ID` for that entry

#### Scenario: Consecutive terminal launches
- **WHEN** two terminals launch from the same profile or provider configuration
- **THEN** each receives an independent immutable identity snapshot and neither launch can observe the other's terminal ID

#### Scenario: Callback identity smoke
- **WHEN** a created terminal calls `cao-mcp-server` to send a launch-smoke callback
- **THEN** the received callback `sender_id` equals the terminal ID returned by creation

### Requirement: Native Antigravity permission modes
Antigravity MUST map `permissionMode: plan` and `permissionMode: acceptEdits`
to supported native `--mode` flags and MUST omit its bypass flag whenever an
explicit mode is selected.

#### Scenario: Plan mode
- **WHEN** an Antigravity profile selects `permissionMode: plan`
- **THEN** launch uses the validated native plan mode and omits the bypass flag

#### Scenario: Accept-edits mode
- **WHEN** an Antigravity profile selects `permissionMode: acceptEdits`
- **THEN** launch uses `--mode accept-edits` and omits `--dangerously-skip-permissions`

#### Scenario: Explicit mode unavailable
- **WHEN** the installed Antigravity CLI does not support the requested native mode
- **THEN** initialization fails instead of silently enabling bypass or downgrading the mode

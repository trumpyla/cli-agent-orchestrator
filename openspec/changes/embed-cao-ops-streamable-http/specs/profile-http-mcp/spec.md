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
- **THEN** its native configuration contains `httpUrl` and no command, args, or env

#### Scenario: Kimi HTTP mapping
- **WHEN** Kimi 0.29 launches with an HTTP entry
- **THEN** its per-terminal `.kimi-code/mcp.json` entry contains only the supported `url` form and CAO does not pass `--mcp-config`

#### Scenario: Terminal identity injection
- **WHEN** a profile contains command and HTTP entries
- **THEN** `CAO_TERMINAL_ID` is injected only into command-launched entries

#### Scenario: Unsupported provider
- **WHEN** a provider without a native HTTP mapping receives an HTTP entry
- **THEN** launch fails clearly rather than coercing it to a command entry

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

# Provider CLI source record — 2026-07-25

Retrieval date: **2026-07-25**

This note records the operator-authoritative sources used to correct Claude
Code, Kimi Code CLI, and Antigravity profile requirements before profile
implementation. Google DevKnowledge was unavailable because the configured
surface required authentication, so no claim below depends on it. Evidence
came from Context7 results for the official repositories, the official
published documentation, installed CLI help/version output, and CAO's
server-stamped smoke callback.

## Claude Code

Official repository and source URLs:

- https://github.com/anthropics/claude-code
- https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md

Context7 resolved the exact official repository as
`/anthropics/claude-code`. The official changelog retrieved on 2026-07-25
establishes that `/effort ultracode` is offered only on models supporting the
`xhigh` effort parameter. It also records that
`CLAUDE_CODE_EFFORT_LEVEL` overrides the `/effort` picker and agent
`effort:` frontmatter overrides the displayed baseline for that agent.

Implementation consequence: native Claude Code profiles pin exact
`claude-opus-5` and request the highest effort supported by the launched
provider-native CLI surface. For this surface that value is `xhigh`, exposed
as `/effort ultracode`; the separate five-value SDK/tool/API enumeration does
not make generic `max` a valid replacement. Model or native-effort
unavailability fails closed without alias substitution or downgrade.

Canonical smoke disposition from authoritative CAO root directive inbox 870:

```text
terminal: 21a9e101
canonicalModel: claude-opus-5
provider-native Claude Code effort: xhigh (/effort ultracode)
permissionMode: plan
callback sender_id: 21a9e101 (equal to the created terminal)
verdict: PASS
```

The server-stamped identity evidence is in callback inbox records 857 and 859;
inbox 870 reconciles the native CLI effort against the unrelated API/tool
ladder. The smoke was adjudicated from that existing evidence and was not
relaunched.

## Kimi Code CLI

Official repository and source URLs:

- https://github.com/moonshotai/kimi-code
- https://github.com/moonshotai/kimi-code/blob/main/docs/en/customization/mcp.md
- https://github.com/moonshotai/kimi-code/blob/main/packages/agent-core-v2/src/app/skillCatalog/builtin/mcp-config.md
- https://github.com/moonshotai/kimi-code/blob/main/docs/en/reference/kimi-command.md
- https://github.com/moonshotai/kimi-code/blob/main/apps/kimi-code/src/cli/commands.ts
- https://github.com/moonshotai/kimi-code/blob/main/docs/en/guides/interaction.md
- https://moonshotai.github.io/kimi-code/en/guides/interaction

Context7 resolved the exact official repository as
`/moonshotai/kimi-code`. The retrieved official sources establish:

- An ordinary remote HTTP MCP entry in `mcp.json` is exactly
  `{"url": "https://example.test/mcp"}`. It has no `type`, `transport`,
  `command`, `args`, or `env`.
- `{"transport": "sse", "url": "..."}` selects the legacy SSE transport.
  Legacy SSE is not the ordinary HTTP shape and is a non-goal for this change.
- MCP discovery reads `$KIMI_CODE_HOME/mcp.json` (default
  `~/.kimi-code/mcp.json`), project-root `.mcp.json`, and
  `<cwd>/.kimi-code/mcp.json`; later scopes override earlier scopes.
- `-m` / `--model` selects the model alias.
- `-p` / `--prompt` runs one prompt non-interactively.
- In the interactive TUI, `Enter` sends the current input;
  `Shift-Enter` / `Ctrl-J` inserts a newline.

Installed probes:

```text
$ kimi --version
0.29.0

$ kimi --help
-m, --model <model>    LLM model alias to use for this invocation
-p, --prompt <prompt>  Run one prompt non-interactively and print the response
```

Implementation consequence: CAO writes the isolated per-terminal Kimi MCP
configuration at `<unique-cwd>/.kimi-code/mcp.json`, one of the documented
discovery locations, and does not use the removed `--mcp-config` flag or mutate
the user-global or repository-shared files. Interactive task delivery writes
the prompt to the TUI and submits it with `Enter`; `-p` / `--prompt` remains the
bounded non-interactive surface for smoke probes.

## Antigravity CLI

Official repository and source URLs:

- https://github.com/google-antigravity/antigravity-cli
- https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md
- https://github.com/google-antigravity/antigravity-cli/blob/main/_autodocs/configuration.md
- https://github.com/google-antigravity/antigravity-cli/blob/main/_autodocs/commands-reference.md

Context7 resolved the exact official repository as
`/google-antigravity/antigravity-cli`. Its official changelog states that
`mcp_config.json` accepts `url` for a direct MCP server. Therefore the native
HTTP entry is `{"url": "https://example.test/mcp"}`; the previously drafted
`httpUrl` field is stale and MUST NOT be emitted.

Installed probes:

```text
$ agy --version
1.1.7

$ agy --help
--mode  Set the agent execution mode for this session (accept-edits, plan)
```

Implementation consequence: `permissionMode: acceptEdits` maps to
`--mode accept-edits`, `permissionMode: plan` maps to `--mode plan`, either
explicit mode suppresses `--dangerously-skip-permissions`, and an unavailable
requested mode fails closed.

## Managed profile surfaces inspected

The current `artagon-scripts/scripts/lib/mcp.sh` source was inspected on
2026-07-25. It exposes Context7, Tavily, Gemini Search, and DuckDuckGo through
the shared proxy's `/servers/<name>/mcp` Streamable HTTP routes and exposes
Serena through a project-scoped direct `http://127.0.0.1:<derived-port>/mcp`
URL. Repository profiles therefore use exact launch-resolved references rather
than literal ports or subprocess launchers:

```yaml
mcpServers:
  cao-mcp-server:
    command: cao-mcp-server
  context7:
    type: http
    url: ${CAO_CONTEXT7_MCP_URL}
  tavily:
    type: http
    url: ${CAO_TAVILY_MCP_URL}
  gemini-search:
    type: http
    url: ${CAO_GEMINI_SEARCH_MCP_URL}
  duckduckgo:
    type: http
    url: ${CAO_DUCKDUCKGO_MCP_URL}
  serena:
    type: http
    url: ${CAO_SERENA_MCP_URL}
```

The HTTP entries carry no `command`, `args`, `env`, credentials, or
`CAO_TERMINAL_ID`. Each newly created terminal instead receives one fresh
launch snapshot, and only its command-launched identity-bearing
`cao-mcp-server` entry receives `env.CAO_TERMINAL_ID=<created-terminal-id>`.

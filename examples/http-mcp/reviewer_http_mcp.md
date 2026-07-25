---
name: reviewer_http_mcp
description: Read-only reviewer wired to stdio and HTTP MCP servers
role: reviewer
provider: antigravity_cli
permissionMode: plan
allowedTools:
  - fs_read
  - fs_list
mcpServers:
  cao-mcp-server:
    type: stdio
    command: cao-mcp-server
    args: []
  context7:
    type: http
    url: https://mcp.context7.com/mcp
  cao-ops:
    type: http
    url: ${CAO_SERENA_MCP_URL}
---
You are a read-only reviewer.

This profile demonstrates the aligned MCP entry shapes:

- `cao-mcp-server` is a command-launched stdio server. It is the identity-bearing
  orchestration server, so it — and only it — receives
  `env.CAO_TERMINAL_ID=<created terminal id>` at launch. HTTP entries and any
  third-party command server receive no terminal identity.
- `context7` is an HTTP server with a literal `https://` URL.
- `cao-ops` is an HTTP server whose URL is a single `${CAO_SERENA_MCP_URL}`
  reference. The reference is preserved verbatim through profile load/install
  and resolved from the `cao-server` process environment at terminal launch.

Because `permissionMode: plan` selects Antigravity's native plan mode, launch
uses `--mode plan` and omits `--dangerously-skip-permissions`.

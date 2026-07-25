"""Provider-native translation of resolved HTTP MCP entries.

Each supported provider receives its exact native HTTP MCP shape with NO empty
subprocess fields. A provider without a native HTTP mapping fails closed rather
than coercing an HTTP entry into a synthetic command.

Command (stdio) entries keep their existing per-provider handling and are not
translated here.
"""

from __future__ import annotations

from typing import Any, Dict

from cli_agent_orchestrator.models.mcp_server import McpConfigError

# Provider identifiers (``ProviderType`` values) with a native HTTP MCP mapping.
HTTP_SUPPORTED_PROVIDERS = frozenset({"codex", "claude_code", "antigravity_cli", "kimi_cli"})


def claude_http_entry(url: str) -> Dict[str, str]:
    """Claude Code MCP JSON entry for an HTTP server."""
    return {"type": "http", "url": url}


def antigravity_http_entry(url: str) -> Dict[str, str]:
    """Antigravity (``agy``) mcp_config.json entry for an HTTP server.

    ``mcp_config.json`` accepts ``url`` for a direct MCP server. The previously
    drafted ``httpUrl`` field is stale and is never emitted: agy would ignore the
    unknown key and register a server with no endpoint.
    """
    return {"url": url}


def kimi_http_entry(url: str) -> Dict[str, str]:
    """Kimi 0.29 ``.kimi-code/mcp.json`` entry for an HTTP server."""
    return {"url": url}


def codex_http_fields(url: str) -> Dict[str, Any]:
    """Codex ``mcp_servers.<name>`` field map for an HTTP server.

    Codex retains its client-side ``tool_timeout_sec`` for HTTP servers but
    receives no command, args, env, or env_vars.
    """
    return {"url": url, "tool_timeout_sec": 600.0}


def render_http_entry(provider: str, url: str) -> Dict[str, Any]:
    """Return the native HTTP entry for ``provider`` or fail closed.

    Raises :class:`McpConfigError` for a provider with no native HTTP mapping so
    an HTTP entry is never silently coerced into a command entry.
    """
    if provider == "claude_code":
        return dict(claude_http_entry(url))
    if provider == "antigravity_cli":
        return dict(antigravity_http_entry(url))
    if provider == "kimi_cli":
        return dict(kimi_http_entry(url))
    if provider == "codex":
        return dict(codex_http_fields(url))
    raise McpConfigError(f"provider '{provider}' has no native HTTP MCP mapping")

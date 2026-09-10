"""Provider-native translation of resolved HTTP MCP entries.

Each supported provider receives its exact native HTTP MCP shape with NO empty
subprocess fields. A provider without a native HTTP mapping fails closed rather
than coercing an HTTP entry into a synthetic command.

Command (stdio) entries keep their existing per-provider handling and are not
translated here.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.models.mcp_server import McpConfigError

# Provider identifiers (``ProviderType`` values) with a native HTTP MCP mapping.
HTTP_SUPPORTED_PROVIDERS = frozenset(
    {"codex", "claude_code", "antigravity_cli", "kimi_cli", "grok_cli"}
)
LOCAL_TOKEN_ENV_VAR = "CAO_AUTH_LOCAL_TOKEN"
_managed_cao_origin: tuple[str, int] | None = None


def _normalize_host(host: str) -> str:
    """Normalize spelling without resolving DNS or inventing host aliases."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.lower()


def _client_origin(host: str, port: int) -> tuple[str, int]:
    host = _normalize_host(host)
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    return host, port


def set_managed_cao_origin(host: str, port: int) -> None:
    """Bind credential authority to the resolved CAO listener at server launch."""
    global _managed_cao_origin
    _managed_cao_origin = _client_origin(host, port)


def claude_http_entry(url: str) -> Dict[str, str]:
    """Claude Code MCP JSON entry for an HTTP server."""
    return {"type": "http", "url": url}


def antigravity_http_entry(url: str) -> Dict[str, str]:
    """Antigravity (``agy``) mcp_config.json entry for an HTTP server.

    ``serverUrl`` is the current documented field. Agy 1.1.7 also accepts
    ``url`` as a compatibility alias, but CAO emits the canonical field.
    ``httpUrl`` belongs to Gemini CLI and is never emitted here.
    """
    return {"serverUrl": url}


def kimi_http_entry(url: str) -> Dict[str, str]:
    """Kimi 0.29 ``.kimi-code/mcp.json`` entry for an HTTP server."""
    return {"url": url}


def codex_http_fields(url: str) -> Dict[str, Any]:
    """Codex ``mcp_servers.<name>`` field map for an HTTP server.

    Codex retains its client-side ``tool_timeout_sec`` for HTTP servers but
    receives no command, args, env, or env_vars.
    """
    return {"url": url, "tool_timeout_sec": 600.0}


def _auth_enabled(env: Mapping[str, str]) -> bool:
    """Mirror CAO's default-off auth switch against one launch snapshot."""
    return bool(env.get("AUTH0_DOMAIN", "").strip()) or bool(
        env.get("CAO_AUTH_JWKS_URI", "").strip()
    )


def _is_managed_cao_ops(url: str) -> bool:
    """Return whether ``url`` is the configured embedded CAO Ops mount.

    CLI host/port resolution takes precedence over configuration defaults.
    Profile/session environments cannot redirect this credential authority.
    """
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else 80
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if parsed.path != "/mcp/ops" or "?" in url or "#" in url or "@" in parsed.netloc:
        return False
    if parsed.hostname is None:
        return False
    origin = _managed_cao_origin or _client_origin(constants.SERVER_HOST, constants.SERVER_PORT)
    return (_normalize_host(parsed.hostname), port) == origin


def _local_ops_auth_fields(provider: str, url: str, env: Mapping[str, str]) -> Dict[str, Any]:
    """Return native auth fields for the embedded local endpoint.

    Codex and Kimi accept the *name* of a bearer-token environment variable.
    Claude expands ``${VAR}`` in HTTP headers. Antigravity 1.1.7 documents
    literal custom headers but no header environment expansion. Grok also uses
    native literal headers. Their per-launch generated configs receive the
    token value and are protected with mode 0600 by the providers.
    """
    if not _is_managed_cao_ops(url) or not _auth_enabled(env):
        return {}

    token = env.get(LOCAL_TOKEN_ENV_VAR, "").strip()
    if not token:
        raise McpConfigError("authenticated local CAO Ops MCP requires CAO_AUTH_LOCAL_TOKEN")

    if provider == "codex":
        return {"bearer_token_env_var": LOCAL_TOKEN_ENV_VAR}
    if provider == "claude_code":
        return {"headers": {"Authorization": f"Bearer ${{{LOCAL_TOKEN_ENV_VAR}}}"}}
    if provider in {"antigravity_cli", "grok_cli"}:
        return {"headers": {"Authorization": f"Bearer {token}"}}
    if provider == "kimi_cli":
        return {"bearerTokenEnvVar": LOCAL_TOKEN_ENV_VAR}
    raise McpConfigError(f"provider '{provider}' has no native HTTP MCP mapping")


def render_http_entry(
    provider: str,
    url: str,
    *,
    transport: str = "http",
    env: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Return the native HTTP entry for ``provider`` or fail closed.

    Preserve the requested transport. SSE has native mappings for Claude and
    Grok; other providers fail closed instead of silently selecting HTTP.
    """
    if transport == "sse" and provider in {"claude_code", "grok_cli"}:
        rendered: Dict[str, Any] = {"type": "sse", "url": url}
    elif transport != "http":
        raise McpConfigError(f"provider '{provider}' has no native {transport.upper()} MCP mapping")
    elif provider == "claude_code":
        rendered = dict(claude_http_entry(url))
    elif provider == "antigravity_cli":
        rendered = dict(antigravity_http_entry(url))
    elif provider == "kimi_cli":
        rendered = dict(kimi_http_entry(url))
    elif provider == "codex":
        rendered = dict(codex_http_fields(url))
    elif provider == "grok_cli":
        rendered = {"type": "http", "url": url}
    else:
        raise McpConfigError(f"provider '{provider}' has no native HTTP MCP mapping")
    rendered.update(_local_ops_auth_fields(provider, url, env or {}))
    return rendered

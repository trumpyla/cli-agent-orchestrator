"""Terminal-launch resolution of MCP HTTP URL references.

At launch, CAO snapshots the ``cao-server`` process environment exactly once,
resolves a literal HTTP(S) URL or one exact ``${ENV}`` reference, validates the
result fail-closed, and returns a new *immutable* resolved model. It never
mutates the loaded profile and never consults the managed legacy ``.env``
interpolation file — that source is for generic profile interpolation only.

Errors name only the server, the variable, and the violated rule; the reference
target and resolved value never appear in logs, traces, or exceptions.

The same launch snapshot also carries the *created terminal id*. Callback
identity is minted per launch and applied only to the identity-bearing
``cao-mcp-server`` command entry — see :func:`apply_terminal_identity`.
"""

from __future__ import annotations

import os
from pathlib import PurePath
from typing import Any, Dict, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict

from cli_agent_orchestrator.models.mcp_server import (
    HttpMcpServer,
    McpConfigError,
    StdioMcpServer,
    env_reference_name,
    parse_mcp_server_entry,
    validate_literal_http_url,
)
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    CAO_MCP_SERVER_MODULE,
)

#: The env var through which ``cao-mcp-server`` learns which terminal it serves.
TERMINAL_ID_ENV_VAR = "CAO_TERMINAL_ID"


class McpUrlResolutionError(McpConfigError):
    """Launch-time HTTP URL resolution failure.

    Names only the server, the variable, and the rule — never the value.
    """


class ResolvedHttpMcpServer(BaseModel):
    """An HTTP MCP entry whose ``url`` is a validated, literal absolute URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["http"] = "http"
    url: str


def snapshot_process_env() -> Dict[str, str]:
    """One point-in-time copy of the ``cao-server`` process environment."""
    return dict(os.environ)


def resolve_http_url(value: str, env: Mapping[str, str], *, server_name: str) -> str:
    """Resolve one HTTP url against ``env`` (a single environment snapshot).

    A literal URL is validated and returned. An exact ``${NAME}`` reference is
    looked up in ``env``, and the resolved value is validated as a literal
    absolute HTTP(S) URL. Every failure is fail-closed and secret-free.
    """
    ref = env_reference_name(value)
    if ref is not None:
        if ref not in env:
            raise McpUrlResolutionError(
                f"MCP server '{server_name}': environment variable '{ref}' is not set"
            )
        try:
            return validate_literal_http_url(env[ref])
        except McpConfigError as exc:
            raise McpUrlResolutionError(
                f"MCP server '{server_name}': environment variable '{ref}' is invalid ({exc})"
            ) from None
    try:
        return validate_literal_http_url(value)
    except McpConfigError as exc:
        raise McpUrlResolutionError(f"MCP server '{server_name}': {exc}") from None


def resolve_mcp_servers(
    mcp_servers: Optional[Mapping[str, object]],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Union[StdioMcpServer, ResolvedHttpMcpServer]]:
    """Resolve every ``mcpServers`` entry for one terminal launch.

    Takes a single environment snapshot (unless ``env`` is supplied for tests),
    narrows each entry to its strict variant, resolves HTTP references, and
    returns new immutable models. The input mapping is never mutated.
    """
    if not mcp_servers:
        return {}
    snapshot = dict(env) if env is not None else snapshot_process_env()
    resolved: Dict[str, Union[StdioMcpServer, ResolvedHttpMcpServer]] = {}
    for name, raw in mcp_servers.items():
        entry = parse_mcp_server_entry(raw, server_name=name)
        if isinstance(entry, HttpMcpServer):
            url = resolve_http_url(entry.url, snapshot, server_name=name)
            resolved[name] = ResolvedHttpMcpServer(url=url)
        else:
            resolved[name] = entry
    return resolved


def _basename(value: str) -> str:
    """Trailing path component of a command, with a Windows ``.exe`` stripped."""
    name = PurePath(value).name
    return name[:-4] if name.endswith(".exe") else name


def is_identity_bearing_command(
    command: Optional[str], args: Optional[Sequence[str]] = None
) -> bool:
    """True when this command launches the bundled orchestration MCP server.

    Only that server needs (and may receive) the terminal's callback identity.
    Every real launch shape counts, because they all end up running the same
    server: the bare console script, an absolute or ``uvx``/``npx``-wrapped
    invocation, and the ``-m cli_agent_orchestrator.mcp_server.server`` module
    entrypoint that :func:`resolve_cao_mcp_command` falls back to.

    Pure: inspects only the strings it is handed.
    """
    if command and _basename(command) == CAO_MCP_SERVER_COMMAND:
        return True
    for arg in args or ():
        if not isinstance(arg, str):
            continue
        if arg == CAO_MCP_SERVER_MODULE or _basename(arg) == CAO_MCP_SERVER_COMMAND:
            return True
    return False


def apply_terminal_identity(
    entry: Mapping[str, Any],
    *,
    terminal_id: str,
    identity_bearing: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return a copy of a command entry with this launch's callback identity.

    The created terminal's id is authoritative. For the identity-bearing
    ``cao-mcp-server`` entry the id is written unconditionally, replacing any
    value inherited from the profile, the ``cao-server`` process environment, a
    provider cache, a persisted provider config, or a prior session. For every
    other command entry no identity is synthesized *and* any pre-existing one is
    dropped — a third-party MCP server holding a terminal id could otherwise
    answer callbacks as that terminal.

    Pass ``identity_bearing`` when the entry's command has already been rewritten
    by :func:`~cli_agent_orchestrator.utils.mcp_resolution.resolve_cao_mcp_command`;
    it must then be computed from the *profile's declared* command so a rewrite
    cannot change whether identity is granted. Omit it to infer from ``entry``.

    Pure: never reads the environment. Unrelated ``env`` keys are preserved, and
    an entry that ends up with no ``env`` at all keeps the key absent rather than
    gaining an empty map.
    """
    result = dict(entry)
    env = {
        key: value for key, value in (result.get("env") or {}).items() if key != TERMINAL_ID_ENV_VAR
    }
    if identity_bearing is None:
        identity_bearing = is_identity_bearing_command(result.get("command"), result.get("args"))
    if identity_bearing:
        env[TERMINAL_ID_ENV_VAR] = terminal_id
    if env:
        result["env"] = env
    else:
        result.pop("env", None)
    return result

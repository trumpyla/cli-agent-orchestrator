"""CAO operations MCP server implementation."""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Annotated, Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import jwt
import requests  # type: ignore[import-untyped]
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from pydantic import AnyUrl, Field
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.ops_mcp_server.backend import (
    CLIENT_DEFAULT_TIMEOUT,
    AsyncRequestBackend,
    HttpxRequestBackend,
    RequestResult,
    RequestTimeout,
)
from cli_agent_orchestrator.ops_mcp_server.models import (
    InstallResult,
    LaunchResult,
    ProfileListResult,
    SendMessageResult,
    SessionListResult,
    TerminalControlResult,
)
from cli_agent_orchestrator.security.auth import (
    FULL_SCOPE_SET,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    extract_scopes_from_token,
    get_local_bearer,
    is_auth_enabled,
)
from cli_agent_orchestrator.utils.terminal import generate_session_name

JsonDict = Dict[str, Any]

_OPS_MCP_INSTRUCTIONS = """
    # CAO Operations MCP Server

    Manage CLI Agent Orchestrator profiles and sessions from outside a CAO session.
    Requires the CAO API server running at localhost:9889.

    ## Typical Workflow
    1. list_profiles to inspect available profiles
    2. get_profile_details to review a profile's full prompt and metadata
    3. install_profile to install a profile for a target provider
    4. launch_session to start a new CAO session
    5. send_session_message to deliver a prompt to a running terminal
    6. get_terminal_status to poll a worker until it finishes a task
    7. Use send_terminal_input/send_terminal_key only to operate an interactive
       prompt or picker; durable work prompts belong in send_session_message
    8. get_terminal_output to read a worker's result (or review its files/git diff)
    9. read_session_output to read a terminal's captured output by session name
    10. get_session_info or list_sessions to monitor overall progress
    11. shutdown_session to clean up when done

    ## Bi-directional bridge (conductor/worker -> driver)
    Register the driving CLI as a pane-less peer so a conductor (or any worker) can
    reply to it over CAO's own inbox, without polling files or terminal output:
    - register_peer(name?) -> an 8-hex peer_id. Pass it to a conductor in its launch
      task so the conductor and its workers know where to reply.
    - The conductor/worker replies with the existing send_message(receiver_id=peer_id,
      message=...); it queues in the peer's inbox (no pane, so it stays pending).
    - receive_messages(peer_id, wait_seconds=60, after_id=<last seen>) is the reliable
      pattern: it long-polls (pseudo-push) and returns the moment a newer reply arrives or
      the timeout expires — works on every MCP client. Then
      ack_messages(peer_id, message_ids) clears processed rows.
    MCP push is also enabled (resources/subscribe on cao://peers/<id>/inbox -> a body-free
    resources/updated wakeup), but long-poll remains authoritative because not every
    client handles notifications. Re-read the resource after a wake; resubscribe if its
    consumer terminates. CAO_AUTH_LOCAL_TOKEN is forwarded on every API request.
    """

_active_backend: ContextVar[AsyncRequestBackend | None] = ContextVar(
    "cao_ops_request_backend",
    default=None,
)


_CAO_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN})


class _CaoAuthenticationBackend(AuthenticationBackend):
    """Authenticate dynamically so CAO's default-off mode remains import-safe."""

    def __init__(self, verifier: "CaoTokenVerifier") -> None:
        self._verifier = verifier

    async def authenticate(
        self,
        conn: HTTPConnection,
    ) -> tuple[AuthCredentials, AuthenticatedUser] | None:
        if not is_auth_enabled():
            access = AccessToken(
                token="",
                client_id="cao-local-anonymous",
                scopes=list(FULL_SCOPE_SET),
                subject="cao-local-anonymous",
                claims={"iss": "cao:local"},
            )
            return AuthCredentials(access.scopes), AuthenticatedUser(access)

        authorization = conn.headers.get("authorization")
        if not authorization:
            return None
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            return None
        verified_access = await self._verifier.verify_token(token.strip())
        if verified_access is None:
            return None
        return AuthCredentials(verified_access.scopes), AuthenticatedUser(verified_access)


class _RequireAnyCaoScopeMiddleware:
    """Map valid-but-unscoped callers to a sanitized 403."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_auth_enabled():
            user = scope.get("user")
            if isinstance(user, AuthenticatedUser) and not _CAO_SCOPES.intersection(user.scopes):
                response = JSONResponse(
                    {"error": "insufficient_scope", "error_description": "CAO scope required"},
                    status_code=403,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer error="insufficient_scope", '
                            'error_description="CAO scope required"'
                        )
                    },
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class CaoTokenVerifier(TokenVerifier):
    """Validate CAO bearer tokens and expose stable session-principal identity."""

    def __init__(self) -> None:
        super().__init__(required_scopes=[])

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            scopes = extract_scopes_from_token(token)
            claims = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_exp": False,
                },
            )
            issuer = claims.get("iss")
            subject = claims.get("sub")
            client_id = claims.get("client_id") or claims.get("azp")
            if not isinstance(issuer, str) or not issuer:
                return None
            if not isinstance(subject, str) or not subject:
                return None
            if not isinstance(client_id, str) or not client_id:
                return None
            expires_at = claims.get("exp")
            if not isinstance(expires_at, int):
                expires_at = None
        except Exception:
            return None

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            subject=subject,
            claims={"iss": issuer},
        )

    def get_middleware(self) -> list[Middleware]:
        return [
            Middleware(
                AuthenticationMiddleware,
                backend=_CaoAuthenticationBackend(self),
                on_error=lambda _connection, _exc: JSONResponse(
                    {"error": "invalid_token", "error_description": "Authentication required"},
                    status_code=401,
                ),
            ),
            Middleware(AuthContextMiddleware),
            Middleware(_RequireAnyCaoScopeMiddleware),
        ]


def _response_detail(response: Any) -> str:
    """Extract the most useful error detail from an API response."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail:
            return detail

    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


def _auth_headers() -> Dict[str, str]:
    """Build the local bearer header for the operations-MCP to API hop."""
    token = get_local_bearer()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request_json(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    operation: str,
    timeout: Optional[float] = None,
) -> tuple[Optional[Any], Optional[str]]:
    """Execute an API request and return either JSON data or an error message.

    ``timeout`` is the ``requests`` read timeout in seconds (``None`` = no timeout,
    needed for a long-poll receive that holds the connection open server-side).
    """
    request_kwargs: Dict[str, Any] = {"params": params, "json": json}
    headers = _auth_headers()
    if headers:
        request_kwargs["headers"] = headers
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    try:
        response = requests.request(method, f"{API_BASE_URL}{path}", **request_kwargs)
    except requests.RequestException as exc:
        return None, f"{operation} failed: {exc}"

    if response.status_code >= 400:
        return None, f"{operation} failed: {_response_detail(response)}"

    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"{operation} failed: invalid JSON response ({exc})"


async def _async_request_json(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    operation: str,
    timeout: Optional[float] = None,
) -> tuple[Optional[Any], Optional[str]]:
    """Execute a cancellation-aware API request for long-polling MCP calls."""
    request_kwargs: Dict[str, Any] = {"params": params, "json": json}
    headers = _auth_headers()
    if headers:
        request_kwargs["headers"] = headers
    # httpx otherwise installs its own 5-second default. ``None`` intentionally
    # preserves the prior immediate-pull behavior (no client read timeout);
    # long-poll callers pass a bounded server-wait-plus-headroom value.
    request_kwargs["timeout"] = timeout
    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(method, f"{API_BASE_URL}{path}", **request_kwargs)
    except httpx.RequestError as exc:
        return None, f"{operation} failed: {exc}"

    if response.status_code >= 400:
        return None, f"{operation} failed: {_response_detail(response)}"

    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"{operation} failed: invalid JSON response ({exc})"


async def _request_from_active_backend(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    operation: str,
    timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
) -> RequestResult:
    """Dispatch through the factory backend, preserving direct-call compatibility."""
    backend = _active_backend.get()
    if backend is not None:
        return await backend.request_json(
            method,
            path,
            params=params,
            json=json,
            operation=operation,
            timeout=timeout,
        )

    # Direct imports of the historical helper functions remain compatible for
    # callers and focused unit tests. Factory-registered MCP handlers always
    # bind a backend before entering this function.
    if timeout is CLIENT_DEFAULT_TIMEOUT:
        return _request_json(
            method,
            path,
            params=params,
            json=json,
            operation=operation,
        )
    return await _async_request_json(
        method,
        path,
        params=params,
        json=json,
        operation=operation,
        timeout=timeout,
    )


def _serialize_allowed_tools(allowed_tools: Optional[List[str]]) -> Optional[str]:
    """Serialize allowed tools for the session creation API."""
    if not allowed_tools:
        return None
    return ",".join(allowed_tools)


async def _launch_session_impl(
    agent_profile: str,
    provider: Optional[str] = None,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
) -> LaunchResult:
    """Create a new CAO session and return the session identifiers."""
    resolved_session_name = session_name or generate_session_name()
    params: Dict[str, Any] = {
        "agent_profile": agent_profile,
        "session_name": resolved_session_name,
    }
    if provider is not None:
        params["provider"] = provider
    if working_directory:
        params["working_directory"] = working_directory

    serialized_allowed_tools = _serialize_allowed_tools(allowed_tools)
    if serialized_allowed_tools:
        params["allowed_tools"] = serialized_allowed_tools

    session_data, error = await _request_from_active_backend(
        "post", "/sessions", params=params, operation="Launch session"
    )
    if error:
        return LaunchResult(
            success=False,
            message=error,
            session_name=resolved_session_name,
            terminal_id=None,
        )

    if (
        not isinstance(session_data, dict)
        or "id" not in session_data
        or "session_name" not in session_data
    ):
        return LaunchResult(
            success=False,
            message="Launch session failed: invalid session response",
            session_name=resolved_session_name,
            terminal_id=None,
        )

    terminal_id = str(session_data["id"])
    canonical_session_name = str(session_data["session_name"])
    return LaunchResult(
        success=True,
        message=f"Session '{canonical_session_name}' launched successfully",
        session_name=canonical_session_name,
        terminal_id=terminal_id,
    )


async def list_profiles() -> ProfileListResult:
    """List available agent profiles.

    Scans built-in store, local store, and all configured provider agent
    directories. Profiles are deduplicated by name with source metadata.

    Returns:
        ProfileListResult with success status and profiles list
    """
    data, error = await _request_from_active_backend(
        "get", "/agents/profiles", operation="List profiles"
    )
    if error:
        return ProfileListResult(success=False, message=error)
    if isinstance(data, list):
        return ProfileListResult(success=True, profiles=data)
    return ProfileListResult(
        success=False,
        message="List profiles failed: invalid response payload",
    )


async def get_profile_details(
    name: Annotated[str, Field(description="The agent profile name to inspect")],
) -> JsonDict:
    """Get the full parsed content of a specific agent profile.

    Returns all AgentProfile fields (name, description, system_prompt, role,
    provider, allowedTools, mcpServers, model) with None-valued fields excluded.

    Args:
        name: Agent profile name to inspect

    Returns:
        Dict with profile fields, or {"success": False, "message": ...} on error
    """
    data, error = await _request_from_active_backend(
        "get",
        f"/agents/profiles/{name}",
        operation=f"Get profile details for '{name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get profile details failed: invalid response payload"}


async def install_profile(
    source: Annotated[str, Field(description="Agent name or https:// URL to install")],
    provider: Annotated[
        Optional[str],
        Field(
            description=(
                "Target provider for the installed profile. Omit to honour the "
                "profile's frontmatter provider, falling back to the default."
            )
        ),
    ] = None,
    env_vars: Annotated[
        Optional[Dict[str, str]],
        Field(description="Optional environment variables to inject before install"),
    ] = None,
) -> InstallResult:
    """Install an agent profile for a target provider.

    ## Source Resolution

    Remote callers (HTTP API / MCP) may install by either:
    1. https:// URL from an allow-listed host (``github.com``,
       ``raw.githubusercontent.com`` by default; extend via the
       ``CAO_PROFILE_ALLOWED_HOSTS`` env var on ``cao-server``).
    2. Profile name matching ``[A-Za-z0-9_-]{1,64}`` — looked up in the local
       store, provider dirs, then the built-in store.

    Installing by local filesystem path is CLI-only and is rejected from the
    HTTP API and this MCP tool.

    ## Provider Config

    - kiro_cli: JSON config written to the provider's agents directory
    - copilot_cli: frontmatter markdown written to the Copilot agents directory
    - claude_code, codex: context file only, no provider-specific config

    Args:
        source: Agent name or https:// URL from an allow-listed host
        provider: Target provider. Precedence: explicit value > the profile's
            frontmatter ``provider:`` key > the server default (kiro_cli)
        env_vars: Optional env vars written to the managed .env before install

    Returns:
        InstallResult with success status, file paths, and unresolved env vars
    """
    body: Dict[str, Any] = {"source": source}
    if provider is not None:
        body["provider"] = provider
    if env_vars:
        body["env_vars"] = env_vars

    data, error = await _request_from_active_backend(
        "post",
        "/agents/profiles/install",
        json=body,
        operation=f"Install profile '{source}'",
    )
    if error:
        return InstallResult(success=False, message=error)
    if isinstance(data, dict):
        return InstallResult(**data)
    return InstallResult(success=False, message="Install profile failed: invalid response payload")


async def launch_session(
    agent_profile: Annotated[str, Field(description="The agent profile to launch")],
    provider: Annotated[
        Optional[str],
        Field(description="The provider to use for the launched session"),
    ] = None,
    session_name: Annotated[
        Optional[str],
        Field(description="Optional custom CAO session name"),
    ] = None,
    working_directory: Annotated[
        Optional[str],
        Field(description="Optional working directory for the launched session"),
    ] = None,
    allowed_tools: Annotated[
        Optional[List[str]],
        Field(description="Optional list of allowed tool restrictions"),
    ] = None,
) -> LaunchResult:
    """Create a new CAO session with the given provider and agent profile.

    Returns immediately with session_name and terminal_id. Use
    send_session_message to deliver an initial prompt once the session is
    running, and get_session_info or list_sessions to monitor progress.

    Args:
        agent_profile: Agent profile for the new session
        provider: CLI provider (default: profile provider or kiro_cli)
        session_name: Optional custom session name (auto-generated if omitted)
        working_directory: Optional working directory for the session
        allowed_tools: Optional list of tool restrictions

    Returns:
        LaunchResult with success status, session_name, and terminal_id
    """
    return await _launch_session_impl(
        agent_profile=agent_profile,
        provider=provider,
        session_name=session_name,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
    )


async def send_session_message(
    terminal_id: Annotated[str, Field(description="The terminal ID to deliver the message to")],
    message: Annotated[str, Field(description="The message text to deliver")],
) -> SendMessageResult:
    """Queue a message for delivery to a running CAO terminal via the inbox service.

    Messages are delivered by the CAO inbox service when the terminal reaches
    IDLE or COMPLETED status. Use get_session_info to retrieve terminal IDs
    from an active session.

    Args:
        terminal_id: Target terminal ID (from launch_session or get_session_info)
        message: Message text to deliver

    Returns:
        SendMessageResult with success status and target terminal_id
    """
    _, error = await _request_from_active_backend(
        "post",
        f"/terminals/{terminal_id}/inbox/messages",
        params={"sender_id": "cao-ops-mcp", "message": message},
        operation=f"Send message to terminal '{terminal_id}'",
    )
    if error:
        return SendMessageResult(success=False, message=error, terminal_id=terminal_id)
    return SendMessageResult(
        success=True,
        message=f"Message queued for terminal '{terminal_id}'",
        terminal_id=terminal_id,
    )


async def send_terminal_input(
    terminal_id: Annotated[str, Field(description="The terminal ID to control")],
    message: Annotated[
        str,
        Field(
            description=(
                "Direct input for the terminal's active interactive prompt. "
                "Use send_session_message for durable work delivery."
            )
        ),
    ],
) -> TerminalControlResult:
    """Send direct operator input through CAO's authenticated HTTP API.

    This is intended for an active approval or selection prompt. It is not
    durable: callers should use ``send_session_message`` for agent work.
    """
    data, error = await _request_from_active_backend(
        "post",
        f"/terminals/{terminal_id}/input",
        params={"message": message},
        operation=f"Send input to terminal '{terminal_id}'",
    )
    if error:
        return TerminalControlResult(
            success=False,
            message=error,
            terminal_id=terminal_id,
        )
    if not isinstance(data, dict) or data.get("success") is not True:
        return TerminalControlResult(
            success=False,
            message="Send terminal input failed: invalid response payload",
            terminal_id=terminal_id,
        )
    return TerminalControlResult(
        success=True,
        message=f"Input sent to terminal '{terminal_id}'",
        terminal_id=terminal_id,
    )


async def send_terminal_key(
    terminal_id: Annotated[str, Field(description="The terminal ID to control")],
    key: Annotated[
        str,
        Field(
            description=(
                "Allowed interactive key name, such as Enter, Escape, Up, Down, "
                "Tab, or C-c. The HTTP API enforces the allowlist."
            )
        ),
    ],
) -> TerminalControlResult:
    """Send one allowlisted key to an interactive terminal prompt."""
    data, error = await _request_from_active_backend(
        "post",
        f"/terminals/{terminal_id}/key",
        params={"key": key},
        operation=f"Send key '{key}' to terminal '{terminal_id}'",
    )
    if error:
        return TerminalControlResult(
            success=False,
            message=error,
            terminal_id=terminal_id,
        )
    if not isinstance(data, dict) or data.get("success") is not True:
        return TerminalControlResult(
            success=False,
            message="Send terminal key failed: invalid response payload",
            terminal_id=terminal_id,
        )
    return TerminalControlResult(
        success=True,
        message=f"Key '{key}' sent to terminal '{terminal_id}'",
        terminal_id=terminal_id,
    )


def _read_session_output_impl(
    terminal_id: Optional[str],
    session_name: Optional[str],
    mode: Optional[str],
    max_chars: Optional[int],
) -> JsonDict:
    """Resolve a terminal and return its captured output (sync; mirrors other helpers)."""
    normalized = (mode or "full").lower()
    if normalized not in ("full", "last"):
        return {"success": False, "message": f"Invalid mode '{mode}'; expected 'full' or 'last'"}

    resolved_terminal_id = terminal_id
    if not resolved_terminal_id:
        if not session_name:
            return {"success": False, "message": "Provide either terminal_id or session_name"}
        info, error = _request_json(
            "get",
            f"/sessions/{session_name}",
            operation=f"Resolve terminals for session '{session_name}'",
        )
        if error:
            return {"success": False, "message": error}
        if not isinstance(info, dict):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid response payload",
            }
        terminals = info.get("terminals", [])
        if not isinstance(terminals, list) or any(
            not isinstance(terminal, dict) for terminal in terminals
        ):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid terminals payload",
            }
        if len(terminals) == 1:
            terminal = terminals[0]
            if not terminal.get("id"):
                return {
                    "success": False,
                    "message": f"Session '{session_name}' returned a terminal without an id",
                }
            resolved_terminal_id = str(terminal["id"])
        elif not terminals:
            return {"success": False, "message": f"Session '{session_name}' has no terminals"}
        else:
            return {
                "success": False,
                "message": (
                    f"Session '{session_name}' has {len(terminals)} terminals; "
                    "specify terminal_id"
                ),
                "terminals": terminals,
            }

    data, error = _request_json(
        "get",
        f"/terminals/{resolved_terminal_id}/output",
        params={"mode": normalized},
        operation=f"Read output for terminal '{resolved_terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if not isinstance(data, dict) or not isinstance(data.get("output"), str):
        return {"success": False, "message": "Read output failed: invalid response payload"}

    output = data["output"]
    total_chars = len(output)
    truncated = False
    if max_chars is not None and max_chars > 0 and total_chars > max_chars:
        output = output[-max_chars:]
        truncated = True

    return {
        "success": True,
        "terminal_id": resolved_terminal_id,
        "mode": normalized,
        "output": output,
        "truncated": truncated,
        "total_chars": total_chars,
    }


async def _read_session_output_async_impl(
    terminal_id: Optional[str],
    session_name: Optional[str],
    mode: Optional[str],
    max_chars: Optional[int],
) -> JsonDict:
    """Resolve a terminal and read output through the injected async backend."""
    normalized = (mode or "full").lower()
    if normalized not in ("full", "last"):
        return {"success": False, "message": f"Invalid mode '{mode}'; expected 'full' or 'last'"}

    resolved_terminal_id = terminal_id
    if not resolved_terminal_id:
        if not session_name:
            return {"success": False, "message": "Provide either terminal_id or session_name"}
        info, error = await _request_from_active_backend(
            "get",
            f"/sessions/{session_name}",
            operation=f"Resolve terminals for session '{session_name}'",
        )
        if error:
            return {"success": False, "message": error}
        if not isinstance(info, dict):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid response payload",
            }
        terminals = info.get("terminals", [])
        if not isinstance(terminals, list) or any(
            not isinstance(terminal, dict) for terminal in terminals
        ):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid terminals payload",
            }
        if len(terminals) == 1:
            terminal = terminals[0]
            if not terminal.get("id"):
                return {
                    "success": False,
                    "message": f"Session '{session_name}' returned a terminal without an id",
                }
            resolved_terminal_id = str(terminal["id"])
        elif not terminals:
            return {"success": False, "message": f"Session '{session_name}' has no terminals"}
        else:
            return {
                "success": False,
                "message": (
                    f"Session '{session_name}' has {len(terminals)} terminals; "
                    "specify terminal_id"
                ),
                "terminals": terminals,
            }

    data, error = await _request_from_active_backend(
        "get",
        f"/terminals/{resolved_terminal_id}/output",
        params={"mode": normalized},
        operation=f"Read output for terminal '{resolved_terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if not isinstance(data, dict) or not isinstance(data.get("output"), str):
        return {"success": False, "message": "Read output failed: invalid response payload"}

    output = data["output"]
    total_chars = len(output)
    truncated = False
    if max_chars is not None and max_chars > 0 and total_chars > max_chars:
        output = output[-max_chars:]
        truncated = True

    return {
        "success": True,
        "terminal_id": resolved_terminal_id,
        "mode": normalized,
        "output": output,
        "truncated": truncated,
        "total_chars": total_chars,
    }


async def read_session_output(
    terminal_id: Annotated[
        Optional[str],
        Field(
            description="Target terminal ID (from list_sessions / get_session_info). "
            "Primary key; either terminal_id or session_name is required."
        ),
    ] = None,
    session_name: Annotated[
        Optional[str],
        Field(
            description="CAO session name; convenience alternative to terminal_id. "
            "Resolved to a terminal when the session has exactly one; if it has more "
            "than one, the terminal list is returned and terminal_id is required."
        ),
    ] = None,
    mode: Annotated[
        str,
        Field(
            description="'full' (default) returns the raw rolling buffer: deterministic and "
            "best for scrollback/debugging. 'last' returns the provider-extracted final "
            "response: best for a completed worker's final message, but can be flaky on "
            "redraw-heavy TUIs."
        ),
    ] = "full",
    max_chars: Annotated[
        Optional[int],
        Field(
            description="Optional cap: return only the last max_chars of output "
            "(guards against flooding the caller's context). Truncation is flagged "
            "in the result. Values <= 0 are treated as no cap."
        ),
    ] = None,
) -> JsonDict:
    """Read a CAO terminal's captured scrollback with a deterministic full-buffer default.

    Defaults to mode='full' because raw rolling-buffer output is deterministic and
    best for scrollback/debugging. Use get_terminal_output, which defaults to
    mode='last', to read a completed worker's provider-extracted final message;
    'last' can be flaky on redraw-heavy TUIs. This tool adds session_name addressing
    (when the session has exactly one terminal) and max_chars tail-capping, which
    get_terminal_output does not provide.

    Args:
        terminal_id: Target terminal ID (primary key)
        session_name: Convenience alternative; resolved to a terminal when unambiguous
        mode: 'full' (default, rolling buffer) or 'last' (provider-extracted)
        max_chars: Optional tail cap on returned characters

    Returns:
        Dict {success, terminal_id, mode, output, truncated, total_chars}, or
        {success: False, message[, terminals]} on error / ambiguous session
    """
    if _active_backend.get() is None:
        return _read_session_output_impl(terminal_id, session_name, mode, max_chars)
    return await _read_session_output_async_impl(terminal_id, session_name, mode, max_chars)


async def get_terminal_status(
    terminal_id: Annotated[str, Field(description="The terminal ID to inspect")],
) -> JsonDict:
    """Get a single terminal's live status and metadata.

    Use this to poll a worker an external supervisor launched: it returns the
    current status (one of unknown / idle / processing / completed /
    waiting_user_answer / error) so the supervisor knows when a delegated task
    has finished before reading its output.

    Args:
        terminal_id: Target terminal ID (from launch_session or get_session_info)

    Returns:
        Dict with id, name, provider, session_name, agent_profile, status,
        last_active — or {"success": False, "message": ...} on error
    """
    data, error = await _request_from_active_backend(
        "get",
        f"/terminals/{terminal_id}",
        operation=f"Get terminal status for '{terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get terminal status failed: invalid response payload"}


async def get_terminal_output(
    terminal_id: Annotated[str, Field(description="The terminal ID to read output from")],
    mode: Annotated[
        str,
        Field(
            description=(
                "'last' (default) returns the provider-extracted final response: best for "
                "a completed worker's final message, but can be flaky on redraw-heavy "
                "TUIs. 'full' returns the raw rolling buffer: deterministic and best for "
                "scrollback/debugging."
            )
        ),
    ] = "last",
) -> JsonDict:
    """Read a worker terminal's output with a completed-message-oriented default.

    Defaults to mode='last' because this tool is optimized for reading a completed
    worker's provider-extracted final message, though redraw-heavy TUIs can make
    extraction flaky. For deterministic raw rolling-buffer scrollback/debugging,
    use read_session_output, which defaults to mode='full' and also supports
    session_name addressing and max_chars tail-capping. For code review, prefer
    inspecting the worker's files / git diff directly rather than relying solely
    on terminal text.

    Args:
        terminal_id: Target terminal ID
        mode: 'last' (final response, default) or 'full' (rolling buffer)

    Returns:
        Dict with output and mode, or {"success": False, "message": ...} on error
    """
    normalized = (mode or "last").lower()
    if normalized not in ("last", "full"):
        return {
            "success": False,
            "message": f"Get terminal output failed: mode must be 'last' or 'full', got '{mode}'",
        }
    data, error = await _request_from_active_backend(
        "get",
        f"/terminals/{terminal_id}/output",
        params={"mode": normalized},
        operation=f"Get terminal output for '{terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get terminal output failed: invalid response payload"}


async def list_sessions() -> SessionListResult:
    """List active CAO sessions with terminal counts and statuses.

    Returns:
        SessionListResult with success status and sessions list
    """
    data, error = await _request_from_active_backend("get", "/sessions", operation="List sessions")
    if error:
        return SessionListResult(success=False, message=error)
    if isinstance(data, list):
        return SessionListResult(success=True, sessions=data)
    return SessionListResult(
        success=False,
        message="List sessions failed: invalid response payload",
    )


async def get_session_info(
    session_name: Annotated[str, Field(description="The CAO session name to inspect")],
) -> JsonDict:
    """Get detailed session metadata including per-terminal status.

    Returns session fields along with a terminals array containing each
    terminal's status, provider, profile, and last activity.

    Args:
        session_name: CAO session name to inspect

    Returns:
        Dict with session fields, or {"success": False, "message": ...} on error
    """
    data, error = await _request_from_active_backend(
        "get",
        f"/sessions/{session_name}",
        operation=f"Get session info for '{session_name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get session info failed: invalid response payload"}


async def shutdown_session(
    session_name: Annotated[str, Field(description="The CAO session name to shut down")],
) -> JsonDict:
    """Cleanly shut down a CAO session.

    Exits all providers, kills the tmux session, and removes database records.

    Args:
        session_name: CAO session name to shut down

    Returns:
        Dict with success status and cleanup details, or failure dict on error
    """
    data, error = await _request_from_active_backend(
        "delete",
        f"/sessions/{session_name}",
        operation=f"Shutdown session '{session_name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Shutdown session failed: invalid response payload"}


async def register_peer(
    name: Annotated[Optional[str], Field(description="Optional human label for the peer")] = None,
) -> JsonDict:
    """Register the driving CLI as a pane-less peer inbox receiver.

    Returns an 8-hex ``peer_id``. Hand this id to a conductor (in its launch task) so it
    and its workers can reply with ``send_message(receiver_id=peer_id)``; pull those
    replies with ``receive_messages`` and clear them with ``ack_messages``. This is the
    driver end of the bi-directional bridge (design Decision 5: shipped pull lane).

    Returns:
        Dict with ``peer_id`` (8-hex), ``name``, ``mode`` — or a failure dict.
    """
    data, error = await _request_from_active_backend(
        "post", "/peers", json={"name": name}, operation="Register peer"
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Register peer failed: invalid response payload"}


async def receive_messages(
    peer_id: Annotated[str, Field(description="The 8-hex peer id from register_peer")],
    limit: Annotated[int, Field(description="Max messages to pull", ge=1, le=100)] = 10,
    wait_seconds: Annotated[
        float,
        Field(
            description="Long-poll: block server-side up to N seconds until a message arrives (0 = return immediately)",
            ge=0.0,
            le=120.0,
        ),
    ] = 0.0,
    after_id: Annotated[
        Optional[int],
        Field(
            description="Exclusive cursor: return only pending messages with id greater than this value",
            ge=0,
        ),
    ] = None,
) -> JsonDict:
    """Pull pending conductor/worker->driver messages for a peer (does NOT ack them).

    The shipped, client-agnostic lane. With ``wait_seconds>0`` this is **pseudo-push**:
    the server holds the call open until a message lands or the timeout, so a driver
    awaiting a reply gets it with push latency over an ordinary tool call (works on every
    MCP client). Pass the highest processed or observed id as ``after_id`` to wait for
    newer messages without first acknowledging older pending rows. Call ``ack_messages``
    with returned ids once processed, or they will be returned again to uncursored reads.

    Returns:
        Dict with ``messages`` (list) and ``count`` — or a failure dict.
    """
    params: Dict[str, Any] = {"status": "pending", "limit": limit}
    if after_id is not None:
        params["after_id"] = after_id
    request_timeout: Optional[float] = None
    if wait_seconds > 0:
        params["wait"] = wait_seconds
        request_timeout = wait_seconds + 5.0  # give the held request headroom over the server wait
    if _active_backend.get() is None:
        direct_options: Dict[str, Any] = {
            "params": params,
            "operation": f"Receive messages for peer '{peer_id}'",
        }
        if request_timeout is not None:
            direct_options["timeout"] = request_timeout
        data, error = await _async_request_json(
            "get",
            f"/terminals/{peer_id}/inbox/messages",
            **direct_options,
        )
    else:
        data, error = await _request_from_active_backend(
            "get",
            f"/terminals/{peer_id}/inbox/messages",
            params=params,
            operation=f"Receive messages for peer '{peer_id}'",
            timeout=request_timeout,
        )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, list):
        return {"success": True, "messages": data, "count": len(data)}
    return {"success": False, "message": "Receive messages failed: invalid response payload"}


async def ack_messages(
    peer_id: Annotated[str, Field(description="The 8-hex peer id")],
    message_ids: Annotated[List[int], Field(description="Message ids to mark delivered")],
) -> JsonDict:
    """Mark pending messages owned by a registered peer ``delivered``.

    The inbox GET does not ack on read; call this after processing the ids from
    ``receive_messages``. Unknown ids and ordinary terminals are rejected by the API;
    delivered, failed, and foreign-peer rows are never mutated.

    Returns:
        Dict with ``acked`` (count) — or a failure dict.
    """
    data, error = await _request_from_active_backend(
        "post",
        f"/terminals/{peer_id}/inbox/ack",
        json={"message_ids": message_ids},
        operation=f"Ack messages for peer '{peer_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Ack messages failed: invalid response payload"}


async def peer_inbox_resource(peer_id: str) -> str:
    """The peer's inbox as a readable MCP resource (subscribe for update notifications).

    Returns the peer's pending messages. A client that supports resource subscriptions
    is notified (``notifications/resources/updated``) when a new message arrives and can
    then re-read this resource; clients that do not can read it on demand or poll
    ``receive_messages``. This is the MCP-subscribe surface for the bi-directional bridge.
    """
    data, error = await _request_from_active_backend(
        "get",
        f"/terminals/{peer_id}/inbox/messages",
        params={"status": "pending", "limit": 100},
        operation=f"Read peer inbox '{peer_id}'",
        timeout=None,
    )
    if error:
        return json.dumps({"success": False, "message": error})
    return json.dumps({"peer_id": peer_id, "messages": data if isinstance(data, list) else []})


# --- MCP-subscribe lane for the peer inbox (bi-directional bridge) ---
# Advertise resources/subscribe and push notifications/resources/updated when a peer's
# inbox gets a new message. The mcp SDK hardcodes ResourcesCapability(subscribe=False),
# so we flip it via the low-level _mcp_server seam (the technique CAO uses in
# ext_apps/sep2133.py). Everything here is best-effort: an SDK/FastMCP build without the
# hooks is logged and skipped rather than crashing startup, and the poll lane
# (receive_messages) stays the client-agnostic fallback.

_PEER_URI_RE = re.compile(r"^cao://peers/([a-f0-9]{8})/inbox$")
_peer_consumers: Dict[Tuple[int, str], "asyncio.Task[None]"] = {}


def _peer_inbox_uri(peer_id: str) -> str:
    return f"cao://peers/{peer_id}/inbox"


async def _long_poll_inbox(peer_id: str, wait: float, after_id: int = 0) -> List[Any]:
    """Cancellation-aware long-poll of a peer inbox; return pending rows."""
    data, error = await _request_from_active_backend(
        "get",
        f"/terminals/{peer_id}/inbox/messages",
        params={
            "status": "pending",
            "limit": 100,
            "wait": wait,
            "after_id": after_id,
        },
        operation=f"Subscribe to peer inbox '{peer_id}'",
        timeout=wait + 5.0,
    )
    if error:
        raise RuntimeError(error)
    return data if isinstance(data, list) else []


async def _consume_inbox(peer_id: str, session: Any) -> None:
    """Long-poll a peer inbox and emit resources/updated when messages land.

    Reuses the same long-poll endpoint as ``receive_messages`` (no separate SSE stream —
    for a stdio child, re-calling a long-poll is simpler than reconnecting an SSE feed).
    HTTP is asynchronous so cancellation (on unsubscribe) closes the in-flight request
    instead of leaving a worker thread and connection alive. Best-effort with capped
    backoff. The resource read remains the source of truth — a missed wake is repaired
    by the next long-poll.
    """
    uri = AnyUrl(_peer_inbox_uri(peer_id))
    backoff = 1.0
    last_notified_id = 0
    while True:
        try:
            rows = await _long_poll_inbox(peer_id, 25.0, last_notified_id)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("peer-inbox: long-poll for %s failed; backing off", peer_id, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        newer_ids = [
            row["id"]
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), int)
            and row["id"] > last_notified_id
        ]
        if not newer_ids:
            # An endpoint/proxy that ignores the cursor must not create a tight loop.
            await asyncio.sleep(0.1)
            continue

        try:
            await session.send_resource_updated(uri)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "peer-inbox: send_resource_updated failed; consumer stopped (%s)",
                peer_id,
                exc_info=True,
            )
            return
        last_notified_id = max(newer_ids)


def _setup_peer_subscribe(
    server: FastMCP,
    backend: AsyncRequestBackend | None = None,
) -> None:
    """Advertise resources/subscribe and wire subscribe/unsubscribe to stream consumers.

    Mirrors ext_apps/sep2133.advertise_capability's low-level seam. No-op + logged if the
    FastMCP/SDK build lacks the hooks, so it never breaks startup or the poll lane.
    """
    low = getattr(server, "_mcp_server", None)
    if low is None:
        logger.warning("peer-inbox: no _mcp_server; MCP subscribe capability not advertised")
        return
    if getattr(low, "_cao_peer_subscribe_setup", False):
        return
    low._cao_peer_subscribe_setup = True

    # 1) Flip resources.subscribe=True in the initialize response.
    original_init = low.create_initialization_options

    def _patched_init(*args: Any, **kwargs: Any) -> Any:
        opts = original_init(*args, **kwargs)
        try:
            resources_cap = getattr(getattr(opts, "capabilities", None), "resources", None)
            if resources_cap is not None:
                resources_cap.subscribe = True
        except Exception:
            logger.debug("peer-inbox: could not advertise resources.subscribe", exc_info=True)
        return opts

    low.create_initialization_options = _patched_init

    # 2) subscribe/unsubscribe start/stop a per-peer stream consumer.
    try:

        @low.subscribe_resource()
        async def _on_subscribe(uri: Any) -> None:
            match = _PEER_URI_RE.match(str(uri))
            if not match:
                return
            peer_id = match.group(1)
            # Capture the live ServerSession from the LOW-LEVEL request context — not
            # server.get_context() (FastMCP's own context, which is not populated for a
            # handler registered directly on the low-level _mcp_server). With stdio there
            # is exactly one session, valid for the process lifetime.
            try:
                session = low.request_context.session
            except LookupError:
                logger.debug("peer-inbox: no request-context session on subscribe for %s", peer_id)
                return
            key = (id(session), peer_id)
            existing = _peer_consumers.get(key)
            if existing is not None and not existing.done():
                return
            if backend is None:
                task = asyncio.create_task(_consume_inbox(peer_id, session))
            else:
                token = _active_backend.set(backend)
                try:
                    task = asyncio.create_task(_consume_inbox(peer_id, session))
                finally:
                    _active_backend.reset(token)
            _peer_consumers[key] = task

            def _remove_completed(completed: "asyncio.Task[None]") -> None:
                if _peer_consumers.get(key) is completed:
                    _peer_consumers.pop(key, None)
                if completed.cancelled():
                    return
                try:
                    error = completed.exception()
                except Exception:
                    logger.debug("peer-inbox: could not inspect consumer task", exc_info=True)
                    return
                if error is not None:
                    logger.debug(
                        "peer-inbox: consumer task failed (%s)",
                        peer_id,
                        exc_info=(type(error), error, error.__traceback__),
                    )

            task.add_done_callback(_remove_completed)

        @low.unsubscribe_resource()
        async def _on_unsubscribe(uri: Any) -> None:
            match = _PEER_URI_RE.match(str(uri))
            if not match:
                return
            try:
                session = low.request_context.session
            except LookupError:
                logger.debug("peer-inbox: no request-context session on unsubscribe")
                return
            task = _peer_consumers.pop((id(session), match.group(1)), None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug(
                        "peer-inbox: failed consumer observed during unsubscribe",
                        exc_info=True,
                    )

    except Exception:
        logger.warning("peer-inbox: could not register MCP subscribe handlers", exc_info=True)


def _bind_backend(
    handler: Callable[..., Awaitable[Any]],
    backend: AsyncRequestBackend,
) -> Callable[..., Awaitable[Any]]:
    """Bind one registered handler to its factory-owned backend."""

    @wraps(handler)
    async def _bound(*args: Any, **kwargs: Any) -> Any:
        token = _active_backend.set(backend)
        try:
            return await handler(*args, **kwargs)
        finally:
            _active_backend.reset(token)

    return _bound


_TOOL_HANDLERS: Tuple[Callable[..., Awaitable[Any]], ...] = (
    list_profiles,
    get_profile_details,
    install_profile,
    launch_session,
    send_session_message,
    send_terminal_input,
    send_terminal_key,
    read_session_output,
    get_terminal_status,
    get_terminal_output,
    list_sessions,
    get_session_info,
    shutdown_session,
    register_peer,
    receive_messages,
    ack_messages,
)


def create_ops_mcp(
    backend: AsyncRequestBackend,
    *,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Create a CAO Ops MCP server backed by asynchronous REST requests."""

    @asynccontextmanager
    async def _lifespan(_server: FastMCP):
        start = getattr(backend, "start", None)
        if start is not None:
            await start()
        try:
            yield
        finally:
            await backend.aclose()

    server = FastMCP(
        "cao-ops-mcp",
        instructions=_OPS_MCP_INSTRUCTIONS,
        auth=auth,
        lifespan=_lifespan,
    )
    for handler in _TOOL_HANDLERS:
        server.tool()(_bind_backend(handler, backend))
    server.resource("cao://peers/{peer_id}/inbox")(_bind_backend(peer_inbox_resource, backend))
    _setup_peer_subscribe(server, backend)
    return server


def _local_authorization() -> str | None:
    token = get_local_bearer()
    return f"Bearer {token}" if token else None


class _StdioRequestBackend(HttpxRequestBackend):
    """Preserve direct-call compatibility outside the real stdio lifespan."""

    def __init__(self) -> None:
        super().__init__(
            base_url=API_BASE_URL,
            authorization=_local_authorization,
        )
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        operation: str,
        timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
    ) -> RequestResult:
        if self._running:
            return await super().request_json(
                method,
                path,
                params=params,
                json=json,
                operation=operation,
                timeout=timeout,
            )
        if timeout is CLIENT_DEFAULT_TIMEOUT or timeout is None:
            return _request_json(
                method,
                path,
                params=params,
                json=json,
                operation=operation,
            )
        return _request_json(
            method,
            path,
            params=params,
            json=json,
            operation=operation,
            timeout=timeout,
        )


mcp = create_ops_mcp(_StdioRequestBackend())


def main() -> None:
    """Run the operations MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

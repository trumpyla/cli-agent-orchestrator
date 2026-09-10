"""Strict functional-Pydantic boundary for ``mcpServers`` profile entries.

Each entry narrows to exactly one variant — a command-launched stdio entry or a
streamable-HTTP entry — *before* any provider translation or environment
resolution. Everything in this module is a pure function of its inputs:
validators perform no environment, filesystem, logging, or provider I/O. HTTP
URL references are validated for *shape* only here; they are resolved at
terminal launch (see :mod:`cli_agent_orchestrator.utils.mcp_launch`).

Errors are sanitized: an :class:`McpConfigError` names only the server, the
offending field, or the violated rule — never the URL, the reference target, or
a resolved secret value.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Union
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


class McpConfigError(ValueError):
    """Sanitized MCP profile validation error.

    The message names only the server, field, or rule. It never contains a URL,
    a reference target, or a resolved value.
    """


# Exactly one ``${NAME}`` reference occupying the entire string. A partial or
# repeated reference deliberately does NOT match — those must fail closed.
_ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_MARKER = "${"


def _has_control_chars(value: str) -> bool:
    """True if ``value`` contains an ASCII control character (C0 or DEL)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def env_reference_name(value: str) -> Optional[str]:
    """Return the variable name iff ``value`` is exactly one ``${NAME}``.

    Pure — does not read the environment. Returns ``None`` for literals and for
    any partial/multiple interpolation.
    """
    match = _ENV_REFERENCE_RE.fullmatch(value)
    return match.group(1) if match else None


def validate_literal_http_url(value: str) -> str:
    """Validate a literal, absolute ``http(s)`` URL. Pure; raises McpConfigError.

    Rejects non-strings, control characters, non-HTTP schemes, missing hosts,
    userinfo, fragments, and non-absolute/relative URLs. The error text names
    only the violated rule — never the value.
    """
    if not isinstance(value, str):
        raise McpConfigError("HTTP MCP url must be a string")
    if _has_control_chars(value):
        raise McpConfigError("HTTP MCP url must not contain control characters")
    if "#" in value:
        raise McpConfigError("HTTP MCP url must not contain a fragment")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise McpConfigError("HTTP MCP url must use the http or https scheme")
    if not parts.netloc or not parts.hostname:
        raise McpConfigError("HTTP MCP url must be absolute with a host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise McpConfigError("HTTP MCP url must not contain userinfo")
    return value


def validate_http_url_form(value: str) -> str:
    """Validate the *profile* form of an HTTP url. Pure; raises McpConfigError.

    Accepts either a literal ``http(s)`` URL or exactly one ``${ENV}`` reference.
    Rejects partial or multiple interpolation (a reference mixed with literal
    text or another reference). Does not resolve anything.
    """
    if not isinstance(value, str):
        raise McpConfigError("HTTP MCP url must be a string")
    if _has_control_chars(value):
        raise McpConfigError("HTTP MCP url must not contain control characters")
    if env_reference_name(value) is not None:
        return value
    if _ENV_MARKER in value:
        raise McpConfigError("HTTP MCP url must be a literal URL or exactly one ${ENV} reference")
    return validate_literal_http_url(value)


class StdioMcpServer(BaseModel):
    """A command-launched stdio MCP entry — the legacy shape.

    ``extra="allow"`` tolerates provider-specific keys (e.g. Codex ``env_vars``
    or ``tool_timeout_sec``) so existing profiles keep working, but a ``url`` is
    rejected: an entry carrying both a command and a url is ambiguous and must
    fail rather than silently launch.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    type: Optional[Literal["stdio"]] = None
    command: str = Field(min_length=1)
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    timeout: Optional[int] = None

    @model_validator(mode="after")
    def _reject_url(self) -> "StdioMcpServer":
        if self.model_extra and "url" in self.model_extra:
            raise ValueError("stdio MCP entry must not define 'url'")
        return self


class HttpMcpServer(BaseModel):
    """An HTTP-family MCP entry: ``type: http|sse`` plus ``url``.

    ``extra="forbid"`` rejects every subprocess field (command/args/env/timeout)
    and any other stray key, so a mixed entry fails closed. ``url`` is validated
    for shape only; it is resolved at launch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["http", "sse"]
    url: str

    @field_validator("url")
    @classmethod
    def _check_url_form(cls, value: str) -> str:
        return validate_http_url_form(value)


def _discriminator(value: Any) -> str:
    """Route an entry to its variant tag by the ``type`` field.

    ``type: http`` and the legacy ``type: sse`` both route to the HTTP variant.
    Every other value routes to stdio, where a missing ``command`` (empty entry)
    or a stray ``url`` (mixed entry) then fails validation.
    """
    if isinstance(value, Mapping):
        tag = value.get("type")
    else:
        tag = getattr(value, "type", None)
    return "http" if tag in ("http", "sse") else "stdio"


McpServerVariant = Annotated[
    Union[
        Annotated[StdioMcpServer, Tag("stdio")],
        Annotated[HttpMcpServer, Tag("http")],
    ],
    Discriminator(_discriminator),
]

MCP_SERVER_ADAPTER: TypeAdapter[Union[StdioMcpServer, HttpMcpServer]] = TypeAdapter(
    McpServerVariant
)


def _entry_as_dict(raw: Any) -> Dict[str, Any]:
    """Coerce a raw entry to a plain dict.

    Accepts a mapping or anything exposing ``model_dump`` — the providers already
    branch on exactly that duck type when serializing a profile entry, so this
    layer must narrow the same set of inputs they will go on to emit.
    """
    if isinstance(raw, BaseModel):
        return raw.model_dump(exclude_none=True)
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        dumped = dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise McpConfigError("MCP server entry must be an object")


def _sanitize(exc: ValidationError, server_name: str) -> McpConfigError:
    """Rebuild a validation failure as a sanitized, value-free error.

    Only the error ``loc`` (field name) and ``msg`` are surfaced — never the
    ``input`` value, which for a bad HTTP url would be the secret itself.
    """
    err = exc.errors()[0]
    loc = ".".join(str(part) for part in err.get("loc", ()))
    msg = err.get("msg", "invalid MCP server entry")
    detail = f"{loc}: {msg}" if loc else msg
    return McpConfigError(f"MCP server '{server_name}': {detail}")


def parse_mcp_server_entry(raw: Any, *, server_name: str) -> Union[StdioMcpServer, HttpMcpServer]:
    """Narrow one raw ``mcpServers`` entry to a strict variant.

    Pure and I/O-free. Raises :class:`McpConfigError` with a sanitized message
    for ambiguous, mixed, empty, or malformed entries.
    """
    try:
        data = _entry_as_dict(raw)
    except McpConfigError as exc:
        raise McpConfigError(f"MCP server '{server_name}': {exc}") from None
    try:
        return MCP_SERVER_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise _sanitize(exc, server_name) from None

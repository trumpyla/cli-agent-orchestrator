"""Contract matrix for the strict MCP-server profile boundary.

These tests pin the *pure* Pydantic normalization/validation layer that narrows
each ``mcpServers`` entry to exactly one variant — a command-launched stdio
entry or a streamable-HTTP entry — before any provider translation or
environment resolution happens. They intentionally exercise the public
``TypeAdapter`` and the sanitized parse wrapper directly.

Every case names the production break it guards and derives its expected
literals independently of the implementation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.mcp_server import (
    MCP_SERVER_ADAPTER,
    HttpMcpServer,
    McpConfigError,
    StdioMcpServer,
    env_reference_name,
    parse_mcp_server_entry,
    validate_http_url_form,
    validate_literal_http_url,
)

# A recognizable secret that must never appear in any error surface.
_SECRET_HOST = "serena-internal.example"
_SECRET_TOKEN = "tok3n-sup3r-s3cret"


class TestVariantNarrowing:
    """parse_mcp_server_entry must pick exactly one strict variant."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param({"command": "cao-mcp-server"}, id="command-only-omitted-type"),
            pytest.param(
                {"type": "stdio", "command": "cao-mcp-server", "args": []},
                id="explicit-stdio-type-empty-args",
            ),
            pytest.param(
                {"command": "srv", "args": ["--x"], "env": {"A": "B"}, "timeout": 5000},
                id="full-legacy-command-entry",
            ),
            pytest.param(
                {"command": "codex-srv", "env_vars": ["CAO_TERMINAL_ID"]},
                id="stdio-tolerates-provider-extra-keys",
            ),
        ],
    )
    def test_command_entries_narrow_to_stdio(self, raw: dict) -> None:
        # Break guarded: legacy command entries (the only historical shape) must
        # keep validating as stdio, including the omitted-type compatibility case
        # and provider-specific extras like Codex ``env_vars``.
        entry = parse_mcp_server_entry(raw, server_name="cao")
        assert isinstance(entry, StdioMcpServer)
        assert entry.command == raw["command"]

    @pytest.mark.parametrize(
        ("transport", "url"),
        [
            pytest.param(
                "http",
                "http://127.0.0.1:9889/mcp/ops",
                id="literal-loopback-http",
            ),
            pytest.param("http", "https://serena.example/mcp", id="literal-https"),
            pytest.param("http", "${CAO_SERENA_MCP_URL}", id="single-env-reference"),
            pytest.param("sse", "https://serena.example/sse", id="legacy-sse"),
        ],
    )
    def test_http_entries_narrow_to_http(self, transport: str, url: str) -> None:
        # Break guarded: an HTTP or legacy SSE URL must narrow to the HTTP variant
        # and keep the url verbatim (references are NOT resolved at this layer).
        entry = parse_mcp_server_entry({"type": transport, "url": url}, server_name="cao-ops")
        assert isinstance(entry, HttpMcpServer)
        assert entry.type == transport
        assert entry.url == url

    def test_type_adapter_is_the_public_boundary(self) -> None:
        # Break guarded: the union must be reachable through the exported
        # TypeAdapter so callers can validate without the wrapper.
        stdio = MCP_SERVER_ADAPTER.validate_python({"command": "srv"})
        http = MCP_SERVER_ADAPTER.validate_python({"type": "http", "url": "https://h/x"})
        assert isinstance(stdio, StdioMcpServer)
        assert isinstance(http, HttpMcpServer)


class TestLegacyRoundTrip:
    """A stdio entry dumped back out must match its minimal legacy shape."""

    def test_stdio_round_trip_excludes_unset(self) -> None:
        raw = {"command": "cao-mcp-server", "args": ["--flag"], "env": {"K": "V"}}
        entry = parse_mcp_server_entry(raw, server_name="cao")
        assert isinstance(entry, StdioMcpServer)
        # exclude_none drops the omitted ``type``/``timeout`` so the entry is
        # byte-for-byte the legacy dict the providers already emit.
        assert entry.model_dump(exclude_none=True) == raw


class TestInvalidClasses:
    """Every ambiguous / malformed shape must fail closed (no synthetic launch)."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(
                {"type": "http", "url": "https://h/x", "command": "srv"},
                id="mixed-http-plus-command",
            ),
            pytest.param(
                {"type": "http", "url": "https://h/x", "args": ["--x"]},
                id="mixed-http-plus-args",
            ),
            pytest.param(
                {"type": "http", "url": "https://h/x", "env": {"A": "B"}},
                id="mixed-http-plus-env",
            ),
            pytest.param({"url": "https://h/x", "command": "srv"}, id="mixed-url-plus-command"),
            pytest.param({}, id="empty-entry"),
            pytest.param({"type": "stdio"}, id="stdio-without-command"),
            pytest.param({"url": "https://h/x"}, id="bare-url-without-http-type"),
            pytest.param({"type": "http"}, id="http-without-url"),
            pytest.param({"command": "srv", "url": "https://h/x"}, id="command-plus-url"),
        ],
    )
    def test_invalid_entries_raise_sanitized(self, raw: dict) -> None:
        # Break guarded: strictness — an ambiguous, mixed, or empty entry must
        # raise instead of being coerced into a command that silently no-ops.
        with pytest.raises(McpConfigError) as exc_info:
            parse_mcp_server_entry(raw, server_name="cao")
        # The server name is always safe to surface; it aids operators.
        assert "cao" in str(exc_info.value)

    def test_empty_entry_is_not_turned_into_a_command(self) -> None:
        # Break guarded: the exact anti-pattern the spec forbids — an empty
        # entry must never validate as a command with an empty command string.
        with pytest.raises(McpConfigError):
            parse_mcp_server_entry({}, server_name="cao")


class TestUrlFormValidation:
    """validate_http_url_form: literal http(s) OR exactly one ${ENV} reference."""

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("http://127.0.0.1:9889/mcp/ops", id="loopback"),
            pytest.param("https://h.example/mcp?x=1", id="query-string-allowed"),
            pytest.param("${CAO_SERENA_MCP_URL}", id="exact-reference"),
        ],
    )
    def test_accepted_forms(self, url: str) -> None:
        assert validate_http_url_form(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("ftp://h/x", id="non-http-scheme-ftp"),
            pytest.param("file:///etc/passwd", id="non-http-scheme-file"),
            pytest.param("/mcp/ops", id="relative-url"),
            pytest.param("http://h/x#frag", id="fragment"),
            pytest.param("http://h/x#", id="empty-fragment"),
            pytest.param("https://h/${TOKEN}", id="partial-interpolation-suffix"),
            pytest.param("${A}${B}", id="multiple-references"),
            pytest.param("pre-${A}", id="reference-with-literal-prefix"),
            pytest.param("http://h/\x01", id="control-char"),
            pytest.param("http:///nohost", id="missing-host"),
        ],
    )
    def test_rejected_forms(self, url: str) -> None:
        with pytest.raises(McpConfigError):
            validate_http_url_form(url)

    @pytest.mark.parametrize(
        "value,expected",
        [
            pytest.param("${CAO_SERENA_MCP_URL}", "CAO_SERENA_MCP_URL", id="exact"),
            pytest.param("${_underscore1}", "_underscore1", id="leading-underscore"),
            pytest.param("http://h/x", None, id="literal-not-a-reference"),
            pytest.param("pre-${A}", None, id="partial-not-a-reference"),
            pytest.param("${A}${B}", None, id="double-not-a-reference"),
            pytest.param("${1bad}", None, id="invalid-identifier"),
        ],
    )
    def test_env_reference_name(self, value: str, expected: str | None) -> None:
        assert env_reference_name(value) == expected


class TestSecretFreeErrors:
    """No sensitive URL material may leak into error text."""

    def test_userinfo_error_is_secret_free(self) -> None:
        url = f"https://user:{_SECRET_TOKEN}@{_SECRET_HOST}/mcp"
        with pytest.raises(McpConfigError) as exc_info:
            validate_literal_http_url(url)
        text = str(exc_info.value)
        assert _SECRET_TOKEN not in text
        assert _SECRET_HOST not in text
        assert "userinfo" in text

    def test_parse_error_never_echoes_the_url(self) -> None:
        raw = {"type": "http", "url": f"ftp://{_SECRET_HOST}/{_SECRET_TOKEN}"}
        with pytest.raises(McpConfigError) as exc_info:
            parse_mcp_server_entry(raw, server_name="cao-ops")
        text = str(exc_info.value)
        assert _SECRET_HOST not in text
        assert _SECRET_TOKEN not in text


class TestImmutability:
    """Narrowed models are frozen so a translator cannot mutate them in place."""

    def test_stdio_is_frozen(self) -> None:
        entry = parse_mcp_server_entry({"command": "srv"}, server_name="cao")
        with pytest.raises(ValidationError):
            entry.command = "other"  # type: ignore[misc]

    def test_http_is_frozen(self) -> None:
        entry = parse_mcp_server_entry(
            {"type": "http", "url": "https://h/x"}, server_name="cao-ops"
        )
        with pytest.raises(ValidationError):
            entry.url = "https://h/y"  # type: ignore[misc]

"""Terminal-launch HTTP URL resolution boundary.

Resolution happens exactly once per launch: CAO snapshots the ``cao-server``
process environment a single time, resolves a literal HTTP(S) URL or one exact
``${ENV}`` reference, validates it fail-closed, and returns a new immutable
resolved model. It must never consult the managed legacy ``.env`` interpolation
file and never mutate the loaded profile. Errors name only the server, the
variable, and the rule — never the secret value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.mcp_server import McpConfigError
from cli_agent_orchestrator.utils.mcp_launch import (
    McpUrlResolutionError,
    ResolvedHttpMcpServer,
    resolve_http_url,
    resolve_mcp_servers,
    snapshot_process_env,
)

_GOOD_URL = "http://127.0.0.1:9889/mcp/ops"
_SECRET_URL = "https://s3cr3t-host.internal/mcp"


class TestResolveHttpUrl:
    def test_literal_url_passes_through(self) -> None:
        assert resolve_http_url(_GOOD_URL, {}, server_name="cao-ops") == _GOOD_URL

    def test_reference_resolves_from_snapshot(self) -> None:
        env = {"CAO_SERENA_MCP_URL": "https://serena.example/mcp"}
        assert (
            resolve_http_url("${CAO_SERENA_MCP_URL}", env, server_name="serena")
            == "https://serena.example/mcp"
        )

    def test_missing_variable_fails_closed_naming_only_server_and_var(self) -> None:
        # Break guarded: an unset reference must fail closed and disclose only
        # the server and variable name — enough to fix, nothing more.
        with pytest.raises(McpUrlResolutionError) as exc_info:
            resolve_http_url("${CAO_SERENA_MCP_URL}", {}, server_name="serena")
        text = str(exc_info.value)
        assert "serena" in text
        assert "CAO_SERENA_MCP_URL" in text

    @pytest.mark.parametrize(
        "resolved",
        [
            pytest.param(f"https://user:pw@{_SECRET_URL.split('//')[1]}", id="userinfo"),
            pytest.param("ftp://host/x", id="non-http-scheme"),
            pytest.param("/relative/path", id="relative"),
            pytest.param("http://host/x#frag", id="fragment"),
            pytest.param("http://host/\x01", id="control-char"),
        ],
    )
    def test_resolved_value_validated_fail_closed(self, resolved: str) -> None:
        # Break guarded: a variable that expands to a malformed/hostile URL must
        # still be rejected at launch — resolution does not trust env contents.
        with pytest.raises(McpUrlResolutionError):
            resolve_http_url("${X}", {"X": resolved}, server_name="cao-ops")

    def test_resolved_value_never_leaks_into_error(self) -> None:
        # Break guarded: the secret value of the variable must not appear in the
        # error even when the value itself is what failed validation.
        with pytest.raises(McpUrlResolutionError) as exc_info:
            resolve_http_url("${X}", {"X": f"ftp://{_SECRET_URL}"}, server_name="cao-ops")
        assert _SECRET_URL not in str(exc_info.value)


class TestSingleSnapshot:
    def test_single_environment_snapshot_per_launch(self, monkeypatch) -> None:
        # Break guarded: reading os.environ per-entry would let a mid-launch env
        # change split a terminal's view. Resolution must snapshot exactly once.
        calls = {"n": 0}

        def _counting_snapshot() -> dict[str, str]:
            calls["n"] += 1
            return {"A": "https://a.example/mcp", "B": "https://b.example/mcp"}

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.mcp_launch.snapshot_process_env", _counting_snapshot
        )
        servers = {
            "one": {"type": "http", "url": "${A}"},
            "two": {"type": "http", "url": "${B}"},
        }
        resolved = resolve_mcp_servers(servers)
        assert calls["n"] == 1
        assert resolved["one"].url == "https://a.example/mcp"  # type: ignore[union-attr]
        assert resolved["two"].url == "https://b.example/mcp"  # type: ignore[union-attr]

    def test_snapshot_reflects_process_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("CAO_LAUNCH_PROBE_VAR", "sentinel-value")
        assert snapshot_process_env()["CAO_LAUNCH_PROBE_VAR"] == "sentinel-value"


class TestEnvironmentSource:
    def test_resolves_from_process_env_not_managed_dotenv(self, monkeypatch) -> None:
        # Break guarded: launch resolution must use the process environment, not
        # the managed legacy ``.env`` interpolation file. Prove ``load_env_vars``
        # is never consulted during resolution.
        sentinel = MagicMock(side_effect=AssertionError("managed .env must not be read"))
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.load_env_vars", sentinel)
        monkeypatch.setenv("CAO_SERENA_MCP_URL", "https://serena.example/mcp")
        resolved = resolve_mcp_servers({"serena": {"type": "http", "url": "${CAO_SERENA_MCP_URL}"}})
        assert resolved["serena"].url == "https://serena.example/mcp"  # type: ignore[union-attr]
        sentinel.assert_not_called()


class TestImmutableResolvedCopy:
    def test_resolved_http_is_frozen(self) -> None:
        resolved = resolve_mcp_servers(
            {"cao-ops": {"type": "http", "url": "${X}"}}, env={"X": _GOOD_URL}
        )
        entry = resolved["cao-ops"]
        assert isinstance(entry, ResolvedHttpMcpServer)
        assert entry.url == _GOOD_URL
        with pytest.raises(ValidationError):
            entry.url = "http://other/x"  # type: ignore[misc]

    def test_input_profile_is_not_mutated(self) -> None:
        # Break guarded: resolution must return a NEW model, leaving the loaded
        # profile's reference intact so a later launch re-resolves cleanly.
        servers = {"cao-ops": {"type": "http", "url": "${X}"}}
        resolve_mcp_servers(servers, env={"X": _GOOD_URL})
        assert servers["cao-ops"]["url"] == "${X}"

    def test_stdio_entries_resolve_to_frozen_models(self) -> None:
        resolved = resolve_mcp_servers({"cao": {"command": "cao-mcp-server", "args": []}})
        entry = resolved["cao"]
        with pytest.raises(ValidationError):
            entry.command = "x"  # type: ignore[union-attr]


class TestMixedAndEmptyRejectionAtLaunch:
    @pytest.mark.parametrize(
        "servers",
        [
            pytest.param({"bad": {}}, id="empty"),
            pytest.param(
                {"bad": {"type": "http", "url": "https://h/x", "command": "srv"}}, id="mixed"
            ),
            pytest.param({"bad": {"url": "https://h/x"}}, id="bare-url"),
        ],
    )
    def test_launch_rejects_invalid_entries(self, servers: dict) -> None:
        with pytest.raises(McpConfigError):
            resolve_mcp_servers(servers)

    def test_none_and_empty_mapping_resolve_to_empty(self) -> None:
        assert resolve_mcp_servers(None) == {}
        assert resolve_mcp_servers({}) == {}

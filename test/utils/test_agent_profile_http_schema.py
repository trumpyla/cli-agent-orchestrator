"""Schema alignment + reference preservation across profile load/install.

The agent-profile JSON schema must accept both an exclusive command (stdio)
entry and an HTTP entry, and reject mixed/empty entries — matching the runtime
Pydantic boundary. Separately, an exact ``${ENV}`` HTTP reference must survive
generic profile interpolation on load (which draws only from the managed ``.env``
file) so it is resolved from the process environment at launch instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_launch import resolve_mcp_servers

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli_agent_orchestrator"
    / "schemas"
    / "agent_profile.schema.json"
)


def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _profile_with_mcp(entry: dict) -> dict:
    return {"name": "p", "description": "d", "mcpServers": {"srv": entry}}


class TestSchemaAcceptsExclusiveShapes:
    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param({"command": "cao-mcp-server", "args": []}, id="legacy-command"),
            pytest.param({"type": "stdio", "command": "srv"}, id="explicit-stdio"),
            pytest.param(
                {"type": "http", "url": "http://127.0.0.1:9889/mcp/ops"}, id="http-literal"
            ),
            pytest.param({"type": "http", "url": "${CAO_SERENA_MCP_URL}"}, id="http-reference"),
            pytest.param({"type": "sse", "url": "https://serena.example/sse"}, id="sse-literal"),
        ],
    )
    def test_valid_entries_pass_schema(self, entry: dict) -> None:
        errors = list(_validator().iter_errors(_profile_with_mcp(entry)))
        assert errors == [], errors

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param({}, id="empty"),
            pytest.param({"type": "http", "url": "https://h/x", "command": "srv"}, id="mixed"),
            pytest.param({"command": "srv", "url": "https://h/x"}, id="command-plus-url"),
            pytest.param({"type": "http"}, id="http-without-url"),
            pytest.param({"url": "https://h/x"}, id="bare-url"),
        ],
    )
    def test_invalid_entries_fail_schema(self, entry: dict) -> None:
        errors = list(_validator().iter_errors(_profile_with_mcp(entry)))
        assert errors, f"schema unexpectedly accepted {entry!r}"


class TestReferenceSurvivesLoad:
    def test_env_reference_preserved_on_load_and_resolved_at_launch(
        self, tmp_path, monkeypatch
    ) -> None:
        # Managed .env is empty: generic interpolation must leave the reference
        # untouched even though the process env HAS the variable set.
        empty_env_file = tmp_path / "cao.env"
        monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", empty_env_file)
        monkeypatch.setenv("CAO_SERENA_MCP_URL", "https://serena.example/mcp")

        local_store = tmp_path / "agent-store"
        local_store.mkdir()
        (local_store / "serena-agent.md").write_text(
            "---\n"
            "name: serena-agent\n"
            "description: Navigation\n"
            "mcpServers:\n"
            "  serena:\n"
            "    type: http\n"
            "    url: ${CAO_SERENA_MCP_URL}\n"
            "---\n"
            "Body\n"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        profile = load_agent_profile("serena-agent")
        # Break guarded: load-time interpolation must NOT expand the reference
        # from the process environment.
        assert profile.mcpServers["serena"]["url"] == "${CAO_SERENA_MCP_URL}"

        # Launch resolution reads the process env and yields the literal URL.
        resolved = resolve_mcp_servers(profile.mcpServers)
        assert resolved["serena"].url == "https://serena.example/mcp"  # type: ignore[union-attr]

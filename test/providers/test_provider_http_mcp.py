"""Provider-native HTTP MCP translation for Codex, Claude, and Antigravity.

Each supported provider must emit its exact native HTTP MCP shape — with NO
empty subprocess fields — and resolve an HTTP URL reference from the process
environment at launch. ``CAO_TERMINAL_ID`` is injected ONLY into command
entries; HTTP entries never carry it. Providers without a native HTTP mapping
fail closed rather than coercing an HTTP entry into a synthetic command.

Kimi's ``.kimi-code/mcp.json`` translation is covered in test_kimi_http_mcp.py.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.mcp_server import McpConfigError
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.mcp_translation import (
    HTTP_SUPPORTED_PROVIDERS,
    antigravity_http_entry,
    claude_http_entry,
    codex_http_fields,
    kimi_http_entry,
    render_http_entry,
)

_OPS_URL = "http://127.0.0.1:9889/mcp/ops"
_EXTERNAL_URL = "https://mcp.example.test/ops"


@pytest.fixture(autouse=True)
def _auth_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep native-shape tests independent of the operator's auth environment."""
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)


def _read_claude_mcp(command: str) -> dict:
    args = shlex.split(command)
    assert "--strict-mcp-config" in args
    mcp_file = Path(args[args.index("--mcp-config") + 1])
    try:
        return json.loads(mcp_file.read_text())
    finally:
        mcp_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Shared translation registry
# --------------------------------------------------------------------------- #


class TestTranslationRegistry:
    def test_supported_providers_are_the_four_native_ones(self) -> None:
        assert HTTP_SUPPORTED_PROVIDERS == frozenset(
            {"codex", "claude_code", "antigravity_cli", "kimi_cli"}
        )

    @pytest.mark.parametrize(
        "provider,expected",
        [
            pytest.param("claude_code", {"type": "http", "url": _OPS_URL}, id="claude"),
            pytest.param("antigravity_cli", {"serverUrl": _OPS_URL}, id="antigravity"),
            pytest.param("kimi_cli", {"url": _OPS_URL}, id="kimi"),
        ],
    )
    def test_render_http_entry_maps_native_shape(self, provider: str, expected: dict) -> None:
        assert render_http_entry(provider, _OPS_URL) == expected

    @pytest.mark.parametrize(
        "provider",
        ["codex", "claude_code", "antigravity_cli", "kimi_cli"],
    )
    def test_no_provider_emits_the_stale_httpurl_field(self, provider: str) -> None:
        # Break guarded: ``httpUrl`` is Gemini CLI's field, not the direct URL
        # field accepted by Agy CLI's mcp_config.json surface.
        assert "httpUrl" not in render_http_entry(provider, _OPS_URL)

    def test_codex_http_fields_have_no_subprocess_keys(self) -> None:
        fields = codex_http_fields(_OPS_URL)
        assert fields["url"] == _OPS_URL
        assert fields["tool_timeout_sec"] == 600.0
        for forbidden in ("command", "args", "env", "env_vars"):
            assert forbidden not in fields

    @pytest.mark.parametrize(
        "helper,expected",
        [
            pytest.param(claude_http_entry, {"type": "http", "url": _OPS_URL}, id="claude"),
            pytest.param(
                antigravity_http_entry,
                {"serverUrl": _OPS_URL},
                id="antigravity",
            ),
            pytest.param(kimi_http_entry, {"url": _OPS_URL}, id="kimi"),
        ],
    )
    def test_helpers_emit_no_empty_subprocess_fields(self, helper, expected: dict) -> None:
        result = helper(_OPS_URL)
        assert result == expected
        for forbidden in ("command", "args", "env"):
            assert forbidden not in result

    @pytest.mark.parametrize(
        "provider",
        ["cursor_cli", "kiro_cli", "copilot_cli", "opencode_cli", "hermes", "mock_cli"],
    )
    def test_unsupported_provider_fails_closed(self, provider: str) -> None:
        # Break guarded: an HTTP entry handed to a provider with no native
        # mapping must fail clearly, never be coerced to a command entry.
        with pytest.raises(McpConfigError):
            render_http_entry(provider, _OPS_URL)

    @pytest.mark.parametrize(
        "provider,expected_auth",
        [
            pytest.param(
                "codex",
                {"bearer_token_env_var": "CAO_AUTH_LOCAL_TOKEN"},
                id="codex",
            ),
            pytest.param(
                "claude_code",
                {"headers": {"Authorization": "Bearer ${CAO_AUTH_LOCAL_TOKEN}"}},
                id="claude",
            ),
            pytest.param(
                "antigravity_cli",
                {"headers": {"Authorization": "Bearer fake-machine-token"}},
                id="antigravity",
            ),
            pytest.param(
                "kimi_cli",
                {"bearerTokenEnvVar": "CAO_AUTH_LOCAL_TOKEN"},
                id="kimi",
            ),
        ],
    )
    def test_authenticated_local_ops_uses_provider_native_auth(
        self, provider: str, expected_auth: dict
    ) -> None:
        env = {
            "CAO_AUTH_JWKS_URI": "https://idp.example.test/jwks",
            "CAO_AUTH_LOCAL_TOKEN": "fake-machine-token",
        }

        rendered = render_http_entry(provider, _OPS_URL, env=env)

        for key, value in expected_auth.items():
            assert rendered[key] == value

    @pytest.mark.parametrize("provider", sorted(HTTP_SUPPORTED_PROVIDERS))
    def test_machine_token_is_never_attached_to_external_http_server(self, provider: str) -> None:
        env = {
            "AUTH0_DOMAIN": "tenant.example.test",
            "CAO_AUTH_LOCAL_TOKEN": "fake-machine-token",
        }

        rendered = render_http_entry(provider, _EXTERNAL_URL, env=env)

        assert "bearer_token_env_var" not in rendered
        assert "bearerTokenEnvVar" not in rendered
        assert "headers" not in rendered
        assert "fake-machine-token" not in json.dumps(rendered)

    @pytest.mark.parametrize("provider", sorted(HTTP_SUPPORTED_PROVIDERS))
    def test_authenticated_local_ops_without_machine_token_fails_closed(
        self, provider: str
    ) -> None:
        with pytest.raises(McpConfigError, match="CAO_AUTH_LOCAL_TOKEN"):
            render_http_entry(
                provider,
                _OPS_URL,
                env={"CAO_AUTH_JWKS_URI": "https://idp.example.test/jwks"},
            )


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


def _codex_http_profile(url: str) -> MagicMock:
    prof = MagicMock()
    prof.model = None
    prof.system_prompt = ""
    prof.codexProfile = None
    prof.codexConfig = None
    prof.mcpServers = {"cao-ops": {"type": "http", "url": url}}
    return prof


class TestCodexHttpMapping:
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_emits_url_and_timeout_no_subprocess(self, mock_load) -> None:
        mock_load.return_value = _codex_http_profile(_OPS_URL)
        provider = CodexProvider("t1", "s", "w", "agent")
        command = provider._build_codex_command()

        assert f'mcp_servers.cao-ops.url="{_OPS_URL}"' in command
        assert "mcp_servers.cao-ops.tool_timeout_sec=600.0" in command
        # Exactly the anti-pattern the spec forbids: no synthetic subprocess keys.
        assert "mcp_servers.cao-ops.command" not in command
        assert "mcp_servers.cao-ops.args" not in command
        assert "mcp_servers.cao-ops.env" not in command
        assert "mcp_servers.cao-ops.env_vars" not in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_resolves_reference_from_process_env(self, mock_load, monkeypatch) -> None:
        monkeypatch.setenv("CAO_SERENA_MCP_URL", "https://serena.example/mcp")
        prof = _codex_http_profile("${CAO_SERENA_MCP_URL}")
        mock_load.return_value = prof
        provider = CodexProvider("t1", "s", "w", "agent")
        command = provider._build_codex_command()
        assert 'mcp_servers.cao-ops.url="https://serena.example/mcp"' in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_unset_reference_fails_closed(self, mock_load, monkeypatch) -> None:
        monkeypatch.delenv("CAO_SERENA_MCP_URL", raising=False)
        mock_load.return_value = _codex_http_profile("${CAO_SERENA_MCP_URL}")
        provider = CodexProvider("t1", "s", "w", "agent")
        with pytest.raises(McpConfigError):
            provider._build_codex_command()

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_codex_authenticated_ops_references_token_env_without_value(
        self, mock_load, monkeypatch
    ) -> None:
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example.test/jwks")
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "fake-machine-token")
        mock_load.return_value = _codex_http_profile(_OPS_URL)

        command = CodexProvider("t1", "s", "w", "agent")._build_codex_command()

        assert 'mcp_servers.cao-ops.bearer_token_env_var="CAO_AUTH_LOCAL_TOKEN"' in command
        assert "fake-machine-token" not in command


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def _claude_profile(mcp_servers: dict) -> MagicMock:
    prof = MagicMock()
    prof.model = None
    prof.system_prompt = None
    prof.permissionMode = None
    prof.mcpServers = mcp_servers
    return prof


class TestClaudeHttpMapping:
    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_claude_emits_type_http_url_no_subprocess(self, mock_load) -> None:
        mock_load.return_value = _claude_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})
        provider = ClaudeCodeProvider("term-1", "s", "w", "agent")
        command = provider._build_claude_command()

        entry = _read_claude_mcp(command)["mcpServers"]["cao-ops"]
        assert entry == {"type": "http", "url": _OPS_URL}
        assert "command" not in entry
        assert "env" not in entry

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_claude_terminal_id_command_only(self, mock_load) -> None:
        # A profile mixing a command server and an HTTP server: only the command
        # server receives CAO_TERMINAL_ID; the HTTP server never does.
        mock_load.return_value = _claude_profile(
            {
                "cao-mcp-server": {"command": "cao-mcp-server", "args": []},
                "cao-ops": {"type": "http", "url": _OPS_URL},
            }
        )
        provider = ClaudeCodeProvider("term-77", "s", "w", "agent")
        command = provider._build_claude_command()
        servers = _read_claude_mcp(command)["mcpServers"]

        assert servers["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "term-77"
        assert servers["cao-ops"] == {"type": "http", "url": _OPS_URL}
        assert "env" not in servers["cao-ops"]

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_claude_authenticated_ops_uses_environment_header_reference(
        self, mock_load, monkeypatch
    ) -> None:
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example.test/jwks")
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "fake-machine-token")
        mock_load.return_value = _claude_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})

        command = ClaudeCodeProvider("term-1", "s", "w", "agent")._build_claude_command()
        entry = _read_claude_mcp(command)["mcpServers"]["cao-ops"]

        assert entry["headers"] == {"Authorization": "Bearer ${CAO_AUTH_LOCAL_TOKEN}"}
        assert "fake-machine-token" not in json.dumps(entry)


# --------------------------------------------------------------------------- #
# Antigravity
# --------------------------------------------------------------------------- #


class TestAntigravityHttpMapping:
    def test_antigravity_emits_server_url_no_subprocess(self, tmp_path) -> None:
        cfg = tmp_path / "mcp_config.json"
        profile = AgentProfile(
            name="reviewer",
            description="Reviewer",
            system_prompt="Review.",
            mcpServers={"cao-ops": {"type": "http", "url": _OPS_URL}},
        )
        provider = AntigravityCliProvider("test-tid", "s", "w", agent_profile="reviewer")
        with (
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
                return_value="/usr/local/bin/agy",
            ),
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
                return_value=profile,
            ),
            patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        ):
            provider._build_agy_command()

        entry = json.loads(cfg.read_text())["mcpServers"]["cao-ops"]
        assert entry == {"serverUrl": _OPS_URL}
        # ``serverUrl`` is Antigravity's documented canonical field. ``url`` is
        # accepted only as a compatibility alias and ``httpUrl`` is Gemini CLI.
        for forbidden in ("url", "httpUrl", "command", "args", "env"):
            assert forbidden not in entry

    def test_antigravity_terminal_id_command_only(self, tmp_path) -> None:
        cfg = tmp_path / "mcp_config.json"
        profile = AgentProfile(
            name="reviewer",
            description="Reviewer",
            system_prompt="Review.",
            mcpServers={
                "cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]},
                "cao-ops": {"type": "http", "url": _OPS_URL},
            },
        )
        provider = AntigravityCliProvider("test-tid", "s", "w", agent_profile="reviewer")
        with (
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
                return_value="/usr/local/bin/agy",
            ),
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
                return_value=profile,
            ),
            patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        ):
            provider._build_agy_command()

        servers = json.loads(cfg.read_text())["mcpServers"]
        assert servers["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
        assert servers["cao-ops"] == {"serverUrl": _OPS_URL}
        assert "env" not in servers["cao-ops"]

    def test_antigravity_authenticated_ops_uses_private_literal_header(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example.test/jwks")
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "fake-machine-token")
        cfg = tmp_path / "mcp_config.json"
        profile = AgentProfile(
            name="reviewer",
            description="Reviewer",
            system_prompt="Review.",
            mcpServers={"cao-ops": {"type": "http", "url": _OPS_URL}},
        )
        provider = AntigravityCliProvider("test-tid", "s", "w", agent_profile="reviewer")
        with (
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
                return_value="/usr/local/bin/agy",
            ),
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
                return_value=profile,
            ),
            patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        ):
            command = provider._build_agy_command()

        entry = json.loads(cfg.read_text())["mcpServers"]["cao-ops"]
        assert entry == {
            "serverUrl": _OPS_URL,
            "headers": {"Authorization": "Bearer fake-machine-token"},
        }
        assert "fake-machine-token" not in command
        assert cfg.stat().st_mode & 0o777 == 0o600

    def test_antigravity_refuses_token_write_when_private_mode_fails(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example.test/jwks")
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "fake-machine-token")
        cfg = tmp_path / "mcp_config.json"
        profile = AgentProfile(
            name="reviewer",
            description="Reviewer",
            system_prompt="Review.",
            mcpServers={"cao-ops": {"type": "http", "url": _OPS_URL}},
        )
        provider = AntigravityCliProvider("test-tid", "s", "w", agent_profile="reviewer")
        with (
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
                return_value="/usr/local/bin/agy",
            ),
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
                return_value=profile,
            ),
            patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
            patch.object(Path, "chmod", side_effect=OSError("denied")),
            pytest.raises(McpConfigError, match="private Antigravity MCP config"),
        ):
            provider._build_agy_command()

        assert not cfg.exists() or "fake-machine-token" not in cfg.read_text()

    def test_antigravity_unset_reference_fails_closed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("CAO_SERENA_MCP_URL", raising=False)
        cfg = tmp_path / "mcp_config.json"
        profile = AgentProfile(
            name="reviewer",
            description="Reviewer",
            system_prompt="Review.",
            mcpServers={"cao-ops": {"type": "http", "url": "${CAO_SERENA_MCP_URL}"}},
        )
        provider = AntigravityCliProvider("test-tid", "s", "w", agent_profile="reviewer")
        with (
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
                return_value="/usr/local/bin/agy",
            ),
            patch(
                "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
                return_value=profile,
            ),
            patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
            pytest.raises(McpConfigError),
        ):
            provider._build_agy_command()

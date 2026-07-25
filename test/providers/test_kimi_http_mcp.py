"""Kimi 0.29 MCP delivery via per-terminal ``.kimi-code/mcp.json``.

Kimi 0.29 has no ``--mcp-config`` flag; it reads a project-local
``<cwd>/.kimi-code/mcp.json`` (Claude-compatible ``mcpServers`` map). CAO writes
that file beneath the terminal's unique temp working directory. HTTP entries are
the minimal ``{"url": ...}`` form; command entries keep command/args/env with
``CAO_TERMINAL_ID`` injected. ``--mcp-config`` is never passed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.mcp_server import McpConfigError
from cli_agent_orchestrator.providers.kimi_cli import (
    KIMI_CWD_MCP_RELPATH,
    KIMI_MCP_DISCOVERY_SCOPES,
    KIMI_MCP_HOME_ENV_VAR,
    KimiCliProvider,
    kimi_mcp_home,
)
from cli_agent_orchestrator.providers.mcp_translation import kimi_http_entry

_OPS_URL = "http://127.0.0.1:9889/mcp/ops"
_HOME = "cli_agent_orchestrator.providers.kimi_cli.Path.home"
_LOAD = "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile"


def _kimi_mcp_file(provider: KimiCliProvider) -> dict:
    path = Path(provider._temp_dir) / ".kimi-code" / "mcp.json"
    return json.loads(path.read_text())


def _profile(mcp_servers) -> MagicMock:
    prof = MagicMock()
    prof.model = None
    prof.system_prompt = None
    prof.mcpServers = mcp_servers
    return prof


class TestKimiHttpMapping:
    def test_http_entry_written_as_url_no_mcp_config_flag(self, tmp_path) -> None:
        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})),
        ):
            command = provider._build_kimi_command()
            entry = _kimi_mcp_file(provider)["mcpServers"]["cao-ops"]

        # Kimi 0.29 has no --mcp-config flag; passing it would be a hard error.
        assert "--mcp-config" not in command
        assert entry == {"url": _OPS_URL}
        for forbidden in ("command", "args", "env", "type", "transport"):
            assert forbidden not in entry
        provider.cleanup()

    def test_command_entry_keeps_command_and_gets_terminal_id(self, tmp_path) -> None:
        provider = KimiCliProvider("term-9", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(
                _LOAD,
                return_value=_profile(
                    {"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}}
                ),
            ),
        ):
            provider._build_kimi_command()
            entry = _kimi_mcp_file(provider)["mcpServers"]["cao-mcp-server"]

        assert entry["command"] == "uvx"
        assert entry["env"]["CAO_TERMINAL_ID"] == "term-9"
        provider.cleanup()

    def test_terminal_id_reaches_orchestration_server_only(self, tmp_path) -> None:
        provider = KimiCliProvider("term-9", "s", "w", agent_profile="dev")
        servers = {
            "cao-mcp-server": {"command": "cao-mcp-server", "args": []},
            "other": {"command": "npx", "args": ["srv"]},
            "cao-ops": {"type": "http", "url": _OPS_URL},
        }
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile(servers)),
        ):
            provider._build_kimi_command()
            written = _kimi_mcp_file(provider)["mcpServers"]

        assert written["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "term-9"
        # A third-party command server must not be handed this terminal's identity.
        assert "env" not in written["other"]
        assert written["cao-ops"] == {"url": _OPS_URL}
        assert "env" not in written["cao-ops"]
        provider.cleanup()

    def test_reference_resolved_from_process_env(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CAO_SERENA_MCP_URL", "https://serena.example/mcp")
        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(
                _LOAD,
                return_value=_profile({"serena": {"type": "http", "url": "${CAO_SERENA_MCP_URL}"}}),
            ),
        ):
            provider._build_kimi_command()
            entry = _kimi_mcp_file(provider)["mcpServers"]["serena"]
        assert entry == {"url": "https://serena.example/mcp"}
        provider.cleanup()

    def test_unset_reference_fails_closed(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("CAO_SERENA_MCP_URL", raising=False)
        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(
                _LOAD,
                return_value=_profile({"serena": {"type": "http", "url": "${CAO_SERENA_MCP_URL}"}}),
            ),
            pytest.raises(McpConfigError),
        ):
            provider._build_kimi_command()
        provider.cleanup()


class TestDocumentedDiscoveryLocations:
    """Kimi resolves MCP config from three scopes; CAO writes the cwd scope only."""

    def test_three_documented_scopes_in_override_order(self) -> None:
        # Derived from the official docs, not the implementation: user scope
        # first, then project root, then cwd — later scopes override earlier.
        assert KIMI_MCP_DISCOVERY_SCOPES == (
            "$KIMI_CODE_HOME/mcp.json",
            ".mcp.json",
            ".kimi-code/mcp.json",
        )
        assert KIMI_MCP_DISCOVERY_SCOPES[-1] == KIMI_CWD_MCP_RELPATH

    def test_user_scope_default_home(self, tmp_path, monkeypatch) -> None:
        # Resolved at call time (never captured at import), and $KIMI_CODE_HOME
        # wins over the ~/.kimi-code default.
        assert KIMI_MCP_HOME_ENV_VAR == "KIMI_CODE_HOME"
        monkeypatch.delenv(KIMI_MCP_HOME_ENV_VAR, raising=False)
        with patch(_HOME, return_value=tmp_path):
            assert kimi_mcp_home() == tmp_path / ".kimi-code"
        monkeypatch.setenv(KIMI_MCP_HOME_ENV_VAR, str(tmp_path / "explicit"))
        assert kimi_mcp_home() == tmp_path / "explicit"

    def test_cao_writes_the_last_overriding_scope(self, tmp_path) -> None:
        # Break guarded: writing an earlier scope would let a user-global or
        # repository-shared file override CAO's per-terminal config.
        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})),
        ):
            provider._build_kimi_command()
            written = Path(provider._temp_dir) / KIMI_CWD_MCP_RELPATH
            assert written.is_file()
            assert json.loads(written.read_text())["mcpServers"]["cao-ops"] == {"url": _OPS_URL}
        provider.cleanup()

    def test_user_global_and_project_scopes_are_not_mutated(self, tmp_path) -> None:
        # Break guarded: mutating $KIMI_CODE_HOME/mcp.json or the repository's
        # .mcp.json would leak CAO wiring into the user's own Kimi sessions.
        home_scope = tmp_path / ".kimi-code" / "mcp.json"
        home_scope.parent.mkdir(parents=True)
        home_scope.write_text('{"mcpServers": {"users-own": {"url": "https://u/x"}}}')
        home_before = home_scope.read_text()

        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})),
        ):
            provider._build_kimi_command()
            project_scope = Path(provider._temp_dir) / ".mcp.json"
            assert not project_scope.exists()
        assert home_scope.read_text() == home_before
        provider.cleanup()


class TestLegacySseIsNotEmitted:
    """An ordinary HTTP entry is flat ``{url}``; legacy SSE is a non-goal."""

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param(_OPS_URL, id="literal"),
            pytest.param("${CAO_LEGACY_PROBE_URL}", id="reference"),
        ],
    )
    def test_ordinary_http_entry_has_no_transport_key(
        self, url: str, tmp_path, monkeypatch
    ) -> None:
        # Break guarded: {"transport": "sse", "url": ...} selects Kimi's LEGACY
        # SSE transport — a different protocol from the Streamable HTTP server
        # CAO exposes, so emitting it would connect to the wrong surface.
        monkeypatch.setenv("CAO_LEGACY_PROBE_URL", _OPS_URL)
        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile({"cao-ops": {"type": "http", "url": url}})),
        ):
            provider._build_kimi_command()
            entry = _kimi_mcp_file(provider)["mcpServers"]["cao-ops"]
        assert entry == {"url": _OPS_URL}
        assert set(entry) == {"url"}
        provider.cleanup()

    def test_translator_never_emits_transport(self) -> None:
        assert set(kimi_http_entry(_OPS_URL)) == {"url"}


class TestInstalledKimiSmoke:
    @pytest.mark.skipif(shutil.which("kimi") is None, reason="kimi not installed")
    def test_installed_help_has_no_mcp_config_flag(self) -> None:
        # Smoke: 0.29 removed --mcp-config. Passing it is a hard CLI error, so a
        # version that reintroduces the flag should be a deliberate decision.
        out = subprocess.run(["kimi", "--help"], capture_output=True, text=True, timeout=30)
        help_text = (out.stdout or "") + (out.stderr or "")
        assert "--mcp-config" not in help_text
        # The model/prompt surfaces CAO relies on elsewhere.
        assert "--model" in help_text
        assert "--prompt" in help_text

    @pytest.mark.skipif(shutil.which("kimi") is None, reason="kimi not installed")
    def test_generated_config_targets_installed_major_minor(self, tmp_path) -> None:
        # Smoke: our .kimi-code/mcp.json shape is pinned to Kimi 0.29. If the
        # installed CLI has moved off 0.29 the format assumption must be re-checked.
        out = subprocess.run(["kimi", "--version"], capture_output=True, text=True, timeout=15)
        version = (out.stdout or out.stderr).strip()
        assert version.startswith("0.29"), f"expected Kimi 0.29.x, got {version!r}"

        provider = KimiCliProvider("t1", "s", "w", agent_profile="dev")
        with (
            patch(_HOME, return_value=tmp_path),
            patch(_LOAD, return_value=_profile({"cao-ops": {"type": "http", "url": _OPS_URL}})),
        ):
            provider._build_kimi_command()
            path = Path(provider._temp_dir) / ".kimi-code" / "mcp.json"
            data = json.loads(path.read_text())
        # Documented shape: {"mcpServers": {name: {"url": ...}}}.
        assert data["mcpServers"]["cao-ops"] == {"url": _OPS_URL}
        provider.cleanup()

"""Fresh callback identity per created terminal.

Every terminal launch snapshots the **newly created** terminal id and injects it
only into each command-launched *identity-bearing* ``cao-mcp-server`` entry.
Launch must not reuse terminal identity from any other source, and must not
synthesize an identity for entries that are not the orchestration server.

The stale sources seeded here are exactly the five the spec enumerates:

======================  =============================================
seeded source           how it is planted in these tests
======================  =============================================
profile                 ``env.CAO_TERMINAL_ID`` inside the profile entry
process                 ``monkeypatch.setenv`` on the cao-server process env
provider (in-memory)    a second provider instance built earlier in-process
persisted config        a pre-existing ``mcp_config.json`` on disk (agy)
prior session           a previously written per-terminal config file
======================  =============================================

Every case derives the expected id from the terminal id passed to the provider
constructor — never from the implementation — so a launch that reads identity
from anywhere else fails.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

_OPS_URL = "http://127.0.0.1:9889/mcp/ops"
_STALE = "stale-99"
_FRESH = "fresh-11"

# The identity-bearing orchestration server, in each of its real profile shapes.
_IDENTITY_SHAPES = {
    "bare-console-script": {"command": "cao-mcp-server", "args": []},
    "uvx-wrapped": {"command": "uvx", "args": ["cao-mcp-server"]},
    "module-entrypoint": {
        "command": "python3",
        "args": ["-m", "cli_agent_orchestrator.mcp_server.server"],
    },
}

# A command MCP server that is NOT the orchestration server.
_UNRELATED = {"command": "npx", "args": ["some-other-mcp"]}


def _profile(mcp_servers: Dict[str, Any]) -> MagicMock:
    prof = MagicMock()
    prof.model = None
    prof.system_prompt = None
    prof.permissionMode = None
    prof.codexProfile = None
    prof.codexConfig = None
    prof.native_agent = None
    prof.mcpServers = mcp_servers
    return prof


# --------------------------------------------------------------------------- #
# Per-provider "launch and read back the emitted MCP servers" adapters
# --------------------------------------------------------------------------- #


def _claude_servers(terminal_id: str, mcp_servers: dict, tmp_path: Path) -> dict:
    with patch(
        "cli_agent_orchestrator.providers.claude_code.load_agent_profile",
        return_value=_profile(mcp_servers),
    ):
        command = ClaudeCodeProvider(terminal_id, "s", "w", "agent")._build_claude_command()
    args = shlex.split(command)
    mcp_file = Path(args[args.index("--mcp-config") + 1])
    try:
        return json.loads(mcp_file.read_text())["mcpServers"]
    finally:
        mcp_file.unlink(missing_ok=True)


def _antigravity_servers(terminal_id: str, mcp_servers: dict, tmp_path: Path) -> dict:
    cfg = tmp_path / "mcp_config.json"
    provider = AntigravityCliProvider(terminal_id, "s", "w", agent_profile="p")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=_profile(mcp_servers),
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        provider._build_agy_command()
    return json.loads(cfg.read_text())["mcpServers"]


def _kimi_servers(terminal_id: str, mcp_servers: dict, tmp_path: Path) -> dict:
    provider = KimiCliProvider(terminal_id, "s", "w", agent_profile="p")
    try:
        with (
            patch(
                "cli_agent_orchestrator.providers.kimi_cli.Path.home",
                return_value=tmp_path,
            ),
            patch(
                "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
                return_value=_profile(mcp_servers),
            ),
        ):
            provider._build_kimi_command()
        path = Path(provider._temp_dir) / ".kimi-code" / "mcp.json"
        return json.loads(path.read_text())["mcpServers"]
    finally:
        provider.cleanup()


def _codex_env_of(command: str, server_name: str) -> Dict[str, str]:
    """Extract ``mcp_servers.<name>.env.*`` overrides from a codex command."""
    args = shlex.split(command)
    prefix = f"mcp_servers.{server_name}.env."
    env: Dict[str, str] = {}
    for flag, value in zip(args, args[1:]):
        if flag == "-c" and value.startswith(prefix):
            key, _, raw = value[len(prefix) :].partition("=")
            env[key] = raw.strip('"')
    return env


def _codex_servers(terminal_id: str, mcp_servers: dict, tmp_path: Path) -> dict:
    """Normalize codex's ``-c`` overrides into the shared ``{name: {env: ...}}``."""
    with patch(
        "cli_agent_orchestrator.providers.codex.load_agent_profile",
        return_value=_profile(mcp_servers),
    ):
        command = CodexProvider(terminal_id, "s", "w", "agent")._build_codex_command()
    servers: Dict[str, Any] = {}
    for name in mcp_servers:
        env = _codex_env_of(command, name)
        servers[name] = {"env": env} if env else {}
        # env_vars must never carry the identity: it inherits from the *parent*
        # process, which is precisely the stale source the spec forbids.
        servers[name]["_env_vars_raw"] = [
            value
            for flag, value in zip(shlex.split(command), shlex.split(command)[1:])
            if flag == "-c" and value.startswith(f"mcp_servers.{name}.env_vars=")
        ]
    return servers


_ADAPTERS = [
    pytest.param(_claude_servers, id="claude"),
    pytest.param(_codex_servers, id="codex"),
    pytest.param(_antigravity_servers, id="antigravity"),
    pytest.param(_kimi_servers, id="kimi"),
]


def _terminal_id_of(entry: dict) -> str | None:
    return (entry.get("env") or {}).get("CAO_TERMINAL_ID")


# --------------------------------------------------------------------------- #
# Identity-bearing command entry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _ADAPTERS)
class TestIdentityBearingCommandEntry:
    @pytest.mark.parametrize("shape", list(_IDENTITY_SHAPES), ids=list(_IDENTITY_SHAPES))
    def test_created_terminal_id_reaches_cao_mcp_server(
        self, adapter, shape: str, tmp_path
    ) -> None:
        # Break guarded: without the created-terminal id, cao-mcp-server cannot
        # attribute a callback, so handoff/assign silently target nothing.
        servers = adapter(_FRESH, {"cao-mcp-server": dict(_IDENTITY_SHAPES[shape])}, tmp_path)
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH

    def test_stale_profile_identity_is_replaced(self, adapter, tmp_path) -> None:
        # Seeded source: profile. A profile that hardcodes an old terminal id
        # must NOT win — the created terminal's id is authoritative.
        entry = {"command": "cao-mcp-server", "args": [], "env": {"CAO_TERMINAL_ID": _STALE}}
        servers = adapter(_FRESH, {"cao-mcp-server": entry}, tmp_path)
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH

    def test_stale_process_identity_is_not_reused(self, adapter, tmp_path, monkeypatch) -> None:
        # Seeded source: process. cao-server itself often runs inside a CAO
        # terminal, so its own CAO_TERMINAL_ID is present and must be ignored.
        monkeypatch.setenv("CAO_TERMINAL_ID", _STALE)
        servers = adapter(_FRESH, {"cao-mcp-server": {"command": "cao-mcp-server"}}, tmp_path)
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH

    def test_other_profile_env_vars_survive(self, adapter, tmp_path) -> None:
        # Replacing the identity must not clobber unrelated env the profile set.
        entry = {
            "command": "cao-mcp-server",
            "env": {"CAO_TERMINAL_ID": _STALE, "MY_VAR": "keep-me"},
        }
        servers = adapter(_FRESH, {"cao-mcp-server": entry}, tmp_path)
        assert servers["cao-mcp-server"]["env"]["MY_VAR"] == "keep-me"
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH


# --------------------------------------------------------------------------- #
# Entries that must receive NO identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _ADAPTERS)
class TestNonIdentityEntriesReceiveNothing:
    def test_unrelated_command_entry_gets_no_identity(self, adapter, tmp_path) -> None:
        # Break guarded: synthesizing CAO_TERMINAL_ID for a third-party MCP
        # server hands an unrelated subprocess this terminal's callback identity.
        servers = adapter(_FRESH, {"other": dict(_UNRELATED)}, tmp_path)
        assert _terminal_id_of(servers["other"]) is None

    def test_stale_identity_on_unrelated_entry_is_stripped(self, adapter, tmp_path) -> None:
        # A stale identity on a non-orchestration entry is not "the user's
        # choice" — it lets that server impersonate some other terminal.
        entry = dict(_UNRELATED, env={"CAO_TERMINAL_ID": _STALE, "KEEP": "yes"})
        servers = adapter(_FRESH, {"other": entry}, tmp_path)
        assert _terminal_id_of(servers["other"]) is None
        assert servers["other"]["env"]["KEEP"] == "yes"

    def test_http_entry_gets_no_identity_and_no_env(self, adapter, tmp_path) -> None:
        servers = adapter(_FRESH, {"cao-ops": {"type": "http", "url": _OPS_URL}}, tmp_path)
        entry = servers["cao-ops"]
        assert _terminal_id_of(entry) is None
        assert "env" not in entry
        assert "env_vars" not in entry

    def test_mixed_profile_routes_identity_only_to_orchestration_server(
        self, adapter, tmp_path
    ) -> None:
        servers = adapter(
            _FRESH,
            {
                "cao-mcp-server": {"command": "cao-mcp-server", "args": []},
                "other": dict(_UNRELATED),
                "cao-ops": {"type": "http", "url": _OPS_URL},
            },
            tmp_path,
        )
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH
        assert _terminal_id_of(servers["other"]) is None
        assert _terminal_id_of(servers["cao-ops"]) is None


# --------------------------------------------------------------------------- #
# Independence across launches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", _ADAPTERS)
class TestConsecutiveLaunchIndependence:
    def test_two_launches_from_one_profile_get_independent_snapshots(
        self, adapter, tmp_path
    ) -> None:
        # Break guarded: a shared/cached launch config would let the second
        # terminal answer with the first terminal's identity (or vice versa).
        # The SAME profile dict is reused, so any in-place mutation leaks.
        profile_servers = {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}
        first = adapter("term-a", profile_servers, tmp_path)
        second = adapter("term-b", profile_servers, tmp_path)
        assert _terminal_id_of(first["cao-mcp-server"]) == "term-a"
        assert _terminal_id_of(second["cao-mcp-server"]) == "term-b"
        # And the source profile is untouched, so a third launch resolves clean.
        assert "env" not in profile_servers["cao-mcp-server"]


class TestPersistedProviderConfigIdentity:
    """Seeded source: persisted config + prior session (Antigravity on disk)."""

    def test_persisted_stale_identity_is_overwritten(self, tmp_path) -> None:
        cfg = tmp_path / "mcp_config.json"
        # A prior session left this terminal's slot behind with an old id.
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "cao-mcp-server": {
                            "command": "cao-mcp-server",
                            "args": [],
                            "env": {"CAO_TERMINAL_ID": _STALE},
                        },
                        "users-own-server": {"command": "npx", "args": ["mine"]},
                    }
                }
            )
        )
        servers = _antigravity_servers(
            _FRESH, {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}}, tmp_path
        )
        assert _terminal_id_of(servers["cao-mcp-server"]) == _FRESH
        # The user's unrelated pre-existing server is preserved untouched.
        assert servers["users-own-server"] == {"command": "npx", "args": ["mine"]}

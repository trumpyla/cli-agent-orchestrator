"""Antigravity native permission modes.

``permissionMode: plan`` maps to ``--mode plan`` and ``acceptEdits`` maps to
``--mode accept-edits``. Selecting an explicit mode MUST omit
``--dangerously-skip-permissions``. If the installed ``agy`` cannot support the
requested native mode, initialization fails closed rather than silently enabling
bypass or downgrading the mode.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.antigravity_cli import (
    AntigravityCliProvider,
    ProviderError,
    _agy_supported_modes,
)

_ALL_MODES = frozenset({"plan", "accept-edits"})

_WHICH = "cli_agent_orchestrator.providers.antigravity_cli.shutil.which"
_LOAD = "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile"
_MODES = "cli_agent_orchestrator.providers.antigravity_cli._agy_supported_modes"


def _make(agent_profile: str = "p") -> AntigravityCliProvider:
    return AntigravityCliProvider("test-tid", "s", "w", agent_profile=agent_profile)


def _profile(permission_mode: str | None) -> AgentProfile:
    return AgentProfile(
        name="p",
        description="d",
        system_prompt="Review only.",
        permissionMode=permission_mode,
    )


def _build_with(monkeypatch, profile: AgentProfile, supported=_ALL_MODES) -> str:
    monkeypatch.setattr(_WHICH, lambda _cmd: "/usr/local/bin/agy")
    monkeypatch.setattr(_LOAD, lambda _name: profile)
    monkeypatch.setattr(_MODES, lambda *a, **k: frozenset(supported))
    return _make()._build_agy_command()


class TestNativeModeMapping:
    def test_plan_mode_uses_mode_flag_and_omits_bypass(self, monkeypatch) -> None:
        command = _build_with(monkeypatch, _profile("plan"))
        assert "--mode plan" in command
        assert "--dangerously-skip-permissions" not in command

    def test_accept_edits_mode_uses_flag_and_omits_bypass(self, monkeypatch) -> None:
        command = _build_with(monkeypatch, _profile("acceptEdits"))
        assert "--mode accept-edits" in command
        assert "--dangerously-skip-permissions" not in command

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param(None, id="no-mode"),
            pytest.param("default", id="default"),
            pytest.param("bypassPermissions", id="bypass"),
        ],
    )
    def test_non_native_modes_keep_bypass(self, monkeypatch, mode: str | None) -> None:
        # Break guarded: a profile that does not request a native mode keeps the
        # historical unattended-orchestration bypass flag.
        command = _build_with(monkeypatch, _profile(mode))
        assert "--dangerously-skip-permissions" in command
        assert "--mode" not in command


class TestFailClosed:
    @pytest.mark.parametrize("mode", ["plan", "acceptEdits"])
    def test_unsupported_native_mode_fails_instead_of_bypass(self, monkeypatch, mode: str) -> None:
        # Break guarded: if agy cannot do the requested mode, init must fail —
        # never silently fall back to --dangerously-skip-permissions.
        with pytest.raises(ProviderError):
            _build_with(monkeypatch, _profile(mode), supported=frozenset())


class TestInstalledModeProbe:
    @pytest.mark.skipif(shutil.which("agy") is None, reason="agy not installed")
    def test_installed_agy_supports_native_modes(self) -> None:
        # Smoke: probe the REAL installed agy (>=1.1.7) so a version that dropped
        # native modes is caught before it silently re-enables bypass.
        modes = _agy_supported_modes()
        assert "plan" in modes
        assert "accept-edits" in modes

    @pytest.mark.skipif(shutil.which("agy") is None, reason="agy not installed")
    def test_installed_agy_documents_direct_url_mcp_entries(self) -> None:
        """Characterize the exact installed CLI surface used by this rollout."""
        executable = shutil.which("agy")
        assert executable is not None
        completed = subprocess.run(
            [executable, "changelog"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "Added support for `url` in `mcp_config.json`" in completed.stdout

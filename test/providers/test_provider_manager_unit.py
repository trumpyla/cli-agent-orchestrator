"""Unit tests for ProviderManager."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.hermes import HermesProvider
from cli_agent_orchestrator.providers.kimi_cli import ProviderError
from cli_agent_orchestrator.providers.manager import ProviderManager


def test_create_provider_codex_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.CODEX.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, CodexProvider)
    assert manager.get_provider("t1") is provider


def test_create_provider_copilot_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.COPILOT_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, CopilotCliProvider)
    assert manager.get_provider("t1") is provider


def test_create_provider_hermes_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.HERMES.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, HermesProvider)
    assert manager.get_provider("t1") is provider


def test_create_provider_unknown_type_raises():
    manager = ProviderManager()
    with pytest.raises(ValueError, match="Unknown provider type"):
        manager.create_provider(
            "unknown",
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )


def test_get_provider_creates_on_demand_from_metadata():
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.CODEX.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
        },
    ):
        provider = manager.get_provider("t1")

    assert isinstance(provider, CodexProvider)
    assert manager.get_provider("t1") is provider


def test_get_provider_creates_copilot_on_demand_from_metadata():
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.COPILOT_CLI.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
        },
    ):
        provider = manager.get_provider("t1")

    assert isinstance(provider, CopilotCliProvider)
    assert manager.get_provider("t1") is provider


@pytest.mark.parametrize(
    "provider_type",
    [ProviderType.CLAUDE_CODE.value, ProviderType.ANTIGRAVITY_CLI.value],
)
def test_get_provider_marks_persisted_initialized_tui_ready_without_shell_baseline(
    provider_type,
):
    """A restored live TUI must accept durable inbox delivery after an API restart."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": provider_type,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
            "shell_command": None,
            "provider_initialized": True,
        },
    ):
        provider = manager.get_provider("t1")

    assert provider.is_input_ready is True


@pytest.mark.parametrize(
    "provider_type",
    [ProviderType.CLAUDE_CODE.value, ProviderType.ANTIGRAVITY_CLI.value],
)
def test_get_provider_does_not_mark_deferred_uninitialized_tui_ready(provider_type):
    """A daemon restart must not make a half-started deferred worker input-ready."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": provider_type,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": None,
            "shell_command": None,
            "provider_initialized": False,
        },
    ):
        provider = manager.get_provider("t1")

    assert provider.is_input_ready is False


def test_get_provider_restores_undelivered_kimi_profile_prompt():
    """A restart before Kimi's first task keeps its profile/security prefix."""
    manager = ProviderManager()
    profile = MagicMock(system_prompt="Review only.", skills=None)

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.KIMI_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "reviewer",
                "allowed_tools": ["fs_read"],
                "shell_command": None,
                "provider_initialized": True,
                "profile_prompt_delivered": False,
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        provider = manager.get_provider("t1")

    prepared = provider.prepare_input("Inspect PR #1")
    assert "Review only." in prepared
    assert "You only have access to these tools: fs_read" in prepared


def test_get_provider_restores_kimi_skills_from_assigned_repository():
    """Restarted Kimi rebuilds its deferred skill catalog from persisted launch context."""
    manager = ProviderManager()
    profile = MagicMock(system_prompt="Review only.", skills=["python-design-patterns"])

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.KIMI_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "reviewer",
                "allowed_tools": ["fs_read"],
                "working_directory": "/projects/assigned-repo",
                "shell_command": None,
                "provider_initialized": True,
                "profile_prompt_delivered": False,
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.load_agent_profile",
            return_value=profile,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.build_skill_catalog",
            return_value="## Available Skills\n\n- python-design-patterns",
        ) as mock_catalog,
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        provider = manager.get_provider("t1")

    assert "python-design-patterns" in provider.prepare_input("Inspect PR #1")
    mock_catalog.assert_called_once_with(
        ["python-design-patterns"],
        start=Path("/projects/assigned-repo"),
    )


def test_get_provider_quarantines_kimi_when_undelivered_profile_cannot_load():
    """A missing profile must not crash restoration or permit unrestricted input."""
    manager = ProviderManager()

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.KIMI_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "deleted-reviewer",
                "allowed_tools": ["fs_read"],
                "shell_command": None,
                "provider_initialized": True,
                "profile_prompt_delivered": False,
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
            side_effect=FileNotFoundError("deleted"),
        ),
    ):
        provider = manager.get_provider("t1")

    assert provider.is_input_ready is False
    with pytest.raises(ProviderError, match="profile instructions could not be restored"):
        provider.prepare_input("Inspect PR #1")


def test_cleanup_provider_calls_cleanup_and_removes():
    manager = ProviderManager()
    provider = MagicMock()
    manager._providers["t1"] = provider

    manager.cleanup_provider("t1")

    provider.cleanup.assert_called_once()
    assert manager._providers.get("t1") is None


def test_create_provider_kiro_cli_without_agent_profile_raises():
    """Test Kiro CLI provider requires agent_profile."""
    manager = ProviderManager()
    with pytest.raises(ValueError, match="Kiro CLI provider requires agent_profile parameter"):
        manager.create_provider(
            ProviderType.KIRO_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )


def test_create_provider_claude_code():
    """Test creating Claude Code provider."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.CLAUDE_CODE.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, ClaudeCodeProvider)
    assert manager.get_provider("t1") is provider


def test_get_provider_not_in_database_raises():
    """Test get_provider raises when terminal not found in database."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="Terminal t1 not found in database"):
            manager.get_provider("t1")


def test_peer_terminal_cannot_create_runtime_provider():
    manager = ProviderManager()

    with pytest.raises(ValueError, match="Unknown provider type: peer"):
        manager.create_provider("peer", "deadbeef", "__peers__", "driver")


def test_peer_terminal_cannot_restore_runtime_provider():
    manager = ProviderManager()
    metadata = {
        "provider": "peer",
        "tmux_session": "__peers__",
        "tmux_window": "driver",
        "agent_profile": None,
    }

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value=metadata,
        ),
        pytest.raises(ValueError, match="Unknown provider type: peer"),
    ):
        manager.get_provider("deadbeef")


def test_cleanup_provider_handles_exception():
    """Test cleanup_provider handles exceptions gracefully."""
    manager = ProviderManager()
    provider = MagicMock()
    provider.cleanup.side_effect = Exception("Cleanup failed")
    manager._providers["t1"] = provider

    # Should not raise
    manager.cleanup_provider("t1")

    provider.cleanup.assert_called_once()
    # Provider should still be removed even if cleanup fails
    assert manager._providers.get("t1") is None


def test_cleanup_provider_nonexistent_terminal():
    """Test cleanup_provider with nonexistent terminal."""
    manager = ProviderManager()

    # Should not raise
    manager.cleanup_provider("nonexistent")


def test_list_providers():
    """Test list_providers returns correct mapping."""
    from cli_agent_orchestrator.providers.codex import CodexProvider

    manager = ProviderManager()
    manager.create_provider(
        ProviderType.CODEX.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )
    manager.create_provider(
        ProviderType.CLAUDE_CODE.value,
        terminal_id="t2",
        tmux_session="s2",
        tmux_window="w2",
        agent_profile=None,
    )

    result = manager.list_providers()

    assert result == {
        "t1": "CodexProvider",
        "t2": "ClaudeCodeProvider",
    }


def test_get_provider_restores_shell_baseline_from_metadata():
    """get_provider sets shell_baseline on the provider when DB metadata has shell_command."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.KIRO_CLI.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": "developer",
            "shell_command": "bash",
            "provider_initialized": True,
        },
    ):
        provider = manager.get_provider("t1")

    assert provider.shell_baseline == "bash"


def test_get_provider_marks_kiro_initialized_on_restore():
    """Restoration path must set _initialized=True so KiroCliProvider's
    post-launch shell-baseline IDLE check trusts the restored baseline.

    Without this, a terminal restored from the DB after cao-server restart
    would have shell_baseline set but _initialized=False, and get_status()
    would report PROCESSING indefinitely once kiro exited back to the shell.
    """
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.KIRO_CLI.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": "developer",
            "shell_command": "zsh",
            "provider_initialized": True,
        },
    ):
        provider = manager.get_provider("t1")

    assert provider.shell_baseline == "zsh"
    assert provider._initialized is True


def test_get_provider_no_shell_baseline_when_metadata_missing_shell_command():
    """get_provider leaves shell_baseline as None when DB metadata has no shell_command."""
    manager = ProviderManager()

    with patch(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        return_value={
            "provider": ProviderType.KIRO_CLI.value,
            "tmux_session": "s1",
            "tmux_window": "w1",
            "agent_profile": "developer",
            "provider_initialized": True,
        },
    ):
        provider = manager.get_provider("t1")

    assert provider.shell_baseline is None
    assert provider._initialized is True


def test_create_provider_mock_cli_stores_mapping():
    """The credentials-free mock_cli provider branch (test/CI infra) is wired
    through create_provider and stored in the terminal->provider mapping."""
    from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.MOCK_CLI.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
    )

    assert isinstance(provider, MockCliProvider)
    assert manager.get_provider("t1") is provider

"""Unit tests for ProviderManager."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.hermes import HermesProvider
from cli_agent_orchestrator.providers.kiro_capabilities import KiroPhase0KASError
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.providers.omp import OmpProvider


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


def test_create_provider_grok_forwards_launch_configuration():
    manager = ProviderManager()
    provider = MagicMock()

    with patch(
        "cli_agent_orchestrator.providers.manager.GrokCliProvider", return_value=provider
    ) as provider_cls:
        result = manager.create_provider(
            ProviderType.GROK_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile="reviewer",
            allowed_tools=["fs_read", "fs_list"],
            skill_prompt="runtime skill catalog",
            model="grok-4.5",
        )

    assert result is provider
    assert manager.get_provider("t1") is provider
    provider_cls.assert_called_once_with(
        "t1",
        "s1",
        "w1",
        "reviewer",
        ["fs_read", "fs_list"],
        skill_prompt="runtime skill catalog",
        model="grok-4.5",
    )


def test_create_provider_minimax_code_forwards_launch_configuration():
    from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider

    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.MINIMAX_CODE.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile="developer",
        allowed_tools=["fs_read", "fs_list"],
        skill_prompt="runtime skill catalog",
    )

    assert isinstance(provider, MiniMaxCodeProvider)
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


def test_get_provider_restores_global_skill_catalog_without_assigned_repository():
    """A terminal without an explicit cwd keeps the same global catalog after restart."""
    manager = ProviderManager()
    profile = MagicMock(skills=["python-design-patterns"])

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.CODEX.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "reviewer",
                "allowed_tools": ["fs_read"],
                "working_directory": None,
                "provider_initialized": True,
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.load_agent_profile",
            return_value=profile,
        ) as mock_load_profile,
        patch(
            "cli_agent_orchestrator.providers.manager.build_skill_catalog",
            return_value="## Available Skills\n\n- python-design-patterns",
        ) as mock_catalog,
    ):
        provider = manager.get_provider("t1")

    assert "python-design-patterns" in provider._skill_prompt
    mock_load_profile.assert_called_once_with("reviewer", start=None)
    mock_catalog.assert_called_once_with(["python-design-patterns"], start=None)


def test_get_provider_restores_real_repository_profile_from_assigned_worktree(
    tmp_path, monkeypatch
):
    """Restore must not depend on cao-server's unrelated process cwd."""
    repo = tmp_path / "assigned-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    profile_dir = repo / ".cao" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "repo-reviewer.md").write_text(
        "---\n"
        "name: repo-reviewer\n"
        "description: Repository reviewer\n"
        "provider: kimi_cli\n"
        "permissionMode: plan\n"
        "skills:\n"
        "  - repository-review\n"
        "---\n"
        "Review only.\n"
    )
    skill_dir = repo / ".cao" / "skills" / "repository-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: repository-review\n"
        "description: Assigned worktree review protocol\n"
        "---\n"
        "Review the assigned worktree.\n"
    )
    (repo / ".cao" / "settings.json").write_text('{"skills":{"extra_dirs":[".cao/skills"]}}')
    unrelated = tmp_path / "daemon-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    manager = ProviderManager()
    restored_provider = MagicMock()

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.KIMI_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "repo-reviewer",
                "allowed_tools": [],
                "working_directory": str(repo),
                "provider_initialized": True,
            },
        ),
        patch.object(manager, "create_provider", return_value=restored_provider) as mock_create,
    ):
        assert manager.get_provider("t1") is restored_provider

    skill_prompt = mock_create.call_args.kwargs["skill_prompt"]
    assert "Assigned worktree review protocol" in skill_prompt


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
    # Preserve the provider as a retry handle; silently dropping it would make
    # a deferred Grok private-home cleanup permanent.
    assert manager._providers["t1"] is provider


def test_cleanup_provider_retains_retryable_provider_until_cleanup_succeeds():
    manager = ProviderManager()
    provider = MagicMock()
    provider.cleanup.side_effect = [False, True]
    manager._providers["grok-terminal"] = provider

    assert manager.cleanup_provider("grok-terminal") is False
    assert manager._providers["grok-terminal"] is provider

    assert manager.cleanup_provider("grok-terminal") is True
    assert "grok-terminal" not in manager._providers


def test_cleanup_provider_nonexistent_terminal():
    """Test cleanup_provider with nonexistent terminal."""
    manager = ProviderManager()

    # Should not raise
    manager.cleanup_provider("nonexistent")


def test_cleanup_provider_recovers_grok_home_after_restart():
    """A restart leaves no provider map entry but terminal metadata survives."""
    manager = ProviderManager()
    cleanup_only_provider = MagicMock()
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.GROK_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "developer",
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.GrokCliProvider",
            return_value=cleanup_only_provider,
        ) as provider_cls,
    ):
        manager.cleanup_provider("restored-grok")

    provider_cls.assert_called_once_with("restored-grok", "s1", "w1", "developer")
    cleanup_only_provider.cleanup.assert_called_once()


def test_cleanup_provider_recovers_minimax_code_data_after_restart():
    manager = ProviderManager()
    cleanup_only_provider = MagicMock()
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.MINIMAX_CODE.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "developer",
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.MiniMaxCodeProvider",
            return_value=cleanup_only_provider,
        ) as provider_cls,
    ):
        manager.cleanup_provider("restored-mcode")

    provider_cls.assert_called_once_with("restored-mcode", "s1", "w1", "developer")
    cleanup_only_provider.cleanup.assert_called_once()


def test_cleanup_provider_retains_restored_grok_metadata_when_cleanup_is_deferred():
    """After restart, the DB row remains the retry handle for a private home."""

    manager = ProviderManager()
    cleanup_only_provider = MagicMock()
    cleanup_only_provider.cleanup.return_value = False
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.GROK_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "developer",
            },
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.GrokCliProvider",
            return_value=cleanup_only_provider,
        ),
    ):
        assert manager.cleanup_provider("restored-grok") is False


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


def test_get_provider_rejects_persisted_kas_before_provider_construction():
    """A persisted KAS terminal is never restored as a runnable provider."""
    manager = ProviderManager()

    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": ProviderType.KIRO_CLI.value,
                "tmux_session": "s1",
                "tmux_window": "w1",
                "agent_profile": "developer",
                "engine": "kas",
            },
        ),
        patch("cli_agent_orchestrator.providers.manager.KiroCliProvider") as provider_class,
    ):
        with pytest.raises(KiroPhase0KASError, match="Cedar"):
            manager.get_provider("t1")

    provider_class.assert_not_called()
    assert "t1" not in manager._providers


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


def test_create_provider_omp_stores_mapping():
    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.OMP.value,
        terminal_id="t1",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile="developer",
        skill_prompt="skill catalog",
    )

    assert isinstance(provider, OmpProvider)
    assert manager.get_provider("t1") is provider


def test_create_provider_resume_session_id_reaches_claude_code():
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    manager = ProviderManager()
    provider = manager.create_provider(
        ProviderType.CLAUDE_CODE.value,
        terminal_id="t-resume",
        tmux_session="s1",
        tmux_window="w1",
        agent_profile=None,
        resume_session_id="11d55034-bb41-46ca-8686-59a9dbff16b5",
    )

    assert isinstance(provider, ClaudeCodeProvider)
    assert provider._resume_session_id == "11d55034-bb41-46ca-8686-59a9dbff16b5"


def test_create_provider_resume_session_id_rejected_for_other_providers():
    """resume_session_id is claude_code-only; other providers must fail closed
    instead of silently starting a fresh conversation."""
    manager = ProviderManager()
    with pytest.raises(Exception, match="resume_session_id is only supported"):
        manager.create_provider(
            ProviderType.CODEX.value,
            terminal_id="t-bad",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
            resume_session_id="11d55034-bb41-46ca-8686-59a9dbff16b5",
        )

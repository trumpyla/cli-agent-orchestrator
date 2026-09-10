"""Restricted Codex launches resolve declared skills from the assigned repository."""

import json
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.utils.skills import InlineSkillPrompt

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


@pytest.mark.parametrize("mode", ["plan", "acceptEdits"])
@pytest.mark.parametrize("declared", ["cao-worker-protocols", "missing-skill"])
def test_direct_codex_constructor_resolves_real_declared_profile(
    tmp_path, monkeypatch, mode, declared
):
    from cli_agent_orchestrator.providers.codex import CodexProvider, ProviderError
    from cli_agent_orchestrator.utils.skills import load_skill_content

    store = tmp_path / "agents"
    store.mkdir()
    (store / "inline-direct.md").write_text(
        "---\nname: inline-direct\ndescription: Direct reviewer\nprovider: codex\n"
        f"permissionMode: {mode}\nskills:\n  - {declared}\n---\nReview the task.\n"
    )
    bundled = Path(ts.__file__).resolve().parents[1] / "skills"
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", bundled)
    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.chdir(tmp_path)
    provider = CodexProvider(
        "direct",
        "s",
        "w",
        agent_profile="inline-direct",
        skill_prompt="Use mcp__cao-mcp-server__load_skill to load the catalog.",
    )
    if declared == "missing-skill":
        with pytest.raises(ProviderError, match="resolve declared Codex worker skills"):
            provider._build_codex_command()
        assert not provider._developer_instructions_file_path().exists()
    else:
        command = provider._build_codex_command()
        instructions = tomllib.loads(
            "instructions = " + provider._developer_instructions_file_path().read_text()
        )["instructions"]
        assert load_skill_content(declared) in instructions
        assert "mcp__cao-mcp-server__load_skill" not in instructions
        assert "--ask-for-approval never" in command
        assert "--yolo" not in command
        provider.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "acceptEdits"])
@pytest.mark.parametrize("declared", ["repository-checks", "missing-skill"])
async def test_real_repository_profile_preloads_declared_skills(
    tmp_path, monkeypatch, mode, declared
):
    repo = tmp_path / "assigned"
    profile_dir = repo / ".cao" / "agents"
    profile_dir.mkdir(parents=True)
    (repo / ".git").mkdir()
    (profile_dir / "inline-test-profile.md").write_text(
        "---\nname: inline-test-profile\ndescription: Test reviewer\nprovider: codex\n"
        f"permissionMode: {mode}\nskills:\n  - {declared}\n---\nReview this repository.\n"
    )
    (repo / ".cao" / "settings.json").write_text(json.dumps({"skills": {"extra_dirs": ["skills"]}}))
    skill = repo / "skills" / "repository-checks"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: repository-checks\ndescription: Assigned repository checks\n---\n"
        "Review every changed entry and report concrete findings.\n"
    )
    unrelated = tmp_path / "daemon"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", tmp_path / "agents"
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    backend = MagicMock()
    backend.session_exists.return_value = False
    provider = AsyncMock()
    with (
        patch("cli_agent_orchestrator.backends.registry._backend", backend),
        patch.object(ts, "provider_manager") as manager,
        patch.object(ts, "db_create_terminal"),
        patch.object(ts, "delete_terminals_by_session"),
        patch.object(ts, "status_monitor"),
        patch.object(ts, "fifo_manager"),
        patch.object(ts, "FIFO_DIR", MagicMock()),
        patch.object(ts, "TERMINAL_LOG_DIR", MagicMock()),
        patch.object(ts, "build_skill_catalog") as catalog,
    ):
        manager.create_provider.return_value = provider
        if declared == "missing-skill":
            with pytest.raises(FileNotFoundError):
                await ts.create_terminal(
                    "codex", "inline-test-profile", new_session=True, working_directory=str(repo)
                )
            backend.create_session.assert_not_called()
            manager.create_provider.assert_not_called()
        else:
            await ts.create_terminal(
                "codex", "inline-test-profile", new_session=True, working_directory=str(repo)
            )
            prompt = manager.create_provider.call_args.kwargs["skill_prompt"]
            assert isinstance(prompt, InlineSkillPrompt)
            assert "Review every changed entry and report concrete findings." in prompt
            assert "load_skill" not in prompt
        catalog.assert_not_called()

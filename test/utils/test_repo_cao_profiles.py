"""Behavioral contract for the generic repository-owned CAO swarm profiles."""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = REPO_ROOT / ".cao" / "agents"
PROJECT_SETTINGS = REPO_ROOT / ".cao" / "settings.json"

SUPERVISORS = {
    "cao-repo-supervisor-sol": ("codex", "gpt-5.6-sol"),
    "cao-repo-execution-supervisor-sol": ("codex", "gpt-5.6-sol"),
}
DESIGN = {
    "cao-repo-design-claude-opus5": ("claude_code", "claude-opus-5"),
    "cao-repo-design-agy-pro": ("antigravity_cli", "Gemini 3.1 Pro (High)"),
    "cao-repo-design-kimi-k3": ("kimi_cli", "kimi-code/k3"),
}
IMPLEMENTATION = {
    "cao-repo-implement-codex-sol": ("codex", "gpt-5.6-sol"),
    "cao-repo-implement-claude-opus5": ("claude_code", "claude-opus-5"),
    "cao-repo-implement-agy-pro": (
        "antigravity_cli",
        "Gemini 3.1 Pro (High)",
    ),
    "cao-repo-implement-kimi-k3": ("kimi_cli", "kimi-code/k3"),
}
TESTING = {
    "cao-repo-test-codex-sol": ("codex", "gpt-5.6-sol"),
    "cao-repo-test-agy-pro": ("antigravity_cli", "Gemini 3.1 Pro (High)"),
}
REVIEW = {
    "cao-repo-review-codex-sol": ("codex", "gpt-5.6-sol"),
    "cao-repo-review-claude-opus5": ("claude_code", "claude-opus-5"),
    "cao-repo-review-agy-pro": ("antigravity_cli", "Gemini 3.1 Pro (High)"),
    "cao-repo-review-kimi-k3": ("kimi_cli", "kimi-code/k3"),
}
ALL = SUPERVISORS | DESIGN | IMPLEMENTATION | TESTING | REVIEW

COMMON_MCPS = {
    "cao-mcp-server",
    "context7",
    "tavily",
    "gemini-search",
    "duckduckgo",
    "serena",
}
PYTHON_SKILLS = {
    "python-type-safety",
    "python-design-patterns",
    "python-error-handling",
    "python-resource-management",
    "async-python-patterns",
    "python-testing-patterns",
    "python-code-style",
    "python-anti-patterns",
}


def test_repository_registers_portable_artagon_python_skill_directory() -> None:
    settings = json.loads(PROJECT_SETTINGS.read_text())
    extra_dirs = settings["skills"]["extra_dirs"]

    assert extra_dirs == ["${CAO_ARTAGON_PYTHON_SKILLS_DIR}"]
    assert all(not path.startswith("/") for path in extra_dirs)
    assert "/Users/" not in PROJECT_SETTINGS.read_text()


def _read(name: str) -> tuple[AgentProfile, str]:
    document = frontmatter.load(PROFILE_DIR / f"{name}.md")
    metadata = dict(document.metadata)
    metadata["system_prompt"] = document.content.strip()
    return AgentProfile(**metadata), document.content


def test_exact_generic_profile_topology_and_models() -> None:
    names = {path.stem for path in PROFILE_DIR.glob("*.md")}

    assert names == set(ALL)
    for name, (provider, model) in ALL.items():
        profile, _ = _read(name)
        assert profile.provider == provider
        assert profile.model == model
        assert "embed-cao-ops" not in profile.description


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_profile_has_portable_mcp_and_navigation_guidance(name: str) -> None:
    profile, prompt = _read(name)

    expected_mcps = COMMON_MCPS | ({"cao-ops"} if name in SUPERVISORS else set())
    assert set(profile.mcpServers or {}) == expected_mcps
    assert profile.mcpServers["cao-mcp-server"]["command"] == "cao-mcp-server"
    assert profile.mcpServers["serena"]["url"] == "${CAO_SERENA_MCP_URL}"
    assert "/Users/" not in (PROFILE_DIR / f"{name}.md").read_text()
    assert "review-verification-protocol" not in prompt
    assert "Serena" in prompt
    assert "`sg`" in prompt
    assert "Context7" in prompt
    assert "Tavily" in prompt
    assert "Gemini Search" in prompt
    assert "DuckDuckGo" in prompt


@pytest.mark.parametrize("name", sorted(SUPERVISORS))
def test_supervisors_own_worker_lifecycle_without_write_tools(name: str) -> None:
    profile, prompt = _read(name)

    assert profile.role == "supervisor"
    assert "cao-supervisor-protocols" in (profile.skills or [])
    assert "cao-worker-protocols" not in (profile.skills or [])
    assert "fs_write" not in (profile.allowedTools or [])
    assert profile.permissionMode == "plan"
    assert profile.mcpServers["cao-ops"] == {
        "type": "http",
        "url": "http://127.0.0.1:9889/mcp/ops",
    }
    assert "worker lifecycle" in prompt
    assert "CAO Ops" in prompt
    assert "implementation verifier" in prompt.lower()


@pytest.mark.parametrize("name", sorted(DESIGN | TESTING | REVIEW))
def test_read_only_workers_use_plan_mode_and_worker_protocol(name: str) -> None:
    profile, _ = _read(name)

    assert profile.role == "reviewer"
    assert profile.permissionMode == "plan"
    assert "fs_write" not in (profile.allowedTools or [])
    assert "cao-worker-protocols" in (profile.skills or [])
    assert "cao-supervisor-protocols" not in (profile.skills or [])


@pytest.mark.parametrize("name", sorted(IMPLEMENTATION))
def test_implementation_workers_receive_scoped_write_and_execute(name: str) -> None:
    profile, prompt = _read(name)

    assert profile.role == "developer"
    assert {"fs_write", "execute_bash"} <= set(profile.allowedTools or [])
    assert "cao-worker-protocols" in (profile.skills or [])
    if profile.provider in {"codex", "claude_code", "antigravity_cli"}:
        assert profile.permissionMode == "acceptEdits"
    else:
        assert profile.permissionMode is None
    assert "assigned worktree" in prompt
    assert "tests first" in prompt.lower()


@pytest.mark.parametrize("name", sorted(ALL))
def test_profiles_scope_python_quality_skills(name: str) -> None:
    profile, _ = _read(name)

    assert PYTHON_SKILLS <= set(profile.skills or [])

# ABOUTME: Behavioral tests for the repository-owned CAO swarm profiles in .cao/agents.
# ABOUTME: Pins OpenSpec change embed-cao-ops-streamable-http tasks 6.1-6.2 (profile
# ABOUTME: discovery, protocol-skill separation, Python skill scoping, managed MCP
# ABOUTME: surfaces, read-only lane modes, and fail-closed ${ENV} URL references).
"""Behavioral tests for the repository-owned swarm profiles (``.cao/agents``).

These tests exercise the *committed* artifacts through the real CAO runtime
surfaces — ``list_agent_profiles`` / ``load_agent_profile`` discovery,
``build_skill_catalog`` skill scoping, ``resolve_allowed_tools`` tool
resolution, ``resolve_mcp_server_config`` command/HTTP translation, and the
Kimi provider's plan-mode command builder — with the project settings file
(``.cao/settings.json``) applied to an isolated settings store.
"""

import json
import re
from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import env as env_utils
from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles, load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.skills import build_skill_catalog, load_skill_metadata
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CAO_DIR = _REPO_ROOT / ".cao"
_PROJECT_SETTINGS_FILE = _CAO_DIR / "settings.json"
_AGENTS_DIR = _CAO_DIR / "agents"
_REPO_SKILLS_DIR = _REPO_ROOT / "skills"

# Write/execution capabilities a read-only lane must never resolve to.
_WRITE_CAPABILITIES = {"fs_write", "fs_*", "execute_bash"}

# Managed HTTP MCP surfaces every swarm profile must carry. Values are exact
# ${ENV_NAME} references (fail-closed at launch; never literal, never secrets).
EXPECTED_HTTP_MCP = {
    "context7": "${CAO_CONTEXT7_MCP_URL}",
    "tavily": "${CAO_TAVILY_MCP_URL}",
    "gemini-search": "${CAO_GEMINI_SEARCH_MCP_URL}",
    "duckduckgo": "${CAO_DUCKDUCKGO_MCP_URL}",
    "serena": "${CAO_SERENA_MCP_URL}",
}

MODELS = ["codex-sol", "claude-opus", "agy-pro-high", "kimi-k3"]

SUPERVISOR_PROFILES = [f"{m}-supervisor" for m in MODELS]
WORKER_PROFILES = [
    f"{m}-{r}"
    for m in MODELS
    for r in ["designer", "implementer", "tester", "researcher", "adversarial", "shell"]
]
ALL_PROFILES = SUPERVISOR_PROFILES + WORKER_PROFILES

# Lanes that must launch with no native write tools.
READ_ONLY_PROFILES = [
    f"{m}-{r}"
    for m in MODELS
    for r in ["designer", "tester", "researcher", "adversarial"]
]

IMPLEMENTER_PROFILES = [f"{m}-implementer" for m in MODELS]

# Pinned provider/model per profile.
EXPECTED_PROVIDER_MODEL = {}
for m in MODELS:
    provider = "codex" if "codex" in m else "claude_code" if "claude" in m else "antigravity_cli" if "agy" in m else "kimi_cli"
    model_name = "gpt-5.6-sol" if "codex" in m else "opus" if "claude" in m else "Gemini 3.1 Pro (High)" if "agy" in m else "kimi-code/k3"
    for r in ["supervisor", "designer", "implementer", "tester", "researcher", "adversarial", "shell"]:
        EXPECTED_PROVIDER_MODEL[f"{m}-{r}"] = (provider, model_name)

# Python lane skills
PYTHON_LANE_SKILLS = {}
for m in MODELS:
    PYTHON_LANE_SKILLS[f"{m}-designer"] = {"python-design-patterns", "python-anti-patterns"}
    PYTHON_LANE_SKILLS[f"{m}-implementer"] = {
        "python-type-safety",
        "python-design-patterns",
        "python-error-handling",
        "python-resource-management",
        "async-python-patterns",
        "python-code-style",
        "python-anti-patterns",
        "python-testing-patterns",
    }
    PYTHON_LANE_SKILLS[f"{m}-tester"] = {"python-testing-patterns", "python-anti-patterns"}
    PYTHON_LANE_SKILLS[f"{m}-adversarial"] = {
        "python-anti-patterns",
        "python-testing-patterns",
    }
FORBIDDEN_SKILLS = {"review-verification-protocol"}


def _load_project_settings() -> dict:
    """Read the committed project-local CAO settings file."""
    return json.loads(_PROJECT_SETTINGS_FILE.read_text())


def _extra_dirs(section: str) -> list:
    """Return the configured extra dirs for a section (``agents``/``skills``)."""
    settings = _load_project_settings()
    dirs = settings.get(section, {}).get("extra_dirs", [])
    assert isinstance(dirs, list) and dirs, f"settings missing {section}.extra_dirs"
    return dirs


def _rebase_dir(entry: str) -> str:
    """Map a committed absolute extra-dir entry onto the current checkout.

    Project settings are machine-local absolute paths; a profile dir committed
    at ``<any checkout>/.cao/agents`` or the repo ``skills/`` dir is rebased to
    this test's repo root so the behavioral assertions exercise the committed
    files regardless of where the worktree lives. External directories (the
    Artagon skill checkout) are returned unchanged.
    """
    for suffix in ("/.cao/agents", "/skills"):
        if entry.endswith(suffix) and "cli-agent-orchestrator" in entry:
            rebased = _REPO_ROOT / suffix.lstrip("/")
            return str(rebased)
    return entry


@pytest.fixture()
def project_settings(tmp_path, monkeypatch):
    """Apply the committed project settings to an isolated CAO settings store.

    Also points the managed env file at a nonexistent path so ``${ENV}``
    references in profiles survive loading verbatim (fail-closed precondition).
    """
    settings = _load_project_settings()
    for section in ("agents", "skills"):
        if section in settings and isinstance(settings[section], dict):
            dirs = settings[section].get("extra_dirs")
            if isinstance(dirs, list):
                settings[section]["extra_dirs"] = [_rebase_dir(d) for d in dirs]
    isolated = tmp_path / "settings.json"
    isolated.write_text(json.dumps(settings))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", isolated)
    monkeypatch.setattr(env_utils, "CAO_ENV_FILE", tmp_path / "nonexistent-cao-env")
    return settings


def _artagon_skill_dirs() -> list:
    """External (Artagon) skill dirs from project settings that exist locally."""
    return [d for d in _extra_dirs("skills") if "artagon" in d and Path(d).is_dir()]


# =============================================================================
# Project settings file
# =============================================================================


class TestProjectSettings:
    def test_settings_file_is_valid_json_with_required_keys(self):
        settings = _load_project_settings()
        agents = settings.get("agents", {})
        skills = settings.get("skills", {})
        assert isinstance(agents.get("extra_dirs"), list) and agents["extra_dirs"]
        assert isinstance(skills.get("extra_dirs"), list) and skills["extra_dirs"]

    def test_agents_extra_dir_registers_committed_profile_dir(self):
        entries = _extra_dirs("agents")
        assert any(
            entry.endswith("/.cao/agents") for entry in entries
        ), f"agents.extra_dirs must register the committed .cao/agents dir: {entries}"

    def test_skills_extra_dir_registers_artagon_python_skills(self):
        entries = _extra_dirs("skills")
        assert any(
            "artagon" in entry and entry.rstrip("/").endswith("skills") for entry in entries
        ), f"skills.extra_dirs must register the Artagon Python skill directory: {entries}"


# =============================================================================
# Profile discovery (through the real list/load runtime path)
# =============================================================================


class TestProfileDiscovery:
    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_profile_is_discoverable_and_loadable(self, project_settings, name):
        discovered = {p["name"]: p for p in list_agent_profiles()}
        assert name in discovered, f"{name} not discovered; have: {sorted(discovered)}"
        assert discovered[name]["source"] == "custom"
        assert discovered[name]["loadable"] is True

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_profile_loads_with_matching_frontmatter(self, project_settings, name):
        profile = load_agent_profile(name)
        assert profile.name == name
        assert profile.description
        assert profile.system_prompt

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_profile_committed_as_flat_markdown(self, name):
        assert (_AGENTS_DIR / f"{name}.md").is_file()

    @pytest.mark.parametrize("name,expected", EXPECTED_PROVIDER_MODEL.items())
    def test_provider_and_model_pinning(self, project_settings, name, expected):
        profile = load_agent_profile(name)
        assert (profile.provider, profile.model) == expected


# =============================================================================
# Protocol skill separation and Python skill scoping
# =============================================================================


class TestSkillScoping:
    @pytest.mark.parametrize("name", SUPERVISOR_PROFILES)
    def test_supervisor_receives_supervisor_protocols_only(self, project_settings, name):
        profile = load_agent_profile(name)
        assert profile.skills == ["cao-supervisor-protocols"]

    @pytest.mark.parametrize("name", WORKER_PROFILES)
    def test_workers_receive_worker_protocols(self, project_settings, name):
        profile = load_agent_profile(name)
        assert "cao-worker-protocols" in profile.skills
        assert "cao-supervisor-protocols" not in profile.skills

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_stale_review_protocol_skill_is_not_advertised(self, project_settings, name):
        profile = load_agent_profile(name)
        assert FORBIDDEN_SKILLS.isdisjoint(profile.skills or [])

    @pytest.mark.parametrize("name,expected", PYTHON_LANE_SKILLS.items())
    def test_python_skills_scoped_per_lane(self, project_settings, name, expected):
        profile = load_agent_profile(name)
        skills = set(profile.skills or [])
        assert expected <= skills
        assert skills == expected | {"cao-worker-protocols"}

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_catalog_advertises_only_assigned_skills(self, project_settings, name):
        if not _artagon_skill_dirs():
            pytest.skip("Artagon Python skill directory not present on this machine")
        profile = load_agent_profile(name)
        catalog = build_skill_catalog(profile.skills)
        advertised = set(re.findall(r"- \*\*([a-z0-9-]+)\*\*:", catalog))
        assert advertised == set(
            profile.skills
        ), f"{name} catalog {sorted(advertised)} != assigned {sorted(profile.skills)}"

    @pytest.mark.parametrize(
        "skill",
        sorted(set().union(*PYTHON_LANE_SKILLS.values())),
    )
    def test_assigned_python_skills_resolve(self, project_settings, skill):
        if not _artagon_skill_dirs():
            pytest.skip("Artagon Python skill directory not present on this machine")
        metadata = load_skill_metadata(skill)
        assert metadata.name == skill


# =============================================================================
# Required MCP surfaces: stdio cao-mcp-server + managed HTTP entries
# =============================================================================


class TestMcpSurfaces:
    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_profile_has_exactly_the_required_mcp_entries(self, project_settings, name):
        profile = load_agent_profile(name)
        assert set(profile.mcpServers) == {"cao-mcp-server", *EXPECTED_HTTP_MCP}

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_cao_mcp_server_is_stdio_command_entry(self, project_settings, name):
        entry = load_agent_profile(name).mcpServers["cao-mcp-server"]
        assert entry.get("type", "stdio") == "stdio"
        assert entry.get("command") == "cao-mcp-server"
        assert "url" not in entry

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_http_entries_are_exact_env_references_without_subprocess_fields(
        self, project_settings, name
    ):
        servers = load_agent_profile(name).mcpServers
        for server, reference in EXPECTED_HTTP_MCP.items():
            entry = servers[server]
            assert entry.get("type") == "http", f"{name}:{server} type"
            assert entry.get("url") == reference, f"{name}:{server} url"
            assert "command" not in entry and "args" not in entry and "env" not in entry

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_command_and_http_entries_survive_translation(self, project_settings, name):
        """Command and HTTP coexistence: stdio stays a command entry; HTTP stays HTTP."""
        servers = load_agent_profile(name).mcpServers
        stdio = resolve_mcp_server_config(dict(servers["cao-mcp-server"]))
        assert stdio["command"], "stdio entry must keep a resolvable command"
        for server in EXPECTED_HTTP_MCP:
            original = dict(servers[server])
            resolved = resolve_mcp_server_config(original)
            assert resolved == original, f"{name}:{server} HTTP entry mutated: {resolved}"

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_env_url_references_survive_profile_load_verbatim(self, project_settings, name):
        """Fail-closed precondition: with the managed env unset, references are
        preserved exactly so launch-time resolution can fail closed instead of
        silently substituting a stale or partial value."""
        servers = load_agent_profile(name).mcpServers
        for server, reference in EXPECTED_HTTP_MCP.items():
            assert servers[server]["url"] == reference

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_no_literal_urls_or_secrets_in_profiles(self, name):
        text = (_AGENTS_DIR / f"{name}.md").read_text()
        mcp_section = text.split("---", 2)[1]
        assert "http://" not in mcp_section and "https://" not in mcp_section
        assert "npx" not in mcp_section


# =============================================================================
# Read-only lanes and implementation write scoping
# =============================================================================


class TestLanePermissions:
    def _resolved_tools(self, name):
        profile = load_agent_profile(name)
        return resolve_allowed_tools(
            profile.allowedTools, profile.role, list(profile.mcpServers or {})
        )

    @pytest.mark.parametrize("name", READ_ONLY_PROFILES)
    def test_read_only_lanes_have_no_write_or_execute_capabilities(self, project_settings, name):
        resolved = self._resolved_tools(name)
        assert "*" not in resolved
        assert _WRITE_CAPABILITIES.isdisjoint(
            resolved
        ), f"{name} resolved write capabilities: {_WRITE_CAPABILITIES & set(resolved)}"

    @pytest.mark.parametrize("name", IMPLEMENTER_PROFILES)
    def test_implementation_lane_carries_write_and_execute(self, project_settings, name):
        resolved = self._resolved_tools(name)
        assert "execute_bash" in resolved
        assert {"fs_write", "fs_*"} & set(resolved)

    @pytest.mark.parametrize("name", SUPERVISOR_PROFILES)
    def test_supervisor_has_no_shell_execute(self, project_settings, name):
        resolved = self._resolved_tools(name)
        assert "execute_bash" not in resolved

    @pytest.mark.parametrize("name", READ_ONLY_PROFILES)
    def test_antigravity_read_only_lanes_use_plan_permission_mode(self, project_settings, name):
        profile = load_agent_profile(name)
        if profile.provider != "antigravity_cli":
            pytest.skip(f"{name} is not an Antigravity lane")
        assert profile.permissionMode == "plan"

    def _kimi_command(self, profile_name):
        resolved = self._resolved_tools(profile_name)
        with pytest.MonkeyPatch.context() as mp:
            # Keep the real ~/.kimi/config.toml untouched.
            mp.setattr(KimiCliProvider, "_ensure_mcp_timeout", classmethod(lambda cls: None))
            provider = KimiCliProvider(
                "term-swarm",
                "session-swarm",
                "window-swarm",
                agent_profile=profile_name,
                allowed_tools=resolved,
            )
            try:
                return provider._build_kimi_command()
            finally:
                provider.cleanup()

    def test_kimi_read_only_lane_enters_plan_mode(self, project_settings):
        command = self._kimi_command("kimi-k3-tester")
        assert "--plan" in command
        assert "--model kimi-code/k3" in command

    def test_kimi_implementation_lane_does_not_enter_plan_mode(self, project_settings):
        command = self._kimi_command("kimi-k3-implementer")
        assert "--plan" not in command
        assert "--model kimi-code/k3" in command


# =============================================================================
# Navigation and managed-MCP usage guidance in profile prompts
# =============================================================================


class TestProfilePrompts:
    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_prompt_requires_serena_and_sg_before_broad_search(self, project_settings, name):
        prompt = load_agent_profile(name).system_prompt.lower()
        assert "serena" in prompt
        assert "sg" in prompt
        assert "symbol" in prompt

    @pytest.mark.parametrize("name", ALL_PROFILES)
    def test_prompt_explains_managed_mcp_usage(self, project_settings, name):
        prompt = load_agent_profile(name).system_prompt.lower()
        for surface in ("context7", "tavily", "gemini", "duckduckgo", "serena"):
            assert surface in prompt, f"{name} prompt is missing {surface} guidance"

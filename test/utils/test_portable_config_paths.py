"""Portable, fail-closed resolution for configured agent and skill roots."""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import agent_profiles, skills
from cli_agent_orchestrator.utils.paths import normalized_path


@pytest.fixture
def portable_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", tmp_path / "local-agents")
    (tmp_path / "local-agents").mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "global-skills")
    (tmp_path / "global-skills").mkdir()
    return home


def _write_profile(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: portable profile\n---\nportable body\n"
    )


def _write_skill(directory: Path, name: str) -> None:
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: portable skill\n---\nportable skill body\n"
    )


def test_tilde_agent_directory_round_trips_and_loads(portable_home: Path) -> None:
    configured = "~/agents"
    _write_profile(portable_home / "agents", "zz-portable-agent")

    assert settings_service.set_extra_agent_dirs([configured]) == [configured]
    assert settings_service.get_extra_agent_dirs() == [configured]
    assert "portable body" in agent_profiles._read_agent_profile_source("zz-portable-agent")
    assert "zz-portable-agent" in {
        profile["name"] for profile in agent_profiles.list_agent_profiles()
    }


def test_legacy_tilde_agent_entry_becomes_active(portable_home: Path) -> None:
    _write_profile(portable_home / "legacy-agents", "zz-legacy-agent")
    settings_service.SETTINGS_FILE.write_text(json.dumps({"extra_agent_dirs": ["~/legacy-agents"]}))

    assert "portable body" in agent_profiles._read_agent_profile_source("zz-legacy-agent")


def test_tilde_skill_directory_round_trips_lists_and_loads(portable_home: Path) -> None:
    configured = "~/skills"
    _write_skill(portable_home / "skills", "zz-portable-skill")

    assert settings_service.set_extra_skill_dirs([configured]) == [configured]
    assert settings_service.get_extra_skill_dirs() == [configured]
    assert "zz-portable-skill" in {skill.name for skill in skills.list_skills()}
    assert skills.load_skill_content("zz-portable-skill") == "portable skill body"


def test_canonical_disabled_matching_preserves_configured_spelling(
    portable_home: Path,
) -> None:
    _write_profile(portable_home / "agents", "zz-disabled-agent")
    settings_service.set_extra_agent_dirs(["~/agents"])

    assert settings_service.set_disabled_agent_dirs([str(portable_home / "agents") + "/"]) == [
        "~/agents"
    ]
    with pytest.raises(FileNotFoundError):
        agent_profiles._read_agent_profile_source("zz-disabled-agent")


def test_absolute_extra_directory_order_remains_stable(portable_home: Path) -> None:
    first = portable_home / "first"
    second = portable_home / "second"
    _write_profile(first, "zz-ordered-agent")
    _write_profile(second, "zz-ordered-agent")
    (first / "zz-ordered-agent.md").write_text(
        "---\nname: zz-ordered-agent\ndescription: first\n---\nfirst body\n"
    )
    (second / "zz-ordered-agent.md").write_text(
        "---\nname: zz-ordered-agent\ndescription: second\n---\nsecond body\n"
    )

    settings_service.set_extra_agent_dirs([str(first), str(second)])

    assert "first body" in agent_profiles._read_agent_profile_source("zz-ordered-agent")


@pytest.mark.parametrize("sensitive_name", [".ssh", ".gnupg", ".aws"])
@pytest.mark.parametrize(
    "setter",
    [
        pytest.param(settings_service.set_extra_agent_dirs, id="agents"),
        pytest.param(settings_service.set_extra_skill_dirs, id="skills"),
    ],
)
def test_sensitive_extra_roots_are_rejected_without_path_disclosure(
    portable_home: Path,
    caplog: pytest.LogCaptureFixture,
    sensitive_name: str,
    setter,
) -> None:
    configured = f"~/{sensitive_name}/private-material"

    with pytest.raises(ValueError, match="sensitive_root") as exc_info:
        setter([configured])

    assert str(portable_home) not in str(exc_info.value)
    assert str(portable_home) not in caplog.text
    assert not settings_service.SETTINGS_FILE.exists()


def test_cao_owned_aws_default_is_allowed() -> None:
    candidate = CAO_HOME_DIR / "agent-store"

    assert normalized_path(candidate) == str(candidate.resolve())

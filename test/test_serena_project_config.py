# ABOUTME: Tests for the shared read-only Serena project config (.serena/project.yml).
# ABOUTME: Pins OpenSpec change embed-cao-ops-streamable-http task 6.3: Python language
# ABOUTME: server, generated/cache/worktree exclusions, gitignore support, read_only.
"""Structural tests for the committed Serena project configuration.

The shared Serena daemon (managed by artagon-scripts ``mcp.sh``) is
navigation-only for this repository: swarm agents use it for symbol-aware
navigation and reference lookups, never for edits. These tests pin the
config file that guarantees that posture.
"""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_YML = _REPO_ROOT / ".serena" / "project.yml"


def _config() -> dict:
    return yaml.safe_load(_PROJECT_YML.read_text())


class TestSerenaProjectConfig:
    def test_project_yml_exists_and_parses(self):
        assert _PROJECT_YML.is_file()
        config = _config()
        assert isinstance(config, dict)
        assert config.get("project_name")

    def test_python_language_server_enabled(self):
        # Serena 1.2.0's project.yml schema key is ``languages`` (the list of
        # language servers to start); ``language_servers`` is not a valid key
        # in the installed schema.
        assert _config().get("languages") == ["python"]

    def test_read_only_mode_enabled(self):
        assert _config().get("read_only") is True

    def test_gitignore_support_enabled(self):
        assert _config().get("ignore_all_files_in_gitignore") is True

    def test_generated_cache_and_worktree_exclusions(self):
        ignored = _config().get("ignored_paths") or []
        assert isinstance(ignored, list)
        required = {
            ".venv",
            ".worktrees",
            "node_modules",
            "htmlcov",
            ".pytest_cache",
            ".mypy_cache",
        }
        missing = {
            pattern for pattern in required if not any(pattern in entry for entry in ignored)
        }
        assert not missing, f"ignored_paths missing exclusions: {sorted(missing)}"

    def test_required_serena_schema_keys_present(self):
        # Keys the Serena 1.2.0 ProjectConfig loader reads directly; a missing
        # key raises KeyError when the daemon loads the project.
        config = _config()
        for key in (
            "project_name",
            "languages",
            "encoding",
            "ignored_paths",
            "read_only",
            "ignore_all_files_in_gitignore",
            "excluded_tools",
            "included_optional_tools",
            "fixed_tools",
            "initial_prompt",
            "added_modes",
            "default_modes",
        ):
            assert key in config, f"project.yml missing required key: {key}"

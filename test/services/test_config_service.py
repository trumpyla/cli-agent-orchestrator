"""Tests for config_service — the unified ConfigService (issue #357).

Covers the precedence chain (env > file > default), legacy config.json +
settings.json migration, and the memory.compile_mode env/file conflict named
in the issue.
"""

import json

import pytest

from cli_agent_orchestrator.services import config_service as cs
from cli_agent_orchestrator.services.config_service import ConfigService


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Redirect both the unified file and the legacy config.json to temp paths.

    Prevents a real dev/CI machine's ``~/.aws/cli-agent-orchestrator/{settings,config}.json``
    from leaking into these tests, and clears every env var this module reads
    so each test starts from a clean slate.
    """
    fake_settings = tmp_path / "settings.json"
    fake_legacy = tmp_path / "config.json"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", fake_settings
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.settings_service.CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(cs, "LEGACY_CONFIG_FILE", fake_legacy)
    for env_name in cs.ENV_REGISTRY:
        monkeypatch.delenv(env_name, raising=False)
    return {"settings": fake_settings, "legacy": fake_legacy}


class TestPrecedence:
    """CLI override > CAO_* env var > settings.json > built-in default."""

    def test_returns_builtin_default_when_nothing_set(self, _isolated_settings):
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_file_value_beats_default(self, _isolated_settings):
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        assert ConfigService.get("terminal.backend") == "herdr"

    def test_env_var_beats_file_value(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "herdr"}}))
        monkeypatch.setenv("CAO_TERMINAL_BACKEND", "tmux")
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_cli_override_beats_env_var(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_BACKEND", "herdr")
        assert ConfigService.get("terminal.backend", override="tmux") == "tmux"

    def test_env_var_beats_default_when_no_file(self, _isolated_settings, monkeypatch):
        monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "true")
        assert ConfigService.get("apps.enabled") is True

    def test_invalid_env_value_falls_back_to_file(self, _isolated_settings, monkeypatch):
        """A malformed env value (e.g. non-numeric int) is ignored, not raised."""
        _isolated_settings["settings"].write_text(
            json.dumps({"server": {"mcp_request_timeout": 45}})
        )
        monkeypatch.setenv("CAO_MCP_REQUEST_TIMEOUT", "not-a-number")
        assert ConfigService.get("server.mcp_request_timeout") == 45


class TestLegacyMigration:
    """On first read, legacy config.json's terminal_backend/herdr_session
    should be folded into the unified settings.json under 'terminal'."""

    def test_migrates_legacy_config_json_into_settings_json(self, _isolated_settings):
        _isolated_settings["legacy"].write_text(
            json.dumps({"terminal_backend": "herdr", "herdr_session": "my-sess"})
        )
        assert ConfigService.get("terminal.backend") == "herdr"
        assert ConfigService.get("terminal.herdr_session") == "my-sess"

        # The migration persisted into settings.json itself.
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"] == {"backend": "herdr", "herdr_session": "my-sess"}

    def test_no_migration_when_settings_json_already_has_terminal_section(self, _isolated_settings):
        """Once migrated (or hand-configured), the legacy file is not re-read."""
        _isolated_settings["settings"].write_text(json.dumps({"terminal": {"backend": "tmux"}}))
        _isolated_settings["legacy"].write_text(json.dumps({"terminal_backend": "herdr"}))
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_missing_legacy_file_is_a_noop(self, _isolated_settings):
        assert not _isolated_settings["legacy"].exists()
        assert ConfigService.get("terminal.backend") == "tmux"

    def test_malformed_legacy_file_falls_back_to_default(self, _isolated_settings):
        _isolated_settings["legacy"].write_text("not valid json {{{")
        assert ConfigService.get("terminal.backend") == "tmux"


class TestMemoryCompileModeConflict:
    """CAO_MEMORY_COMPILE_MODE must win over a conflicting settings.json value."""

    def test_env_var_wins_over_file_value(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(json.dumps({"memory": {"compile_mode": "llm"}}))
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
        assert ConfigService.get("memory.compile_mode") == "append"

    def test_file_value_used_when_no_env_var(self, _isolated_settings):
        _isolated_settings["settings"].write_text(
            json.dumps({"memory": {"compile_mode": "append"}})
        )
        assert ConfigService.get("memory.compile_mode") == "append"

    def test_default_llm_when_neither_set(self, _isolated_settings):
        assert ConfigService.get("memory.compile_mode") == "llm"


class TestGetConfig:
    """ConfigService.get_config() assembles a validated CAOConfig."""

    def test_assembles_full_typed_config(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(
            json.dumps(
                {
                    "terminal": {"backend": "herdr", "herdr_session": "s1"},
                    "server": {"mcp_request_timeout": 99},
                }
            )
        )
        monkeypatch.setenv("CAO_MEMORY_COMPILE_MODE", "append")
        cfg = ConfigService.get_config()
        assert cfg.terminal.backend == "herdr"
        assert cfg.terminal.herdr_session == "s1"
        assert cfg.server.mcp_request_timeout == 99
        assert cfg.memory.compile_mode == "append"
        # Untouched sections keep built-in defaults.
        assert cfg.apps.enabled is False
        assert cfg.logging.level == "INFO"


class TestSetAndPath:
    def test_set_persists_and_get_reads_it_back(self, _isolated_settings):
        ConfigService.set("terminal.backend", "herdr")
        assert ConfigService.get("terminal.backend") == "herdr"
        on_disk = json.loads(_isolated_settings["settings"].read_text())
        assert on_disk["terminal"]["backend"] == "herdr"

    def test_set_agents_extra_dirs_routes_through_settings_service(self, _isolated_settings):
        ConfigService.set("agents.extra_dirs", ["/a", "/b"])
        assert ConfigService.get("agents.extra_dirs") == ["/a", "/b"]

    def test_path_returns_settings_service_settings_file(self, _isolated_settings):
        assert ConfigService.path() == _isolated_settings["settings"]


class TestListAll:
    def test_list_all_includes_known_sections(self, _isolated_settings):
        result = ConfigService.list_all()
        assert "terminal.backend" in result
        assert "memory.compile_mode" in result
        assert "server.mcp_request_timeout" in result
        assert result["terminal.backend"] == "tmux"


class TestCleanupConfig:
    """Strict, merged validation for opt-in completed-session cleanup."""

    def test_defaults_are_typed_and_visible_everywhere(self, _isolated_settings):
        expected = {
            "completed_sessions_enabled": False,
            "completed_session_grace_s": 900,
            "sweep_interval_s": 300,
            "max_sessions_per_sweep": 5,
            "preserve_patterns": [],
        }

        assert ConfigService.get_config().cleanup.model_dump() == expected
        for key, value in expected.items():
            assert ConfigService.get(f"cleanup.{key}") == value
            assert ConfigService.list_all()[f"cleanup.{key}"] == value

    def test_valid_values_round_trip_as_typed_values(self, _isolated_settings):
        configured = {
            "completed_sessions_enabled": True,
            "completed_session_grace_s": 1200,
            "sweep_interval_s": 60,
            "max_sessions_per_sweep": 12,
            "preserve_patterns": ["cao-supervisor-*", "cao-release-?"],
        }

        for key, value in configured.items():
            assert ConfigService.set(f"cleanup.{key}", value) == value

        assert ConfigService.get_config().cleanup.model_dump() == configured

    @pytest.mark.parametrize(
        ("path", "invalid"),
        [
            pytest.param("completed_sessions_enabled", 1, id="bool-from-int"),
            pytest.param("completed_sessions_enabled", "true", id="bool-from-string"),
            pytest.param("completed_session_grace_s", 59, id="grace-too-short"),
            pytest.param("completed_session_grace_s", True, id="grace-from-bool"),
            pytest.param("sweep_interval_s", 29, id="interval-too-short"),
            pytest.param("max_sessions_per_sweep", 0, id="batch-zero"),
            pytest.param("max_sessions_per_sweep", 51, id="batch-too-large"),
        ],
    )
    def test_invalid_scalar_preserves_prior_bytes(
        self,
        _isolated_settings,
        path,
        invalid,
    ):
        prior = b'{\n  "sentinel": "preserve-formatting"\n}\n'
        _isolated_settings["settings"].write_bytes(prior)

        with pytest.raises(ValueError):
            ConfigService.set(f"cleanup.{path}", invalid)

        assert _isolated_settings["settings"].read_bytes() == prior

    @pytest.mark.parametrize(
        "patterns",
        [
            pytest.param(["x"] * 101, id="too-many"),
            pytest.param(["x" * 129], id="entry-too-long"),
            pytest.param(["safe", "contains\ncontrol"], id="control-character"),
        ],
    )
    def test_invalid_preserve_patterns_reject_whole_update(
        self,
        _isolated_settings,
        patterns,
    ):
        prior = b'{"terminal":{"backend":"herdr"}}\n'
        _isolated_settings["settings"].write_bytes(prior)

        with pytest.raises(ValueError):
            ConfigService.set("cleanup.preserve_patterns", patterns)

        assert _isolated_settings["settings"].read_bytes() == prior

    def test_set_validates_fully_merged_section(self, _isolated_settings):
        prior = b'{"cleanup":{"completed_session_grace_s":1}}\n'
        _isolated_settings["settings"].write_bytes(prior)

        with pytest.raises(ValueError):
            ConfigService.set("cleanup.completed_sessions_enabled", True)

        assert _isolated_settings["settings"].read_bytes() == prior

    def test_runtime_read_observes_file_change_without_restart(self, _isolated_settings):
        _isolated_settings["settings"].write_text(
            json.dumps({"cleanup": {"completed_sessions_enabled": True}})
        )
        assert ConfigService.get("cleanup.completed_sessions_enabled") is True

        _isolated_settings["settings"].write_text(
            json.dumps({"cleanup": {"completed_sessions_enabled": False}})
        )
        assert ConfigService.get("cleanup.completed_sessions_enabled") is False

    def test_valid_environment_value_overrides_file(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(
            json.dumps({"cleanup": {"completed_session_grace_s": 1200}})
        )
        monkeypatch.setenv("CAO_CLEANUP_COMPLETED_SESSION_GRACE_S", "1800")

        assert ConfigService.get("cleanup.completed_session_grace_s") == 1800

    def test_invalid_environment_value_falls_back_to_file(
        self,
        _isolated_settings,
        monkeypatch,
    ):
        _isolated_settings["settings"].write_text(
            json.dumps({"cleanup": {"completed_sessions_enabled": True}})
        )
        monkeypatch.setenv("CAO_CLEANUP_COMPLETED_SESSIONS_ENABLED", "sometimes")

        assert ConfigService.get("cleanup.completed_sessions_enabled") is True


class TestMemoryConfigSymmetry:
    """Every documented memory setting is validated on both read and write."""

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            pytest.param("memory.enabled", False, id="enabled"),
            pytest.param("memory.flush_threshold", 0.5, id="flush-threshold"),
            pytest.param("memory.compile_mode", "append", id="compile-mode"),
            pytest.param("memory.compile_timeout_s", 0.25, id="timeout-fractional"),
            pytest.param("memory.compile_timeout_s", 3600.0, id="timeout-upper-bound"),
        ],
    )
    def test_supported_setting_round_trips(self, _isolated_settings, path, value):
        assert ConfigService.set(path, value)[path.split(".", 1)[1]] == value
        assert ConfigService.get(path) == value

    @pytest.mark.parametrize(
        ("path", "invalid"),
        [
            pytest.param("memory.compile_mode", "LLM", id="mode-case"),
            pytest.param("memory.compile_mode", "other", id="mode-unknown"),
            pytest.param("memory.compile_timeout_s", 0, id="timeout-zero"),
            pytest.param("memory.compile_timeout_s", -1, id="timeout-negative"),
            pytest.param("memory.compile_timeout_s", 3600.1, id="timeout-too-large"),
            pytest.param("memory.compile_timeout_s", float("inf"), id="timeout-infinite"),
            pytest.param("memory.compile_timeout_s", float("nan"), id="timeout-nan"),
            pytest.param("memory.compile_timeout_s", True, id="timeout-bool"),
        ],
    )
    def test_invalid_setting_preserves_prior_bytes(
        self,
        _isolated_settings,
        path,
        invalid,
    ):
        prior = b'{"memory":{"project_id":"preserve-extra"}}\n'
        _isolated_settings["settings"].write_bytes(prior)

        with pytest.raises(ValueError):
            ConfigService.set(path, invalid)

        assert _isolated_settings["settings"].read_bytes() == prior

    def test_compile_timeout_environment_precedence(self, _isolated_settings, monkeypatch):
        _isolated_settings["settings"].write_text(
            json.dumps({"memory": {"compile_timeout_s": 30.0}})
        )
        monkeypatch.setenv("CAO_MEMORY_COMPILE_TIMEOUT_S", "45.5")

        assert ConfigService.get("memory.compile_timeout_s") == 45.5

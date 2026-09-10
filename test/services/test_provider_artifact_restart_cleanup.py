"""Real provider restoration keeps teardown debt until terminal artifacts are gone."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.runtime_resource_cleanup import build_runtime_resource_cleanup
from test.services.test_runtime_cleanup_debt import teardown_world

_REAL_DISMANTLE = terminal_service.dismantle_terminal_runtime


@pytest.mark.parametrize("provider_class", [CodexProvider, ClaudeCodeProvider])
def test_artifact_unlink_failure_keeps_initialized_provider(tmp_path, monkeypatch, provider_class):
    monkeypatch.setattr(f"{provider_class.__module__}.CAO_HOME_DIR", tmp_path)
    provider = provider_class("owned", "s", "w")
    provider._initialized = True
    manager = ProviderManager()
    manager._providers["owned"] = provider
    real_unlink = Path.unlink

    def denied(*args, **kwargs):
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(Path, "unlink", denied)
    assert manager.cleanup_provider("owned") is False
    assert manager._providers["owned"] is provider
    assert provider._initialized is True
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert manager.cleanup_provider("owned") is True
    assert provider._initialized is False
    assert "owned" not in manager._providers


@pytest.mark.parametrize(
    "provider_name,suffixes",
    [
        ("codex", [".codex_developer_instructions"]),
        ("claude_code", [".prompt", ".mcp.json"]),
    ],
)
def test_manager_reset_retries_artifact_failure_before_deleting_terminal(
    tmp_path,
    monkeypatch,
    teardown_world,
    provider_name,
    suffixes,
):
    backend, inventory, _, dispatch = teardown_world
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "old").provider = provider_name
        db.commit()
    home = tmp_path / "cao"
    (home / "tmp").mkdir(parents=True)
    paths = [home / "tmp" / f"old{suffix}" for suffix in suffixes]
    for path in paths:
        path.write_text("synthetic provider artifact")
    foreign = home / "tmp" / f"other{suffixes[0]}"
    foreign.write_text("keep")
    monkeypatch.setattr(f"cli_agent_orchestrator.providers.{provider_name}.CAO_HOME_DIR", home)
    manager = ProviderManager()  # no original provider object survives
    monkeypatch.setattr(terminal_service, "provider_manager", manager)
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", _REAL_DISMANTLE)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", MagicMock())
    monkeypatch.setattr(terminal_service.status_monitor, "clear_terminal", MagicMock())
    real_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == paths[0]:
            raise OSError("synthetic removal failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    first = build_runtime_resource_cleanup(backend=backend).sweep()
    assert first.deleted == 0
    assert inventory == []
    assert database.get_terminal_metadata("old") is not None
    assert database.list_runtime_cleanup_debt()
    assert paths[0].exists()
    assert "old" in manager._providers
    assert not any(call.args[1] == "post_kill_session" for call in dispatch.call_args_list)

    # A second daemon has neither the old map nor the restored failed provider.
    monkeypatch.setattr(terminal_service, "provider_manager", ProviderManager())
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = build_runtime_resource_cleanup(backend=backend).sweep()
    assert second.deleted == 1
    assert database.get_terminal_metadata("old") is None
    assert database.list_runtime_cleanup_debt() == []
    assert all(not path.exists() for path in paths)
    assert foreign.read_text() == "keep"
    assert backend.close_workspace_by_id.call_count == 1
    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == 1

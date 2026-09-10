"""Async lifecycle callers await real Antigravity cleanup before deleting rows."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services import flow_service, terminal_service


@pytest.fixture
def broken_ownership(tmp_path, monkeypatch):
    def setup(ambiguous):
        cfg = tmp_path / "mcp_config.json"
        servers = {"local-tid": {"command": "local", "env": {"CAO_TERMINAL_ID": "tid"}}}
        if ambiguous:
            servers["remote-tid"] = {"serverUrl": "https://example.invalid/mcp"}
        cfg.write_text(json.dumps({"mcpServers": servers}))
        Path(f"{cfg}.cao-ownership-journal").write_text("not-json")
        monkeypatch.setattr(AntigravityCliProvider, "_mcp_config_path", lambda self: cfg)
        return cfg

    return setup


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("session_exists", [False, True])
async def test_flow_recycle_awaits_cleanup_and_retains_ambiguous_rows(
    isolated_memory_db,
    tmp_path,
    monkeypatch,
    broken_ownership,
    ambiguous,
    session_exists,
):
    cfg = broken_ownership(ambiguous)
    database.create_terminal("tid", "cao-flow-cleanup", "worker", "antigravity_cli")
    manager = ProviderManager()
    provider = AntigravityCliProvider("tid", "cao-flow-cleanup", "worker")
    provider._initialized = True
    manager._providers["tid"] = provider
    flow_path = tmp_path / "flow.md"
    flow_path.write_text(
        "---\nname: cleanup\nschedule: '* * * * *'\nagent_profile: worker\n---\nTask.\n"
    )
    flow = SimpleNamespace(
        name="cleanup",
        file_path=str(flow_path),
        script="",
        schedule="* * * * *",
        provider="antigravity_cli",
        agent_profile="worker",
        engine=None,
    )
    backend = MagicMock()
    backend.session_exists.return_value = session_exists
    monkeypatch.setattr(flow_service, "get_flow", lambda name: flow)
    monkeypatch.setattr(flow_service, "get_backend", lambda: backend)
    monkeypatch.setattr(flow_service, "_is_terminal_busy", lambda tid: False)
    monkeypatch.setattr(flow_service, "provider_manager", manager)
    monkeypatch.setattr(flow_service, "fifo_manager", MagicMock())
    monkeypatch.setattr(flow_service, "status_monitor", MagicMock())
    monkeypatch.setattr(flow_service, "db_update_flow_run_times", MagicMock())
    create = AsyncMock(return_value=SimpleNamespace(id="new"))
    monkeypatch.setattr(flow_service, "create_terminal", create)
    monkeypatch.setattr(flow_service, "send_input", MagicMock())
    assert await flow_service.execute_flow("cleanup") is not ambiguous
    assert (database.get_terminal_metadata("tid") is not None) is ambiguous
    assert ("tid" in manager._providers) is ambiguous
    assert provider._mcp_cleanup_future is None
    assert "local-tid" not in json.loads(cfg.read_text())["mcpServers"]
    assert create.await_count == (0 if ambiguous else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_create_rollback_awaits_cleanup_and_retains_ambiguous_row(
    isolated_memory_db,
    tmp_path,
    monkeypatch,
    broken_ownership,
    ambiguous,
):
    cfg = broken_ownership(ambiguous)
    manager = ProviderManager()
    backend = MagicMock()
    backend.session_exists.return_value = False
    monkeypatch.setattr(terminal_service, "provider_manager", manager)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "tid")
    monkeypatch.setattr(terminal_service, "generate_window_name", lambda *a, **kw: "worker")
    monkeypatch.setattr(
        terminal_service,
        "load_agent_profile",
        lambda *a, **kw: AgentProfile(name="worker", description="worker"),
    )
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service, "fifo_manager", MagicMock())
    monkeypatch.setattr(terminal_service, "status_monitor", MagicMock())
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        AntigravityCliProvider,
        "initialize",
        AsyncMock(side_effect=RuntimeError("synthetic init failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic init failure"):
        await terminal_service.create_terminal(
            provider="antigravity_cli",
            agent_profile="worker",
            session_name="cao-rollback",
            new_session=True,
            allowed_tools=["*"],
        )
    assert (database.get_terminal_metadata("tid") is not None) is ambiguous
    assert ("tid" in manager._providers) is ambiguous
    assert "local-tid" not in json.loads(cfg.read_text())["mcpServers"]
    if ambiguous:
        assert manager._providers["tid"]._mcp_cleanup_future is None

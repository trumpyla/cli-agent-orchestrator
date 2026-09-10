"""A dispatched flow with uncertain acceptance is not a failed launch or retry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import flow_service
from cli_agent_orchestrator.services.terminal_service import InputAcceptanceUnconfirmedError


@pytest.mark.asyncio
async def test_unconfirmed_dispatch_preserves_worker_and_normal_recurrence(
    isolated_memory_db,
    tmp_path,
    monkeypatch,
    caplog,
):
    flow_path = tmp_path / "flow.md"
    flow_path.write_text(
        "---\nname: uncertainty\nschedule: '* * * * *'\nagent_profile: worker\n---\nTask.\n"
    )
    flow = SimpleNamespace(
        name="uncertainty",
        file_path=str(flow_path),
        script="",
        schedule="* * * * *",
        provider="kimi_cli",
        agent_profile="worker",
        engine=None,
    )
    monkeypatch.setattr(flow_service, "get_flow", lambda name: flow)
    update_times = MagicMock()
    monkeypatch.setattr(flow_service, "db_update_flow_run_times", update_times)
    backend = MagicMock()
    backend.session_exists.side_effect = lambda name: bool(database.list_terminals_by_session(name))
    monkeypatch.setattr(flow_service, "get_backend", lambda: backend)
    manager = MagicMock()
    monkeypatch.setattr(flow_service, "provider_manager", manager)
    monkeypatch.setattr(flow_service, "fifo_manager", MagicMock())
    monkeypatch.setattr(flow_service, "status_monitor", MagicMock())
    created = []

    async def create(**kwargs):
        tid = f"worker-{len(created)}"
        database.create_terminal(tid, kwargs["session_name"], "worker", "kimi_cli")
        created.append(tid)
        return SimpleNamespace(id=tid)

    create_mock = AsyncMock(side_effect=create)
    monkeypatch.setattr(flow_service, "create_terminal", create_mock)

    def uncertain_send(*args):
        assert update_times.call_count == 1  # schedule advanced before dispatch
        raise InputAcceptanceUnconfirmedError("PRIVATE_TRANSPORT_DETAIL")

    send = MagicMock(side_effect=uncertain_send)
    monkeypatch.setattr(flow_service, "send_input", send)
    assert await flow_service.execute_flow("uncertainty") is True
    assert send.call_count == 1
    assert create_mock.await_count == 1
    assert database.get_terminal_metadata("worker-0") is not None
    manager.cleanup_provider.assert_not_called()
    backend.kill_session.assert_not_called()
    assert "outcome=dispatched_unconfirmed" in caplog.text
    assert "PRIVATE_TRANSPORT_DETAIL" not in caplog.text
    assert "failed" not in caplog.text

    # The next cron occurrence respects the existing busy-conductor guard.
    monkeypatch.setattr(flow_service, "_is_terminal_busy", lambda tid: True)
    assert await flow_service.execute_flow("uncertainty") is False
    assert send.call_count == 1
    assert database.get_terminal_metadata("worker-0") is not None

    # An idle future occurrence remains new scheduled work, not a disabled flow.
    monkeypatch.setattr(flow_service, "_is_terminal_busy", lambda tid: False)
    send.side_effect = None
    assert await flow_service.execute_flow("uncertainty") is True
    assert send.call_count == 2
    assert created == ["worker-0", "worker-1"]
    assert database.get_terminal_metadata("worker-0") is None
    assert database.get_terminal_metadata("worker-1") is not None

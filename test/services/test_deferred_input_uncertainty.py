"""A dispatched Kimi first task survives inconclusive acceptance evidence."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services import terminal_service as ts


@pytest.mark.asyncio
@pytest.mark.parametrize("through_assign", [False, True])
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("has_prefix", [False, True])
async def test_deferred_kimi_dispatch_is_never_resubmitted_or_deleted_on_uncertainty(
    isolated_memory_db,
    monkeypatch,
    through_assign,
    accepted,
    has_prefix,
):
    database.create_terminal("caller", "cao-test", "supervisor", "mock")
    database.create_terminal("worker", "cao-test", "worker", "kimi_cli", caller_id="caller")
    earlier = datetime.now() - timedelta(hours=1)
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "worker").last_active = earlier
        db.commit()

    provider = KimiCliProvider("worker", "cao-test", "worker")
    provider._first_message_prefix = "Follow the review constraints." if has_prefix else None

    async def initialize():
        provider._initialized = True

    monkeypatch.setattr(provider, "initialize", initialize)
    manager = ProviderManager()
    manager._providers["worker"] = provider
    monkeypatch.setattr(ts, "provider_manager", manager)
    backend = MagicMock()
    backend.get_history.side_effect = [
        "context: 0%",
        "• Reviewing the task\ncontext: 1%" if accepted else "context: 0%",
    ]
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.IDLE
    monkeypatch.setattr(ts, "status_monitor", monitor)
    monkeypatch.setattr(ts, "inject_memory_context", lambda message, *_args: message)
    confirm = provider.confirm_input_accepted
    probe = MagicMock(side_effect=lambda baseline, message: confirm(baseline, message, timeout=0))
    monkeypatch.setattr(provider, "confirm_input_accepted", probe)
    commit = MagicMock(wraps=provider.commit_prepared_input)
    monkeypatch.setattr(provider, "commit_prepared_input", commit)
    resubmit = AsyncMock(return_value=True)
    monkeypatch.setattr(ts, "_confirm_worker_started_or_resubmit", resubmit)
    delete = MagicMock()
    monkeypatch.setattr(ts, "delete_terminal", delete)
    event = MagicMock()
    monkeypatch.setattr(ts, "dispatch_plugin_event", event)
    registry = MagicMock()

    before_tasks = set(ts._deferred_init_tasks)
    if through_assign:
        from cli_agent_orchestrator.mcp_server import server

        def create_terminal(_profile, _directory, **kwargs):
            # Model the HTTP creation boundary while executing the actual
            # assign message construction and service background callback.
            assert kwargs["defer_init"] is True
            assert kwargs["initial_message_orchestration_type"] == OrchestrationType.ASSIGN
            ts._schedule_deferred_init(
                provider,
                "worker",
                kwargs["initial_message"],
                kwargs["initial_message_orchestration_type"],
                registry,
            )
            return "worker", "kimi_cli"

        monkeypatch.setattr(server, "_create_terminal", create_terminal)
        monkeypatch.setattr(server, "_current_terminal_id", lambda: "caller")
        monkeypatch.setattr(server, "ENABLE_SENDER_ID_INJECTION", True)
        monkeypatch.setattr(server, "_get_cleanup_nudge", lambda: "")
        result = server._assign_impl("reviewer", "Review the task")
        assert result["success"] is True
        assert result["terminal_id"] == "worker"
    else:
        ts._schedule_deferred_init(
            provider, "worker", "Review the task", OrchestrationType.ASSIGN, registry
        )

    (task,) = set(ts._deferred_init_tasks) - before_tasks
    await task

    backend.send_keys.assert_called_once()
    probe.assert_called_once()
    resubmit.assert_not_called()
    delete.assert_not_called()
    assert manager.get_provider("worker") is provider
    metadata = database.get_terminal_metadata("worker")
    assert metadata["provider_initialized"] is True
    assert metadata["last_active"] > earlier
    assert metadata["profile_prompt_delivered"] is (accepted and has_prefix)
    event.assert_called_once()
    assert event.call_args.args[1] == "post_send_message"
    assert event.call_args.args[2].orchestration_type == OrchestrationType.ASSIGN
    if through_assign:
        assert "Assigned by terminal caller" in backend.send_keys.call_args.args[2]
    if accepted:
        commit.assert_called_once()
        assert not provider.has_pending_profile_prompt
        assert database.get_pending_messages("caller") == []
    else:
        commit.assert_not_called()
        assert provider.has_pending_profile_prompt is has_prefix
        (notification,) = database.get_pending_messages("caller")
        assert "input was dispatched" in notification.message
        assert "acceptance is uncertain" in notification.message
        assert "Do not retry or re-assign automatically" in notification.message
        assert "inspect this terminal" in notification.message

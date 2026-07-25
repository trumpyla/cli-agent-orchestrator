"""Strict, deterministic startup reconciliation tests for the Herdr backend."""

import asyncio
import json
import logging
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.herdr_inbox_service import (
    HerdrInboxService,
    _run_startup_reconciliation,
)


def _workspace(
    workspace_id: str = "ws-live",
    label: str = "cao-live",
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "label": label,
        "agent_status": "idle",
        "pane_count": 1,
        "tab_count": 1,
        "active_tab_id": f"{workspace_id}:1",
    }


def _terminal(
    terminal_id: str,
    session: str,
    window: str,
    provider: str = "claude_code",
) -> dict[str, object]:
    return {
        "id": terminal_id,
        "tmux_session": session,
        "tmux_window": window,
        "provider": provider,
    }


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = "sensitive-command-output"
    result.returncode = returncode
    return result


def _inventory_side_effect(
    workspaces: list[object],
    tabs: list[object],
):
    responses = {
        "workspace": _completed(json.dumps({"result": {"workspaces": workspaces}})),
        "tab": _completed(json.dumps({"result": {"tabs": tabs}})),
    }

    def run(command, **_kwargs):
        return responses[command[3]]

    return run


@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_valid_empty_inventory_is_authoritative(
    mock_run,
    mock_list,
    mock_delete,
):
    """A complete empty inventory deletes every persisted non-peer terminal."""
    mock_run.side_effect = _inventory_side_effect([], [])
    mock_list.return_value = [_terminal("terminal-secret", "session-secret", "window-secret")]
    mock_delete.return_value = True

    summary = _run_startup_reconciliation("cao")

    mock_delete.assert_called_once_with("terminal-secret")
    assert summary.model_dump() == {
        "inventory_valid": True,
        "reason": "ok",
        "examined": 1,
        "peer_skipped": 0,
        "missing_workspace": 1,
        "missing_tab": 0,
        "deleted": 1,
        "delete_failed": 0,
    }


@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_missing_workspace_and_tab_use_transactional_delete_primitive(
    mock_run,
    mock_list,
    mock_delete,
):
    mock_run.side_effect = _inventory_side_effect(
        [_workspace()],
        [{"tab_id": "ws-live:1", "workspace_id": "ws-live", "label": "live-window"}],
    )
    mock_list.return_value = [
        _terminal("missing-workspace", "cao-gone", "gone-window"),
        _terminal("missing-tab", "cao-live", "gone-window"),
        _terminal("live", "cao-live", "live-window"),
    ]
    mock_delete.return_value = True

    summary = _run_startup_reconciliation("cao")

    assert [call.args for call in mock_delete.call_args_list] == [
        ("missing-workspace",),
        ("missing-tab",),
    ]
    assert summary.missing_workspace == 1
    assert summary.missing_tab == 1
    assert summary.deleted == 2


@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_peer_provider_and_peer_session_are_never_reconciled(
    mock_run,
    mock_list,
    mock_delete,
):
    mock_run.side_effect = _inventory_side_effect([], [])
    mock_list.return_value = [
        _terminal("provider-peer", "ordinary-session", "driver", provider="peer"),
        _terminal("session-peer", "__peers__", "driver", provider="claude_code"),
    ]

    summary = _run_startup_reconciliation("cao")

    mock_delete.assert_not_called()
    assert summary.examined == 0
    assert summary.peer_skipped == 2


@pytest.mark.parametrize(
    ("workspace_stdout", "expected_reason"),
    [
        ("not-json", "workspace_inventory_invalid"),
        (json.dumps([]), "workspace_inventory_invalid"),
        (json.dumps({}), "workspace_inventory_invalid"),
        (json.dumps({"result": {}}), "workspace_inventory_invalid"),
        (json.dumps({"result": {"workspaces": {}}}), "workspace_inventory_invalid"),
        (
            json.dumps({"result": {"workspaces": ["bad-element"]}}),
            "workspace_inventory_invalid",
        ),
        (
            json.dumps({"result": {"workspaces": [_workspace(), _workspace("ws-other")]}}),
            "duplicate_workspace_label",
        ),
    ],
)
@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_incomplete_workspace_inventory_fails_closed(
    mock_run,
    mock_list,
    mock_delete,
    workspace_stdout,
    expected_reason,
):
    mock_run.return_value = _completed(workspace_stdout)

    summary = _run_startup_reconciliation("cao")

    assert not summary.inventory_valid
    assert summary.reason == expected_reason
    mock_list.assert_not_called()
    mock_delete.assert_not_called()


@pytest.mark.parametrize(
    "tabs_value",
    [
        {},
        ["bad-element"],
        [{"tab_id": "tab-1", "workspace_id": "ws-live"}],
        [{"tab_id": 1, "workspace_id": "ws-live", "label": "window"}],
    ],
)
@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_malformed_tab_inventory_fails_closed(
    mock_run,
    mock_list,
    mock_delete,
    tabs_value,
):
    mock_run.side_effect = [
        _completed(json.dumps({"result": {"workspaces": [_workspace()]}})),
        _completed(json.dumps({"result": {"tabs": tabs_value}})),
    ]

    summary = _run_startup_reconciliation("cao")

    assert not summary.inventory_valid
    assert summary.reason == "tab_inventory_invalid"
    mock_list.assert_not_called()
    mock_delete.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (_completed(returncode=7), "workspace_command_failed"),
        (subprocess.TimeoutExpired(["herdr"], 10), "workspace_command_timeout"),
    ],
)
@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_workspace_command_failure_or_timeout_fails_closed(
    mock_run,
    mock_list,
    mock_delete,
    failure,
    expected_reason,
):
    if isinstance(failure, BaseException):
        mock_run.side_effect = failure
    else:
        mock_run.return_value = failure

    summary = _run_startup_reconciliation("cao")

    assert not summary.inventory_valid
    assert summary.reason == expected_reason
    mock_list.assert_not_called()
    mock_delete.assert_not_called()


@patch("cli_agent_orchestrator.clients.database.delete_terminal")
@patch("cli_agent_orchestrator.clients.database.list_all_terminals")
@patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
def test_delete_failures_are_isolated_and_logs_are_content_free(
    mock_run,
    mock_list,
    mock_delete,
    caplog,
):
    mock_run.side_effect = _inventory_side_effect([], [])
    mock_list.return_value = [
        _terminal("terminal-secret-one", "session-secret", "window-secret-one"),
        _terminal("terminal-secret-two", "session-secret", "window-secret-two"),
    ]
    mock_delete.side_effect = [RuntimeError("message-body-secret"), True]

    with caplog.at_level(logging.INFO):
        summary = _run_startup_reconciliation("cao")

    assert summary.deleted == 1
    assert summary.delete_failed == 1
    assert "startup_reconciliation" in caplog.text
    assert "delete_failed=1" in caplog.text
    for sentinel in (
        "terminal-secret",
        "session-secret",
        "window-secret",
        "message-body-secret",
        "sensitive-command-output",
    ):
        assert sentinel not in caplog.text


def test_startup_reconciliation_runs_off_event_loop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocked(_herdr_session):
        entered.set()
        assert release.wait(timeout=2)
        return MagicMock()

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_service._run_startup_reconciliation",
        blocked,
    )

    async def exercise() -> None:
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        cleanup_task = asyncio.create_task(service._startup_db_cleanup())
        assert await asyncio.to_thread(entered.wait, 1)

        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)

        release.set()
        await asyncio.wait_for(cleanup_task, timeout=1)

    asyncio.run(exercise())

"""Focused regressions for completed-workspace cleanup reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend, HerdrWorkspace
from cli_agent_orchestrator.services.config_service import CleanupConfig
from cli_agent_orchestrator.services.runtime_resource_cleanup import (
    RuntimeResourceCleanup,
    build_runtime_resource_cleanup,
)

NOW = datetime(2026, 9, 8, 12, 0, 0)


def _workspace() -> HerdrWorkspace:
    return HerdrWorkspace(
        workspace_id="ws-old",
        label="cao-old",
        agent_status="done",
        pane_count=1,
        tab_count=1,
        active_tab_id="ws-old:1",
    )


def _terminal() -> dict[str, object]:
    return {
        "id": "terminal-old",
        "tmux_session": "cao-old",
        "tmux_window": "developer-old",
        "provider": "claude_code",
        "last_active": NOW - timedelta(hours=2),
    }


def _config() -> CleanupConfig:
    return CleanupConfig(
        completed_sessions_enabled=True,
        completed_session_grace_s=900,
        sweep_interval_s=300,
        max_sessions_per_sweep=5,
        preserve_patterns=[],
    )


def _backend(*inventories: list[HerdrWorkspace]) -> HerdrBackend:
    backend = object.__new__(HerdrBackend)
    backend.list_workspace_inventory = MagicMock(side_effect=list(inventories))
    return backend


def test_absent_workspace_revalidation_rechecks_terminal_rows() -> None:
    terminal_reader = MagicMock(side_effect=[[_terminal()], []])
    teardown = MagicMock()
    cleanup = RuntimeResourceCleanup(
        backend=_backend([_workspace()], []),
        config_reader=_config,
        terminal_reader=terminal_reader,
        pending_checker=lambda _terminal_id: False,
        teardown=teardown,
        clock=lambda: NOW,
    )

    summary = cleanup.sweep()

    assert summary.deleted == 0
    assert summary.reasons == {"terminal_state_changed": 1}
    assert terminal_reader.call_count == 2
    teardown.assert_not_called()


def test_absent_workspace_revalidation_rechecks_pending_inbox() -> None:
    pending_checker = MagicMock(side_effect=[False, True])
    teardown = MagicMock()
    cleanup = RuntimeResourceCleanup(
        backend=_backend([_workspace()], []),
        config_reader=_config,
        terminal_reader=lambda _label: [_terminal()],
        pending_checker=pending_checker,
        teardown=teardown,
        clock=lambda: NOW,
    )

    summary = cleanup.sweep()

    assert summary.deleted == 0
    assert summary.reasons == {"pending_inbox_changed": 1}
    assert pending_checker.call_count == 2
    teardown.assert_not_called()


def _run_built_cleanup(teardown_result: dict[str, object]):
    backend = _backend([_workspace()], [_workspace()])
    with (
        patch("cli_agent_orchestrator.clients.database.list_runtime_cleanup_debt", return_value=[]),
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get_config",
            return_value=SimpleNamespace(cleanup=_config()),
        ),
        patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[_terminal()],
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_pending_messages",
            return_value=[],
        ),
        patch(
            "cli_agent_orchestrator.services.session_service.delete_session_automatically",
            return_value=teardown_result,
        ),
    ):
        return build_runtime_resource_cleanup(backend=backend, clock=lambda: NOW).sweep()


def test_completed_cleanup_ignores_nonfatal_ancillary_errors() -> None:
    summary = _run_built_cleanup(
        {
            "deleted": ["cao-old"],
            "errors": [
                {
                    "session": "cao-old",
                    "step": "clear_session_env",
                    "error": "ancillary cleanup warning",
                }
            ],
        }
    )

    assert summary.deleted == 1
    assert summary.reasons == {}


@pytest.mark.parametrize(
    "teardown_result",
    [
        pytest.param({"deleted": [], "errors": []}, id="missing-deleted-session"),
        pytest.param(
            {
                "deleted": ["cao-old"],
                "errors": [],
                "deferred_ids": ["terminal-old"],
            },
            id="explicit-deferred-terminal",
        ),
    ],
)
def test_incomplete_cleanup_result_is_a_teardown_failure(
    teardown_result: dict[str, object],
) -> None:
    summary = _run_built_cleanup(teardown_result)

    assert summary.deleted == 0
    assert summary.reasons == {"teardown_failed": 1}

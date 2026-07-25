"""Credential-free source-backed supervisor integration harness."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.terminal_factory import TerminalFactory
from typing import Callable

import pytest
import requests


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    poll: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    pytest.fail(f"condition did not become true within {timeout:.1f}s")


def _terminal_status(server_url: str, terminal_id: str) -> str:
    response = requests.get(f"{server_url}/terminals/{terminal_id}", timeout=2)
    if response.status_code != 200:
        return "missing"
    return str(response.json()["status"])


def _delivered_callback_exists(server_url: str, terminal_id: str) -> bool:
    response = requests.get(
        f"{server_url}/terminals/{terminal_id}/inbox/messages",
        params={"status": "delivered", "limit": 50},
        timeout=2,
    )
    response.raise_for_status()
    return bool(response.json())


def _assert_database_has_no_runtime_rows(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        terminal_count = connection.execute("SELECT COUNT(*) FROM terminals").fetchone()[0]
        inbox_count = connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    assert terminal_count == 0
    assert inbox_count == 0


@pytest.mark.integration
def test_supervisor_assign_callback_failure_cleanup_and_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the complete supervisor lifecycle without external credentials."""

    import cli_agent_orchestrator.mcp_server.server as supervisor_mcp

    server = _start_cao_server(tmp_path / "home", _pick_free_port())
    supervisor = None
    try:
        supervisor = TerminalFactory.create(
            server,
            provider="mock_cli",
            agent_profile="developer",
            session_prefix="cao-harness",
        )
        monkeypatch.setattr(supervisor_mcp, "API_BASE_URL", server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", supervisor.terminal_id)

        assignment = supervisor_mcp._assign_impl(
            "developer",
            "Return the deterministic integration result through send_message.",
        )
        assert assignment["success"] is True
        worker_id = assignment["terminal_id"]
        assert isinstance(worker_id, str)
        _wait_until(
            lambda: _terminal_status(server.url, worker_id) == "completed",
            timeout=20,
        )

        monkeypatch.setenv("CAO_TERMINAL_ID", worker_id)
        callback = supervisor_mcp._send_message_impl(
            None,
            "deterministic worker result",
        )
        assert callback["success"] is True
        _wait_until(
            lambda: _delivered_callback_exists(server.url, supervisor.terminal_id),
            timeout=20,
        )
        _wait_until(
            lambda: _terminal_status(server.url, supervisor.terminal_id) == "completed",
            timeout=20,
        )

        missing_receiver = supervisor_mcp._send_message_impl(
            "ffffffff",
            "this must fail closed",
        )
        assert missing_receiver["success"] is False
        assert "ffffffff" in missing_receiver["error"]

        deleted = supervisor_mcp.delete_terminal(worker_id)
        assert deleted["success"] is True
        _wait_until(lambda: _terminal_status(server.url, worker_id) == "missing")

        session_response = requests.get(
            f"{server.url}/sessions/{supervisor.session_name}",
            timeout=2,
        )
        session_response.raise_for_status()
        assert [terminal["id"] for terminal in session_response.json()["terminals"]] == [
            supervisor.terminal_id
        ]
    finally:
        if supervisor is not None:
            supervisor.cleanup()
        server.stop()

    _assert_database_has_no_runtime_rows(server.db_path)
    with pytest.raises(requests.ConnectionError):
        requests.get(f"{server.url}/health", timeout=0.2)

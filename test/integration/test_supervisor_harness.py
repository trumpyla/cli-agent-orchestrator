"""Credential-free source-backed supervisor integration harness."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.terminal_factory import TerminalFactory
from typing import Callable

import httpx
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


def _mcp_headers(session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _jsonrpc_body(response: httpx.Response) -> dict[str, object]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data_lines = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert data_lines, response.text
        return json.loads(data_lines[-1])
    return response.json()


async def _initialize_ops(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "source-backed-harness", "version": "1.0"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id

    initialized = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(session_id),
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )
    assert initialized.status_code in {200, 202}, initialized.text
    return session_id


async def _call_ops_tool(
    client: httpx.AsyncClient,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    response = await client.post(
        "/mcp/ops",
        headers=_mcp_headers(session_id),
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    body = _jsonrpc_body(response)
    assert "error" not in body, body
    result = body["result"]
    assert isinstance(result, dict), result
    structured = result.get("structuredContent")
    assert isinstance(structured, dict), result
    return structured


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_workers_run_through_embedded_ops_and_clean_up(
    tmp_path: Path,
) -> None:
    """Run three real source-backed mock workers through the HTTP Ops surface."""

    server = _start_cao_server(tmp_path / "three-worker-home", _pick_free_port())
    worker_session_names = [f"cao-three-worker-{worker_number}" for worker_number in range(3)]
    launched: list[dict[str, object]] = []
    try:
        async with httpx.AsyncClient(
            base_url=server.url,
            timeout=httpx.Timeout(10.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            session_id = await _initialize_ops(client)

            async def launch(worker_number: int) -> dict[str, object]:
                result = await _call_ops_tool(
                    client,
                    session_id,
                    worker_number + 10,
                    "launch_session",
                    {
                        "agent_profile": "developer",
                        "provider": "mock_cli",
                        "session_name": worker_session_names[worker_number],
                        "initial_message": f"worker-{worker_number}-result",
                    },
                )
                if result.get("success") is True:
                    launched.append(result)
                return result

            launch_results = await asyncio.wait_for(
                asyncio.gather(*(launch(worker_number) for worker_number in range(3))),
                timeout=30,
            )
            assert all(worker.get("success") is True for worker in launch_results)
            assert len({worker["terminal_id"] for worker in launched}) == 3
            assert len({worker["session_name"] for worker in launched}) == 3

            async def wait_for_completion(
                worker_number: int,
                worker: dict[str, object],
            ) -> dict[str, object]:
                deadline = time.monotonic() + 30
                status: dict[str, object] = {}
                while time.monotonic() < deadline:
                    status = await _call_ops_tool(
                        client,
                        session_id,
                        100 + worker_number,
                        "get_terminal_status",
                        {"terminal_id": worker["terminal_id"]},
                    )
                    if status.get("status") == "completed":
                        output = await _call_ops_tool(
                            client,
                            session_id,
                            200 + worker_number,
                            "read_session_output",
                            {
                                "terminal_id": worker["terminal_id"],
                                "mode": "full",
                            },
                        )
                        assert output.get("success") is True
                        assert "MOCK:" in str(output.get("output", ""))
                        return status
                    assert status.get("status") != "error", status
                    await asyncio.sleep(0.2)
                pytest.fail(f"worker did not complete: {worker}, last status={status}")

            await asyncio.gather(
                *(
                    wait_for_completion(worker_number, worker)
                    for worker_number, worker in enumerate(launched)
                )
            )

            async def shutdown(worker_number: int, worker: dict[str, object]) -> dict[str, object]:
                return await _call_ops_tool(
                    client,
                    session_id,
                    300 + worker_number,
                    "shutdown_session",
                    {"session_name": worker["session_name"]},
                )

            shutdown_results = await asyncio.gather(
                *(shutdown(worker_number, worker) for worker_number, worker in enumerate(launched))
            )
            assert all(result.get("success") is True for result in shutdown_results)

            for worker in launched:
                response = await client.get(f"/sessions/{worker['session_name']}")
                assert response.status_code == 404, response.text
    finally:
        # The MCP calls above are expected to clean up. If an assertion or
        # timeout interrupts them, use the REST endpoint to avoid leaking tmux
        # sessions into the developer's machine.
        for session_name in worker_session_names:
            with contextlib.suppress(Exception):
                requests.delete(f"{server.url}/sessions/{session_name}", timeout=2)
        server.stop()

    _assert_database_has_no_runtime_rows(server.db_path)

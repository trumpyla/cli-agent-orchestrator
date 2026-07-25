"""Factory compatibility tests for the CAO Ops MCP server."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.ops_mcp_server as ops_package
from cli_agent_orchestrator.ops_mcp_server import server as server_module
from cli_agent_orchestrator.ops_mcp_server.backend import (
    CLIENT_DEFAULT_TIMEOUT,
    RequestResult,
    RequestTimeout,
)

create_ops_mcp = getattr(server_module, "create_ops_mcp", None)


def test_create_ops_mcp_factory_exists_for_backend_injection() -> None:
    """Without the factory, embedded and stdio transports share global I/O."""
    assert hasattr(server_module, "create_ops_mcp")


def test_ops_package_exports_factory_and_backend_contract() -> None:
    """Hiding the factory boundary would force callers back to module globals."""
    assert hasattr(ops_package, "create_ops_mcp")
    assert hasattr(ops_package, "AsyncRequestBackend")


@dataclass(frozen=True)
class BackendCall:
    """One hand-checkable backend invocation."""

    method: str
    path: str
    params: dict[str, Any] | None
    json: Any | None
    operation: str
    timeout: RequestTimeout


class StrictBackend:
    """Async fake that fails on extra calls and records every exact argument."""

    def __init__(self, responses: list[RequestResult]) -> None:
        self._responses = list(responses)
        self.calls: list[BackendCall] = []
        self.closed = False

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        operation: str,
        timeout: RequestTimeout = CLIENT_DEFAULT_TIMEOUT,
    ) -> RequestResult:
        if not self._responses:
            raise AssertionError("unexpected extra backend request")
        self.calls.append(
            BackendCall(
                method=method,
                path=path,
                params=params,
                json=json,
                operation=operation,
                timeout=timeout,
            )
        )
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


EXPECTED_TOOL_NAMES = {
    "ack_messages",
    "get_profile_details",
    "get_session_info",
    "get_terminal_output",
    "get_terminal_status",
    "install_profile",
    "launch_session",
    "list_profiles",
    "list_sessions",
    "read_session_output",
    "receive_messages",
    "register_peer",
    "send_session_message",
    "send_terminal_input",
    "send_terminal_key",
    "shutdown_session",
}


@pytest.mark.asyncio
async def test_factory_preserves_stdio_tool_and_resource_schemas() -> None:
    """Dropping or reshaping a registration would break existing stdio clients."""
    backend = StrictBackend([])
    factory_mcp = create_ops_mcp(backend)

    factory_tools = {tool.name: tool.parameters for tool in await factory_mcp.list_tools()}
    stdio_tools = {tool.name: tool.parameters for tool in await server_module.mcp.list_tools()}
    templates = await factory_mcp.list_resource_templates()

    assert set(factory_tools) == EXPECTED_TOOL_NAMES
    assert factory_tools == stdio_tools
    assert [(str(template.uri_template), template.name) for template in templates] == [
        ("cao://peers/{peer_id}/inbox", "peer_inbox_resource")
    ]


@pytest.mark.asyncio
async def test_factory_registered_handler_uses_backend_without_requests() -> None:
    """A registered async tool calling requests would block the MCP event loop."""
    backend = StrictBackend([([{"name": "developer"}], None)])
    factory_mcp = create_ops_mcp(backend)

    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
        side_effect=AssertionError("registered MCP handler called requests"),
    ):
        result = await factory_mcp.call_tool("list_profiles", {})

    assert result.structured_content == {
        "success": True,
        "message": None,
        "profiles": [{"name": "developer"}],
    }
    assert backend.calls == [
        BackendCall(
            method="get",
            path="/agents/profiles",
            params=None,
            json=None,
            operation="List profiles",
            timeout=CLIENT_DEFAULT_TIMEOUT,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wait_seconds", "expected_timeout"),
    [
        pytest.param(0.0, None, id="immediate-explicit-none"),
        pytest.param(60.0, 65.0, id="long-poll-wait-plus-five"),
    ],
)
async def test_factory_receive_messages_preserves_timeout_contract(
    wait_seconds: float,
    expected_timeout: float | None,
) -> None:
    """Using a default timeout would break immediate reads or 60s long-polls."""
    backend = StrictBackend([([], None)])
    factory_mcp = create_ops_mcp(backend)

    result = await factory_mcp.call_tool(
        "receive_messages",
        {"peer_id": "deadbeef", "wait_seconds": wait_seconds},
    )

    assert result.structured_content == {
        "success": True,
        "messages": [],
        "count": 0,
    }
    assert backend.calls[0].timeout == expected_timeout
    assert backend.calls[0].params == (
        {"status": "pending", "limit": 10}
        if wait_seconds == 0
        else {"status": "pending", "limit": 10, "wait": 60.0}
    )


@pytest.mark.asyncio
async def test_factory_peer_resource_uses_explicit_no_timeout_for_immediate_read() -> None:
    """Letting the client default apply would fail inbox reads after five seconds."""
    backend = StrictBackend([([{"id": 7, "message": "ready"}], None)])
    factory_mcp = create_ops_mcp(backend)

    result = await factory_mcp.read_resource("cao://peers/deadbeef/inbox")

    assert json.loads(result.contents[0].content)["messages"] == [{"id": 7, "message": "ready"}]
    assert backend.calls[0].timeout is None


class GatedBackend(StrictBackend):
    """Backend that exposes deterministic start/release events."""

    def __init__(self, payload: list[dict[str, str]]) -> None:
        super().__init__([(payload, None)])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request_json(self, *args: Any, **kwargs: Any) -> RequestResult:
        self.started.set()
        await self.release.wait()
        return await super().request_json(*args, **kwargs)


@pytest.mark.asyncio
async def test_factories_isolate_parallel_backend_instances() -> None:
    """A process-global backend would cross-wire concurrent embedded clients."""
    first_backend = GatedBackend([{"name": "first"}])
    second_backend = GatedBackend([{"name": "second"}])
    first_mcp = create_ops_mcp(first_backend)
    second_mcp = create_ops_mcp(second_backend)

    first_task = asyncio.create_task(first_mcp.call_tool("list_profiles", {}))
    second_task = asyncio.create_task(second_mcp.call_tool("list_profiles", {}))
    await asyncio.gather(first_backend.started.wait(), second_backend.started.wait())
    second_backend.release.set()
    second_result = await second_task
    first_backend.release.set()
    first_result = await first_task

    assert first_result.structured_content["profiles"] == [{"name": "first"}]
    assert second_result.structured_content["profiles"] == [{"name": "second"}]
    assert len(first_backend.calls) == 1
    assert len(second_backend.calls) == 1


@pytest.mark.asyncio
async def test_factory_lifespan_closes_its_backend() -> None:
    """Omitting backend shutdown would leak the shared HTTPX connection pool."""
    backend = StrictBackend([])
    factory_mcp = create_ops_mcp(backend)

    async with factory_mcp._lifespan_manager():
        assert backend.closed is False

    assert backend.closed is True

"""Concurrency regressions for async identity-MCP tools."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from cli_agent_orchestrator.mcp_server import server


def _tool_callable(tool: Any) -> Callable[..., Awaitable[Any]]:
    """Return the coroutine wrapped by the installed FastMCP version."""
    return tool.fn if hasattr(tool, "fn") else tool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "helper_name", "kwargs", "result"),
    [
        pytest.param(
            server.load_skill,
            "_load_skill_impl",
            {"name": "python-testing"},
            "# Python testing",
            id="load-skill",
        ),
        pytest.param(
            server.send_message,
            "_send_message_impl",
            {"receiver_id": "deadbeef", "message": "done"},
            {"success": True},
            id="send-message",
        ),
        pytest.param(
            server.answer_user_prompt,
            "_send_user_prompt_answer",
            {"terminal_id": "deadbeef", "answer": "1"},
            {"success": True},
            id="answer-user-prompt",
        ),
    ],
)
async def test_async_tool_offloads_sync_http_helper(
    monkeypatch: pytest.MonkeyPatch,
    tool: Any,
    helper_name: str,
    kwargs: dict[str, str],
    result: Any,
) -> None:
    """A slow synchronous HTTP helper must not stall another event-loop task."""
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def blocking_helper(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        release.wait(timeout=1.0)
        events.append("helper-finished")
        return result

    def delayed_release() -> None:
        started.wait(timeout=1.0)
        time.sleep(0.05)
        release.set()

    monkeypatch.setattr(server, helper_name, blocking_helper)
    releaser = threading.Thread(target=delayed_release)
    releaser.start()
    try:
        task = asyncio.create_task(_tool_callable(tool)(**kwargs))
        while not started.is_set():
            await asyncio.sleep(0)
        events.append("loop-progress")
        assert await task == result
    finally:
        release.set()
        releaser.join(timeout=1.0)

    assert events.index("loop-progress") < events.index("helper-finished")

"""Concurrency regressions for async identity-MCP tools."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
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
        pytest.param(
            server.assign,
            "_assign_impl",
            {"agent_profile": "reviewer", "message": "review"},
            {"success": True},
            id="assign",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        pytest.param(
            server.memory_store,
            {
                "content": "decision",
                "scope": "project",
                "memory_type": "project",
                "key": "decision",
                "tags": "",
            },
            id="memory-store",
        ),
        pytest.param(
            server.memory_recall,
            {
                "query": "decision",
                "scope": None,
                "memory_type": None,
                "limit": 10,
                "search_mode": "hybrid",
                "sort_by": "recency",
                "include_related": False,
            },
            id="memory-recall",
        ),
        pytest.param(
            server.memory_forget,
            {"key": "decision", "scope": "project"},
            id="memory-forget",
        ),
    ],
)
async def test_memory_tools_offload_terminal_context_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tool: Any,
    kwargs: dict[str, Any],
) -> None:
    """Memory tools must not perform identity HTTP discovery on the event loop."""
    from cli_agent_orchestrator.services import memory_service, settings_service

    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def blocking_context() -> dict[str, str]:
        started.set()
        release.wait(timeout=1.0)
        events.append("context-finished")
        return {"terminal_id": "deadbeef"}

    class FakeMemoryService:
        async def store(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                key="decision",
                scope="project",
                scope_id=None,
                file_path="decision.md",
                action="created",
                created_at=1,
                updated_at=1,
            )

        async def recall(self, **_kwargs: Any) -> list[Any]:
            return []

        async def forget(self, **_kwargs: Any) -> bool:
            return True

    def delayed_release() -> None:
        started.wait(timeout=1.0)
        time.sleep(0.05)
        release.set()

    monkeypatch.setattr(server, "_get_terminal_context_from_env", blocking_context)
    monkeypatch.setattr(memory_service, "MemoryService", FakeMemoryService)
    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: True)
    releaser = threading.Thread(target=delayed_release)
    releaser.start()
    try:
        task = asyncio.create_task(_tool_callable(tool)(**kwargs))
        while not started.is_set():
            await asyncio.sleep(0)
        events.append("loop-progress")
        result = await task
    finally:
        release.set()
        releaser.join(timeout=1.0)

    assert result["success"] is True
    assert events.index("loop-progress") < events.index("context-finished")

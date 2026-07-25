"""Availability regression for Kimi initialization and the CAO health API."""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider


@pytest.mark.asyncio
async def test_health_responds_while_kimi_startup_dialog_handler_is_blocked() -> None:
    """Offloaded Kimi prompt polling must not starve unrelated API requests."""
    entered_handler = threading.Event()
    release_handler = threading.Event()
    ordering: list[str] = []

    def blocking_dialog(*, outer_timeout: float) -> None:
        del outer_timeout
        ordering.append("handler-enter")
        entered_handler.set()
        if not release_handler.wait(timeout=1.0):
            ordering.append("handler-timeout")
        ordering.append("handler-exit")

    provider_backend = MagicMock()
    provider = KimiCliProvider("term-1", "session-1", "window-1")

    with (
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.wait_for_shell",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.wait_until_status",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.get_backend",
            return_value=provider_backend,
        ),
        patch(
            "cli_agent_orchestrator.api.main.get_backend",
            return_value=MagicMock(),
        ),
        patch.object(provider, "_handle_startup_dialog", side_effect=blocking_dialog),
    ):
        initialize_task = asyncio.create_task(provider.initialize())
        handler_started = await asyncio.to_thread(entered_handler.wait, 0.5)
        assert handler_started is True

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client:
            response = await client.get("/health")
        ordering.append("health-response")
        release_handler.set()
        await initialize_task

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "handler-timeout" not in ordering
    assert ordering.index("health-response") < ordering.index("handler-exit")

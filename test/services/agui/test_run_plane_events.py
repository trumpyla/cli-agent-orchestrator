"""Wire-contract tests for CAO's typed AG-UI extension events."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest

pytest.importorskip("ag_ui")

from ag_ui.core import BaseEvent, EventType, RunFinishedEvent, RunStartedEvent
from ag_ui.encoder import EventEncoder

from cli_agent_orchestrator.services.agui.run_plane_events import (
    CaoCustomEvent,
    CaoRunErrorEvent,
    CaoStateDeltaEvent,
    CaoStateSnapshotEvent,
    CaoStepFinishedEvent,
    CaoStepStartedEvent,
    CaoToolCallEndEvent,
    CaoToolCallStartEvent,
)


def _payload(event: BaseEvent) -> dict[str, Any]:
    frame = EventEncoder().encode(event)
    assert frame.startswith("data: ")
    value = json.loads(frame.removeprefix("data: ").strip())
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast(dict[str, Any], value)


_EVENT_FACTORIES: tuple[Callable[[], BaseEvent], ...] = (
    lambda: CaoRunErrorEvent(
        type=EventType.RUN_ERROR,
        thread_id="thread-1",
        run_id="run-1",
        message="failed",
    ),
    lambda: CaoStateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        thread_id="thread-1",
        run_id="run-1",
        snapshot={},
    ),
    lambda: CaoStateDeltaEvent(
        type=EventType.STATE_DELTA,
        thread_id="thread-1",
        run_id="run-1",
        delta=[],
    ),
    lambda: CaoToolCallStartEvent(
        type=EventType.TOOL_CALL_START,
        thread_id="thread-1",
        run_id="run-1",
        tool_call_id="tool-1",
        tool_call_name="read",
    ),
    lambda: CaoToolCallEndEvent(
        type=EventType.TOOL_CALL_END,
        thread_id="thread-1",
        run_id="run-1",
        tool_call_id="tool-1",
    ),
    lambda: CaoCustomEvent(
        type=EventType.CUSTOM,
        thread_id="thread-1",
        run_id="run-1",
        name="cao.test",
        value={"ok": True},
    ),
)


@pytest.mark.parametrize("event_factory", _EVENT_FACTORIES)
def test_cao_extension_events_preserve_snake_case_correlation_keys(
    event_factory: Callable[[], BaseEvent],
) -> None:
    payload = _payload(event_factory())

    assert payload["thread_id"] == "thread-1"
    assert payload["run_id"] == "run-1"
    assert "threadId" not in payload
    assert "runId" not in payload


@pytest.mark.parametrize(
    "event",
    (
        CaoStepStartedEvent(
            type=EventType.STEP_STARTED,
            thread_id="thread-1",
            run_id="run-1",
            step_id="step-1",
            step_name="worker",
        ),
        CaoStepFinishedEvent(
            type=EventType.STEP_FINISHED,
            thread_id="thread-1",
            run_id="run-1",
            step_id="step-1",
            step_name="worker",
        ),
    ),
)
def test_cao_step_events_preserve_snake_case_step_id(event: BaseEvent) -> None:
    payload = _payload(event)

    assert payload["thread_id"] == "thread-1"
    assert payload["run_id"] == "run-1"
    assert payload["step_id"] == "step-1"
    assert payload["stepName"] == "worker"
    assert "threadId" not in payload
    assert "runId" not in payload
    assert "stepId" not in payload
    assert "step_name" not in payload


def test_cao_tool_call_event_keeps_sdk_field_aliases() -> None:
    payload = _payload(
        CaoToolCallStartEvent(
            type=EventType.TOOL_CALL_START,
            thread_id="thread-1",
            run_id="run-1",
            tool_call_id="tool-1",
            tool_call_name="read",
        )
    )

    assert payload["toolCallId"] == "tool-1"
    assert payload["toolCallName"] == "read"
    assert "tool_call_id" not in payload
    assert "tool_call_name" not in payload


@pytest.mark.parametrize(
    "event",
    (
        RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id="thread-1",
            run_id="run-1",
        ),
        RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id="thread-1",
            run_id="run-1",
        ),
    ),
)
def test_official_run_events_keep_sdk_camel_case_aliases(event: BaseEvent) -> None:
    payload = _payload(event)

    assert payload["threadId"] == "thread-1"
    assert payload["runId"] == "run-1"
    assert "thread_id" not in payload
    assert "run_id" not in payload

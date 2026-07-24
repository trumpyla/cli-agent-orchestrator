"""Typed CAO extensions to the official AG-UI event models.

The AG-UI SDK uses a camel-case alias generator for declared fields. CAO's
correlation fields on non-run lifecycle events predate that convention and are
part of the shipped wire contract in snake_case, so each field has an explicit
serialization alias.

This module imports the optional ``ag-ui-protocol`` package directly. Callers
must import it from within the same optional dependency guard used for the
official SDK.
"""

from ag_ui.core.events import (
    CustomEvent,
    RunErrorEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pydantic import Field


class CaoRunErrorEvent(RunErrorEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


class CaoStateSnapshotEvent(StateSnapshotEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


class CaoStateDeltaEvent(StateDeltaEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


class CaoStepStartedEvent(StepStartedEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")
    step_id: str = Field(serialization_alias="step_id")


class CaoStepFinishedEvent(StepFinishedEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")
    step_id: str = Field(serialization_alias="step_id")


class CaoToolCallStartEvent(ToolCallStartEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


class CaoToolCallEndEvent(ToolCallEndEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


class CaoCustomEvent(CustomEvent):
    thread_id: str = Field(serialization_alias="thread_id")
    run_id: str = Field(serialization_alias="run_id")


__all__ = [
    "CaoCustomEvent",
    "CaoRunErrorEvent",
    "CaoStateDeltaEvent",
    "CaoStateSnapshotEvent",
    "CaoStepFinishedEvent",
    "CaoStepStartedEvent",
    "CaoToolCallEndEvent",
    "CaoToolCallStartEvent",
]

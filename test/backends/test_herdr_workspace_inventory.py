"""Strict Herdr 0.7.5 workspace inventory and public-session mapping."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend


@pytest.fixture
def backend() -> HerdrBackend:
    with patch(
        "cli_agent_orchestrator.backends.herdr_backend.os.path.exists",
        return_value=True,
    ):
        return HerdrBackend()


def _completed(payload: object, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = json.dumps(payload)
    result.stderr = ""
    result.returncode = returncode
    return result


def _workspace(
    *,
    workspace_id: object = "workspace-1",
    label: object = "cao-session",
    agent_status: object = "idle",
    pane_count: object = 1,
    tab_count: object = 1,
    active_tab_id: object = "tab-1",
) -> dict:
    return {
        "workspace_id": workspace_id,
        "label": label,
        "agent_status": agent_status,
        "pane_count": pane_count,
        "tab_count": tab_count,
        "active_tab_id": active_tab_id,
    }


def _envelope(workspaces: object) -> dict:
    return {
        "id": "cli:workspace:list",
        "result": {"type": "workspace_list", "workspaces": workspaces},
    }


def test_live_0_7_5_fixture_preserves_stable_identity_and_native_status(
    backend: HerdrBackend,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "herdr_workspace_list_0_7_5.json"
    backend._run_herdr = MagicMock(return_value=_completed(json.loads(fixture.read_text())))

    inventory = backend.list_workspace_inventory()

    assert len(inventory) == 1
    assert inventory[0].workspace_id == "workspace-redacted-1"
    assert inventory[0].label == "cao-redacted-session"
    assert inventory[0].agent_status == "idle"
    assert backend.list_sessions() == [
        {
            "id": "cao-redacted-session",
            "name": "cao-redacted-session",
            "status": "idle",
        }
    ]


@pytest.mark.parametrize("native_status", ["done", "blocked", "idle", "working", "unknown"])
def test_public_session_status_maps_native_value(
    backend: HerdrBackend,
    native_status: str,
) -> None:
    backend._run_herdr = MagicMock(
        return_value=_completed(_envelope([_workspace(agent_status=native_status)]))
    )

    assert backend.list_sessions()[0]["status"] == native_status


@pytest.mark.parametrize(
    "workspaces",
    [
        pytest.param([None], id="non-dict-element"),
        pytest.param([_workspace(workspace_id="")], id="empty-id"),
        pytest.param([_workspace(label="")], id="empty-label"),
        pytest.param([_workspace(agent_status="active")], id="unknown-status"),
        pytest.param([_workspace(workspace_id=123)], id="wrong-id-type"),
        pytest.param([_workspace(label=123)], id="wrong-label-type"),
        pytest.param([_workspace(pane_count="1")], id="wrong-pane-count-type"),
        pytest.param([_workspace(tab_count=-1)], id="negative-tab-count"),
        pytest.param({"workspace_id": "not-a-list"}, id="wrong-envelope-type"),
    ],
)
def test_malformed_inventory_fails_closed(
    backend: HerdrBackend,
    workspaces: object,
) -> None:
    backend._run_herdr = MagicMock(return_value=_completed(_envelope(workspaces)))

    with pytest.raises(TerminalBackendError, match="invalid_workspace_inventory"):
        backend.list_workspace_inventory()
    assert backend.list_sessions() == []


@pytest.mark.parametrize(
    "missing_key",
    ["workspace_id", "label", "agent_status", "pane_count", "tab_count"],
)
def test_missing_required_workspace_key_fails_closed(
    backend: HerdrBackend,
    missing_key: str,
) -> None:
    workspace = _workspace()
    del workspace[missing_key]
    backend._run_herdr = MagicMock(return_value=_completed(_envelope([workspace])))

    with pytest.raises(TerminalBackendError, match="invalid_workspace_inventory"):
        backend.list_workspace_inventory()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="non-object-root"),
        pytest.param({}, id="missing-result"),
        pytest.param({"result": []}, id="non-object-result"),
        pytest.param({"result": {"type": "workspace_list"}}, id="missing-workspaces"),
    ],
)
def test_malformed_workspace_envelope_fails_closed(
    backend: HerdrBackend,
    payload: object,
) -> None:
    backend._run_herdr = MagicMock(return_value=_completed(payload))

    with pytest.raises(TerminalBackendError, match="invalid_workspace_inventory"):
        backend.list_workspace_inventory()


def test_duplicate_labels_make_inventory_ambiguous(backend: HerdrBackend) -> None:
    backend._run_herdr = MagicMock(
        return_value=_completed(
            _envelope(
                [
                    _workspace(workspace_id="workspace-1"),
                    _workspace(workspace_id="workspace-2"),
                ]
            )
        )
    )

    with pytest.raises(TerminalBackendError, match="duplicate_workspace_label"):
        backend.list_workspace_inventory()


def test_revalidate_requires_same_label_and_workspace_id(backend: HerdrBackend) -> None:
    backend._run_herdr = MagicMock(
        return_value=_completed(_envelope([_workspace(workspace_id="workspace-new")]))
    )

    assert backend.revalidate_workspace("cao-session", "workspace-old") is None
    assert (
        backend.revalidate_workspace("cao-session", "workspace-new").workspace_id == "workspace-new"
    )


def test_close_workspace_uses_stable_id_not_label(backend: HerdrBackend) -> None:
    backend._run_herdr = MagicMock(return_value=_completed({}, returncode=0))

    assert backend.close_workspace_by_id("workspace-123") is True
    backend._run_herdr.assert_called_once_with(
        ["workspace", "close", "workspace-123"],
        check=False,
    )

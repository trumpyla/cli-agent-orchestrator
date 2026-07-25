"""Functional and adversarial tests for completed Herdr workspace cleanup."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend, HerdrWorkspace
from cli_agent_orchestrator.services.config_service import CleanupConfig
from cli_agent_orchestrator.services.runtime_resource_cleanup import RuntimeResourceCleanup

NOW = datetime(2026, 7, 25, 12, 0, 0)


def workspace(
    label: str = "cao-old",
    workspace_id: str = "ws-old",
    status: str = "done",
) -> HerdrWorkspace:
    return HerdrWorkspace(
        workspace_id=workspace_id,
        label=label,
        agent_status=status,
        pane_count=1,
        tab_count=1,
        active_tab_id=f"{workspace_id}:1",
    )


def terminal(
    terminal_id: str = "terminal-old",
    session: str = "cao-old",
    provider: str = "claude_code",
    last_active: datetime | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": terminal_id,
        "tmux_session": session,
        "tmux_window": "worker",
        "provider": provider,
    }
    if last_active is not None:
        row["last_active"] = last_active
    return row


class FakeHerdrBackend(HerdrBackend):
    """Minimal strict-inventory backend without starting a Herdr process."""

    def __init__(self, inventories: list[list[HerdrWorkspace]]) -> None:
        self.inventories = inventories
        self.inventory_calls = 0

    def list_workspace_inventory(self) -> list[HerdrWorkspace]:
        index = min(self.inventory_calls, len(self.inventories) - 1)
        self.inventory_calls += 1
        return self.inventories[index]


def engine(
    *,
    inventories: list[list[HerdrWorkspace]] | None = None,
    rows: dict[str, list[dict[str, object]]] | None = None,
    config: CleanupConfig | None = None,
    pending: Callable[[str], bool] | None = None,
    teardown: Callable[[str, str], None] | None = None,
    backend: object | None = None,
) -> tuple[RuntimeResourceCleanup, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    terminal_rows = (
        rows
        if rows is not None
        else {
            "cao-old": [terminal(last_active=NOW - timedelta(hours=2))],
        }
    )

    def record_teardown(label: str, workspace_id: str) -> None:
        calls.append((label, workspace_id))

    cleanup = RuntimeResourceCleanup(
        backend=backend or FakeHerdrBackend(inventories or [[workspace()], [workspace()]]),
        config_reader=lambda: config
        or CleanupConfig(
            completed_sessions_enabled=True,
            completed_session_grace_s=900,
            sweep_interval_s=300,
            max_sessions_per_sweep=5,
            preserve_patterns=[],
        ),
        terminal_reader=lambda session: terminal_rows.get(session, []),
        pending_checker=pending or (lambda _terminal_id: False),
        teardown=teardown or record_teardown,
        clock=lambda: NOW,
    )
    return cleanup, calls


def test_disabled_policy_and_tmux_backend_are_noops():
    disabled, disabled_calls = engine(
        config=CleanupConfig(completed_sessions_enabled=False),
    )
    disabled_summary = disabled.sweep()
    assert disabled_calls == []
    assert disabled_summary.reasons == {"disabled": 1}

    tmux, tmux_calls = engine(backend=object())
    tmux_summary = tmux.sweep()
    assert tmux_calls == []
    assert tmux_summary.reasons == {"unsupported_backend": 1}


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("blocked", id="blocked"),
        pytest.param("idle", id="idle"),
        pytest.param("working", id="working"),
        pytest.param("unknown", id="unknown"),
    ],
)
def test_every_nonterminal_native_state_is_preserved(status):
    cleanup, calls = engine(inventories=[[workspace(status=status)]])

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {"status_not_done": 1}


def test_duplicate_workspace_labels_fail_closed_for_entire_inventory():
    cleanup, calls = engine(
        inventories=[
            [
                workspace(workspace_id="ws-one"),
                workspace(workspace_id="ws-two"),
            ]
        ]
    )

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {"ambiguous_identity": 1}


@pytest.mark.parametrize(
    ("candidate", "rows", "patterns", "pending", "reason"),
    [
        pytest.param(workspace(label="personal"), {}, [], False, "outside_cao", id="prefix"),
        pytest.param(
            workspace(label="__peers__"),
            {},
            [],
            False,
            "outside_cao",
            id="peer-session",
        ),
        pytest.param(workspace(), {}, [], False, "no_terminals", id="zero-rows"),
        pytest.param(
            workspace(),
            {"cao-old": [terminal(provider="peer", last_active=NOW - timedelta(hours=2))]},
            [],
            False,
            "peer_terminal",
            id="provider-peer",
        ),
        pytest.param(workspace(), None, ["cao-*"], False, "preserved", id="preserve-pattern"),
        pytest.param(workspace(), None, [], True, "pending_inbox", id="pending-inbox"),
    ],
)
def test_ownership_delivery_and_preservation_gates(
    candidate,
    rows,
    patterns,
    pending,
    reason,
):
    config = CleanupConfig(
        completed_sessions_enabled=True,
        preserve_patterns=patterns,
    )
    cleanup, calls = engine(
        inventories=[[candidate]],
        rows=rows,
        config=config,
        pending=lambda _terminal_id: pending,
    )

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {reason: 1}


@pytest.mark.parametrize(
    ("last_active", "reason"),
    [
        pytest.param(None, "timestamp_invalid", id="missing"),
        pytest.param(
            NOW.replace(tzinfo=timezone.utc) - timedelta(hours=2),
            "timestamp_aware",
            id="aware",
        ),
        pytest.param(NOW + timedelta(seconds=1), "timestamp_future", id="future"),
        pytest.param(NOW - timedelta(seconds=899), "within_grace", id="within-grace"),
        pytest.param(NOW - timedelta(seconds=900), "within_grace", id="exact-cutoff"),
    ],
)
def test_timestamp_boundaries_fail_closed(last_active, reason):
    cleanup, calls = engine(
        rows={"cao-old": [terminal(last_active=last_active)]},
    )

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {reason: 1}


def test_newest_terminal_activity_controls_grace_boundary():
    cleanup, calls = engine(
        rows={
            "cao-old": [
                terminal("old", last_active=NOW - timedelta(hours=4)),
                terminal("recent", last_active=NOW - timedelta(minutes=5)),
            ]
        }
    )

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {"within_grace": 1}


def test_oldest_activity_then_label_order_and_batch_limit():
    inventories = [
        [
            workspace(label="cao-z", workspace_id="ws-z"),
            workspace(label="cao-b", workspace_id="ws-b"),
            workspace(label="cao-a", workspace_id="ws-a"),
        ],
        [workspace(label="cao-a", workspace_id="ws-a")],
        [workspace(label="cao-b", workspace_id="ws-b")],
    ]
    rows = {
        "cao-z": [terminal("z", "cao-z", last_active=NOW - timedelta(hours=1))],
        "cao-b": [terminal("b", "cao-b", last_active=NOW - timedelta(hours=3))],
        "cao-a": [terminal("a", "cao-a", last_active=NOW - timedelta(hours=3))],
    }
    config = CleanupConfig(
        completed_sessions_enabled=True,
        max_sessions_per_sweep=2,
    )
    cleanup, calls = engine(inventories=inventories, rows=rows, config=config)

    summary = cleanup.sweep()

    assert calls == [("cao-a", "ws-a"), ("cao-b", "ws-b")]
    assert summary.selected == 2
    assert summary.deleted == 2
    assert summary.reasons == {"batch_limited": 1}


@pytest.mark.parametrize(
    ("second_inventory", "expected_reason"),
    [
        pytest.param([workspace(status="idle")], "state_changed", id="state-change"),
        pytest.param(
            [workspace(workspace_id="ws-reused")],
            "identity_changed",
            id="label-reuse",
        ),
    ],
)
def test_revalidation_preserves_changed_state_or_identity(
    second_inventory,
    expected_reason,
):
    cleanup, calls = engine(inventories=[[workspace()], second_inventory])

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {expected_reason: 1}


def test_pending_inbox_arrival_during_revalidation_preserves():
    checks = iter([False, True])
    cleanup, calls = engine(pending=lambda _terminal_id: next(checks))

    summary = cleanup.sweep()

    assert calls == []
    assert summary.reasons == {"pending_inbox_changed": 1}


def test_authoritatively_absent_workspace_continues_persistence_cleanup():
    cleanup, calls = engine(inventories=[[workspace()], []])

    summary = cleanup.sweep()

    assert calls == [("cao-old", "ws-old")]
    assert summary.deleted == 1
    assert summary.reasons == {"workspace_absent": 1}


def test_teardown_failure_isolated_without_immediate_retry():
    attempts: list[str] = []

    def fail_first(label: str, _workspace_id: str) -> None:
        attempts.append(label)
        if label == "cao-a":
            raise RuntimeError("provider-output-secret")

    rows = {
        "cao-a": [terminal("a", "cao-a", last_active=NOW - timedelta(hours=3))],
        "cao-b": [terminal("b", "cao-b", last_active=NOW - timedelta(hours=2))],
    }
    initial = [
        workspace("cao-a", "ws-a"),
        workspace("cao-b", "ws-b"),
    ]
    cleanup, _ = engine(
        inventories=[
            initial,
            initial,
            initial,
        ],
        rows=rows,
        teardown=fail_first,
    )

    summary = cleanup.sweep()

    assert attempts == ["cao-a", "cao-b"]
    assert summary.deleted == 1
    assert summary.reasons == {"teardown_failed": 1}


def test_logs_never_include_identity_content_or_exception_text(caplog):
    def fail(_label: str, _workspace_id: str) -> None:
        raise RuntimeError("message-output-token-env-path-secret")

    cleanup, _ = engine(
        inventories=[
            [workspace("cao-session-secret", "workspace-secret")],
            [workspace("cao-session-secret", "workspace-secret")],
        ],
        rows={
            "cao-session-secret": [
                terminal(
                    "terminal-secret",
                    "cao-session-secret",
                    last_active=NOW - timedelta(hours=2),
                )
            ]
        },
        teardown=fail,
    )

    with caplog.at_level(logging.INFO):
        summary = cleanup.sweep()

    assert summary.reasons == {"teardown_failed": 1}
    assert "runtime_cleanup" in caplog.text
    for sentinel in (
        "session-secret",
        "workspace-secret",
        "terminal-secret",
        "message-output-token-env-path-secret",
    ):
        assert sentinel not in caplog.text

"""Opt-in isolated Herdr proof for completed-session runtime cleanup."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from test.e2e.test_exact_provider_supervisor_matrix import _stop_isolated_herdr
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from typing import Callable

import pytest
import requests


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    poll: float = 0.25,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    pytest.fail(f"condition did not become true within {timeout:.1f}s")


def _herdr_env(home_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    return env


def _workspace_inventory(home_dir: Path, herdr_session: str) -> list[dict]:
    result = subprocess.run(
        ("herdr", "--session", herdr_session, "workspace", "list"),
        env=_herdr_env(home_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return []
    return list(json.loads(result.stdout)["result"]["workspaces"])


def _workspace_status(
    home_dir: Path,
    herdr_session: str,
    label: str,
) -> str | None:
    for workspace in _workspace_inventory(home_dir, herdr_session):
        if workspace.get("label") == label:
            return str(workspace.get("agent_status"))
    return None


def _workspace_diagnostics(home_dir: Path, herdr_session: str, label: str) -> str:
    workspace = next(
        (
            candidate
            for candidate in _workspace_inventory(home_dir, herdr_session)
            if candidate.get("label") == label
        ),
        None,
    )
    if workspace is None:
        return "workspace_missing"
    panes = subprocess.run(
        (
            "herdr",
            "--session",
            herdr_session,
            "pane",
            "list",
            "--workspace",
            str(workspace["workspace_id"]),
        ),
        env=_herdr_env(home_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if panes.returncode != 0:
        return f"pane_list_failed:{panes.stderr.strip()}"
    try:
        pane_items = json.loads(panes.stdout)["result"]["panes"]
        pane_id = str(pane_items[0]["pane_id"])
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return f"pane_list_unparseable:{panes.stdout.strip()}"
    explained = subprocess.run(
        (
            "herdr",
            "--session",
            herdr_session,
            "agent",
            "explain",
            pane_id,
            "--json",
        ),
        env=_herdr_env(home_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if explained.returncode != 0:
        return f"agent_explain_failed:{explained.stderr.strip()}"
    return explained.stdout.strip()


def _workspace_pane_id(home_dir: Path, herdr_session: str, label: str) -> str:
    workspace = next(
        workspace
        for workspace in _workspace_inventory(home_dir, herdr_session)
        if workspace.get("label") == label
    )
    panes = subprocess.run(
        (
            "herdr",
            "--session",
            herdr_session,
            "pane",
            "list",
            "--workspace",
            str(workspace["workspace_id"]),
        ),
        env=_herdr_env(home_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return str(json.loads(panes.stdout)["result"]["panes"][0]["pane_id"])


def _report_completed_agent(
    home_dir: Path,
    herdr_session: str,
    label: str,
    *,
    focused_label: str,
) -> None:
    """Drive a non-focused agent through an observed working-to-idle cycle."""

    focused_workspace = next(
        workspace
        for workspace in _workspace_inventory(home_dir, herdr_session)
        if workspace.get("label") == focused_label
    )
    subprocess.run(
        (
            "herdr",
            "--session",
            herdr_session,
            "workspace",
            "focus",
            str(focused_workspace["workspace_id"]),
        ),
        env=_herdr_env(home_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    pane_id = _workspace_pane_id(home_dir, herdr_session, label)
    for sequence, state in ((1, "working"), (2, "idle")):
        subprocess.run(
            (
                "herdr",
                "--session",
                herdr_session,
                "pane",
                "report-agent",
                pane_id,
                "--source",
                "cao-cleanup-proof",
                "--agent",
                "proof-agent",
                "--state",
                state,
                "--seq",
                str(sequence),
            ),
            env=_herdr_env(home_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if state == "working":
            _wait_workspace_status(
                home_dir,
                herdr_session,
                label,
                "working",
                timeout=10,
            )


def _wait_workspace_status(
    home_dir: Path,
    herdr_session: str,
    label: str,
    expected: str,
    *,
    timeout: float,
) -> None:
    last_status: str | None = None

    def matches() -> bool:
        nonlocal last_status
        last_status = _workspace_status(home_dir, herdr_session, label)
        return last_status == expected

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if matches():
            return
        time.sleep(0.25)
    pytest.fail(
        f"workspace {label!r} did not reach {expected!r} within {timeout:.1f}s; "
        f"last status was {last_status!r}; diagnostics: "
        f"{_workspace_diagnostics(home_dir, herdr_session, label)}"
    )


def _create_mock_terminal(server_url: str, session_name: str) -> tuple[str, str]:
    response = requests.post(
        f"{server_url}/sessions",
        params={
            "provider": "mock_cli",
            "agent_profile": "developer",
            "session_name": session_name,
        },
        timeout=30,
    )
    assert response.status_code in {200, 201}, response.text
    payload = response.json()
    return str(payload["id"]), str(payload["session_name"])


def _age_terminals(db_path: Path, terminal_ids: list[str]) -> None:
    old = datetime.now() - timedelta(minutes=5)
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "UPDATE terminals SET last_active = ? WHERE id = ?",
            [(old, terminal_id) for terminal_id in terminal_ids],
        )
        connection.commit()


def _set_cleanup_enabled(settings_path: Path, enabled: bool) -> None:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["cleanup"]["completed_sessions_enabled"] = enabled
    settings_path.write_text(json.dumps(settings), encoding="utf-8")


def _receiver_orphan_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("""
                SELECT COUNT(*)
                FROM inbox AS message
                LEFT JOIN terminals AS receiver ON receiver.id = message.receiver_id
                WHERE receiver.id IS NULL
                """).fetchone()[0])


@pytest.fixture
def short_herdr_home() -> Path:
    """Keep the named-session Unix socket below macOS sun_path limits."""

    with tempfile.TemporaryDirectory(prefix="cao-herdr-", dir="/private/tmp") as directory:
        yield Path(directory)


@pytest.mark.e2e
def test_isolated_herdr_cleanup_and_empty_restart(short_herdr_home: Path) -> None:
    """Prove default-off, hot enablement, safety gates, and valid-empty restart."""

    if os.environ.get("CAO_RUN_HERDR_CLEANUP_PROOF") != "1":
        pytest.skip("set CAO_RUN_HERDR_CLEANUP_PROOF=1 for the isolated Herdr proof")

    home_dir = short_herdr_home

    settings_path = home_dir / ".aws" / "cli-agent-orchestrator" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "cleanup": {
                    "completed_sessions_enabled": False,
                    "completed_session_grace_s": 60,
                    "sweep_interval_s": 30,
                    "max_sessions_per_sweep": 5,
                    "preserve_patterns": ["cao-proof-preserve-*"],
                }
            }
        ),
        encoding="utf-8",
    )

    herdr_session = f"cao-cleanup-proof-{uuid.uuid4().hex[:8]}"
    server = _start_cao_server(
        home_dir,
        _pick_free_port(),
        extra_env={
            "CAO_TERMINAL_BACKEND": "herdr",
            "CAO_HERDR_SESSION": herdr_session,
            "XDG_CONFIG_HOME": str(home_dir / ".config"),
        },
        deadline=30,
    )

    session_names: list[str] = []
    first_phase_complete = False
    try:
        health = requests.get(f"{server.url}/health", timeout=5)
        assert health.status_code == 200
        assert health.json()["terminal_backend"] == "herdr"

        delete_id, delete_session = _create_mock_terminal(server.url, "cao-proof-delete-old")
        preserve_id, preserve_session = _create_mock_terminal(
            server.url, "cao-proof-preserve-active-supervisor"
        )
        pending_id, pending_session = _create_mock_terminal(server.url, "cao-proof-pending-old")
        idle_id, idle_session = _create_mock_terminal(server.url, "cao-proof-idle")
        sender_id, sender_session = _create_mock_terminal(server.url, "cao-proof-sender")
        session_names.extend(
            [
                delete_session,
                preserve_session,
                pending_session,
                idle_session,
                sender_session,
            ]
        )

        for label in (delete_session, preserve_session, pending_session):
            _report_completed_agent(
                home_dir,
                herdr_session,
                label,
                focused_label=sender_session,
            )
            _wait_workspace_status(
                home_dir,
                herdr_session,
                label,
                "done",
                timeout=20,
            )

        _age_terminals(
            server.db_path,
            [delete_id, preserve_id, pending_id, idle_id, sender_id],
        )
        with sqlite3.connect(server.db_path) as connection:
            connection.execute(
                """
                INSERT INTO inbox (sender_id, receiver_id, message, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (sender_id, pending_id, "redacted-proof-message", datetime.now()),
            )
            connection.commit()

        # The first sweep fires after 30 seconds but the feature remains off.
        time.sleep(32)
        assert _workspace_status(home_dir, herdr_session, delete_session) == "done"
        assert requests.get(f"{server.url}/terminals/{delete_id}", timeout=5).status_code == 200

        _set_cleanup_enabled(settings_path, True)
        _wait_until(
            lambda: _workspace_status(home_dir, herdr_session, delete_session) is None,
            timeout=40,
        )
        assert requests.get(f"{server.url}/terminals/{delete_id}", timeout=5).status_code == 404

        assert _workspace_status(home_dir, herdr_session, preserve_session) == "done"
        assert _workspace_status(home_dir, herdr_session, pending_session) == "done"
        assert _workspace_status(home_dir, herdr_session, idle_session) in {
            "idle",
            "unknown",
        }
        assert _receiver_orphan_count(server.db_path) == 0

        # Health remains responsive after the live sweep.
        started = time.monotonic()
        assert requests.get(f"{server.url}/health", timeout=2).status_code == 200
        assert time.monotonic() - started < 1.0

        for session_name in session_names:
            deleted = requests.delete(
                f"{server.url}/sessions/{session_name}",
                timeout=15,
            )
            assert deleted.status_code in {200, 404}, deleted.text
        _wait_until(
            lambda: _workspace_inventory(home_dir, herdr_session) == [],
            timeout=15,
        )
        session_names.clear()
        first_phase_complete = True
    finally:
        _set_cleanup_enabled(settings_path, False)
        for session_name in session_names:
            with contextlib.suppress(Exception):
                requests.delete(f"{server.url}/sessions/{session_name}", timeout=15)
        server.stop()
        if not first_phase_complete:
            _stop_isolated_herdr(home_dir, herdr_session)

    # With Herdr now authoritative-empty, a restart must reconcile a stale DB
    # receiver and its inbox row without inventing sessions or leaving orphans.
    with sqlite3.connect(server.db_path) as connection:
        connection.execute(
            """
            INSERT INTO terminals
                (id, tmux_session, tmux_window, provider, last_active)
            VALUES ('deadbeef', 'cao-proof-ghost', 'ghost-window', 'mock_cli', ?)
            """,
            (datetime.now(),),
        )
        connection.execute(
            """
            INSERT INTO inbox (sender_id, receiver_id, message, status, created_at)
            VALUES ('feedface', 'deadbeef', 'redacted-ghost-message', 'pending', ?)
            """,
            (datetime.now(),),
        )
        connection.commit()

    restarted = _start_cao_server(
        home_dir,
        _pick_free_port(),
        extra_env={
            "CAO_TERMINAL_BACKEND": "herdr",
            "CAO_HERDR_SESSION": herdr_session,
            "XDG_CONFIG_HOME": str(home_dir / ".config"),
        },
        deadline=30,
    )
    try:
        deadline = time.monotonic() + 10
        ghost_status = 0
        while time.monotonic() < deadline:
            ghost_status = requests.get(
                f"{restarted.url}/terminals/deadbeef",
                timeout=5,
            ).status_code
            if ghost_status == 404:
                break
            time.sleep(0.25)
        else:
            log_lines = restarted.log_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()[-80:]
            pytest.fail(
                f"startup reconciliation retained ghost with status {ghost_status}; "
                f"log tail: {' | '.join(log_lines)}"
            )
        assert _workspace_inventory(home_dir, herdr_session) == []
        assert _receiver_orphan_count(restarted.db_path) == 0
        assert requests.get(f"{restarted.url}/health", timeout=2).status_code == 200
    finally:
        restarted.stop()
        _stop_isolated_herdr(home_dir, herdr_session)

"""Opt-in exact-model supervisor matrix across Codex, Claude, Agy, and Kimi."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from test.fixtures.cao_server import (
    CaoServer,
    _pick_free_port,
    _start_cao_server,
)
from test.harness.live_supervisor_matrix import (
    LIVE_PROVIDER_LANES,
    LiveProviderLane,
    run_live_preflight,
)

import pytest
import requests

READY_STATES = {"idle", "completed"}
LANE_TIMEOUT_S = 900
FINAL_MARKER = "CAO_LIVE_FAN_IN_OK"
WORKER_MARKER = "CAO_LIVE_WORKER_OK"


@dataclass(frozen=True)
class LaneEvidence:
    """Content-free lane result suitable for one matrix fan-in verdict."""

    lane: str
    passed: bool
    reason: str


def _profile_text(lane: LiveProviderLane, *, supervisor: bool) -> str:
    name = lane.supervisor_profile if supervisor else lane.worker_profile
    role = "supervisor" if supervisor else "developer"
    prompt = (
        "You are the lifecycle-owning supervisor. Use assign exactly once with "
        f"agent_profile={lane.worker_profile!r}. Become idle so the callback can arrive. "
        "After the worker callback arrives, delete that worker terminal, then return exactly "
        f"{FINAL_MARKER}. Do not relaunch, retry, or change models. Keep all messages concise."
        if supervisor
        else "Complete the assigned smoke task, then call send_message with receiver_id omitted "
        f"and message exactly {WORKER_MARKER}. Do not spawn agents. Keep output concise."
    )
    codex_config = ""
    if lane.name == "codex":
        codex_config = (
            "\ncodexConfig:\n" '  model_reasoning_effort: "max"\n' '  service_tier: "priority"\n'
        )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Exact-model live {role} harness profile\n"
        f"provider: {lane.provider}\n"
        f"role: {role}\n"
        f"model: {lane.model}\n"
        "permissionMode: plan\n"
        "allowedTools:\n"
        '  - "@cao-mcp-server"\n'
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    command: cao-mcp-server\n"
        f"{codex_config}"
        "---\n"
        f"{prompt}\n"
    )


def _seed_exact_profiles(home_dir: Path) -> None:
    profile_dir = home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for lane in LIVE_PROVIDER_LANES:
        (profile_dir / f"{lane.supervisor_profile}.md").write_text(
            _profile_text(lane, supervisor=True),
            encoding="utf-8",
        )
        (profile_dir / f"{lane.worker_profile}.md").write_text(
            _profile_text(lane, supervisor=False),
            encoding="utf-8",
        )


def _terminal_status(server: CaoServer, terminal_id: str) -> str:
    response = requests.get(f"{server.url}/terminals/{terminal_id}", timeout=5)
    if response.status_code != 200:
        return "missing"
    return str(response.json().get("status", "unknown"))


def _session_terminals(server: CaoServer, session_name: str) -> list[dict]:
    response = requests.get(f"{server.url}/sessions/{session_name}", timeout=5)
    if response.status_code != 200:
        return []
    return list(response.json().get("terminals", []))


def _delivered_callbacks(server: CaoServer, supervisor_id: str) -> list[dict]:
    response = requests.get(
        f"{server.url}/terminals/{supervisor_id}/inbox/messages",
        params={"status": "delivered", "limit": 50},
        timeout=5,
    )
    if response.status_code != 200:
        return []
    return list(response.json())


def _full_output(server: CaoServer, terminal_id: str) -> str:
    response = requests.get(
        f"{server.url}/terminals/{terminal_id}/output",
        params={"mode": "full"},
        timeout=5,
    )
    if response.status_code != 200:
        return ""
    return str(response.json().get("output", ""))


def _stop_isolated_herdr(home_dir: Path, session_name: str) -> None:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    for command in (
        ("herdr", "session", "stop", session_name),
        ("herdr", "session", "delete", session_name),
    ):
        with contextlib.suppress(Exception):
            subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )


def _run_lane(server: CaoServer, lane: LiveProviderLane) -> LaneEvidence:
    session_name = f"cao-live-{lane.name}-{uuid.uuid4().hex[:8]}"
    supervisor_id: str | None = None
    try:
        create = requests.post(
            f"{server.url}/sessions",
            params={
                "provider": lane.provider,
                "agent_profile": lane.supervisor_profile,
                "session_name": session_name,
            },
            timeout=180,
        )
        if create.status_code not in {200, 201}:
            return LaneEvidence(lane=lane.name, passed=False, reason="supervisor_launch_failed")
        supervisor_id = str(create.json()["id"])

        deadline = time.monotonic() + LANE_TIMEOUT_S
        while time.monotonic() < deadline:
            status = _terminal_status(server, supervisor_id)
            if status in READY_STATES:
                break
            if status == "error":
                return LaneEvidence(
                    lane=lane.name,
                    passed=False,
                    reason="supervisor_initialization_error",
                )
            time.sleep(2)
        else:
            return LaneEvidence(
                lane=lane.name,
                passed=False,
                reason="supervisor_initialization_timeout",
            )

        task = (
            "Run the exact lifecycle smoke workflow from your profile. The worker task is: "
            "return the configured worker marker through send_message. After receiving it, "
            f"delete the worker and finish with {FINAL_MARKER}."
        )
        sent = requests.post(
            f"{server.url}/terminals/{supervisor_id}/input",
            params={"message": task},
            timeout=10,
        )
        if sent.status_code != 200:
            return LaneEvidence(lane=lane.name, passed=False, reason="task_delivery_failed")

        seen_worker_ids: set[str] = set()
        callback_seen = False
        while time.monotonic() < deadline:
            live_terminals = _session_terminals(server, session_name)
            seen_worker_ids.update(
                str(terminal["id"])
                for terminal in live_terminals
                if terminal.get("id") != supervisor_id
            )
            callback_seen = callback_seen or any(
                WORKER_MARKER in str(message.get("message", ""))
                for message in _delivered_callbacks(server, supervisor_id)
            )
            worker_deleted = bool(seen_worker_ids) and all(
                _terminal_status(server, worker_id) == "missing" for worker_id in seen_worker_ids
            )
            supervisor_done = _terminal_status(
                server, supervisor_id
            ) in READY_STATES and FINAL_MARKER in _full_output(server, supervisor_id)
            if callback_seen and worker_deleted and supervisor_done:
                return LaneEvidence(lane=lane.name, passed=True, reason="passed")
            if _terminal_status(server, supervisor_id) == "error":
                return LaneEvidence(lane=lane.name, passed=False, reason="supervisor_runtime_error")
            time.sleep(3)

        if not seen_worker_ids:
            reason = "worker_not_assigned"
        elif not callback_seen:
            reason = "callback_not_delivered"
        else:
            reason = "fan_in_or_cleanup_timeout"
        return LaneEvidence(lane=lane.name, passed=False, reason=reason)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return LaneEvidence(lane=lane.name, passed=False, reason="harness_boundary_failure")
    finally:
        with contextlib.suppress(Exception):
            requests.delete(f"{server.url}/sessions/{session_name}", timeout=30)


@pytest.mark.e2e
def test_exact_provider_supervisor_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run every exact lane once and emit one severity-neutral fan-in verdict."""

    if os.environ.get("CAO_LIVE_SUPERVISOR_MATRIX") != "1":
        pytest.skip("set CAO_LIVE_SUPERVISOR_MATRIX=1 to run the exact live matrix")

    port = _pick_free_port()
    preflight_env = dict(os.environ)
    preflight_env["CAO_OPS_MCP_URL"] = f"http://127.0.0.1:{port}/mcp/ops"
    report = run_live_preflight(
        Path.cwd(),
        environ=preflight_env,
    )
    report.require_ready()

    home_dir = tmp_path / "live-home"
    herdr_session = f"cao-live-matrix-{uuid.uuid4().hex[:8]}"
    _seed_exact_profiles(home_dir)
    server = _start_cao_server(
        home_dir,
        port,
        extra_env={
            "CAO_TERMINAL_BACKEND": "herdr",
            "CAO_HERDR_SESSION": herdr_session,
            "XDG_CONFIG_HOME": str(home_dir / ".config"),
        },
        deadline=30,
    )
    monkeypatch.setenv("CAO_OPS_MCP_URL", f"{server.url}/mcp/ops")

    try:
        health = requests.get(f"{server.url}/health", timeout=5)
        assert health.status_code == 200
        assert health.json()["terminal_backend"] == "herdr"

        evidence = [_run_lane(server, lane) for lane in LIVE_PROVIDER_LANES]
    finally:
        server.stop()
        _stop_isolated_herdr(home_dir, herdr_session)

    failures = [item for item in evidence if not item.passed]
    assert not failures, "exact provider matrix failed: " + ", ".join(
        f"{item.lane}={item.reason}" for item in failures
    )

"""Opt-in exact-model supervisor matrix across Codex, Claude, Agy, and Kimi."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from cli_agent_orchestrator.utils.atomic_write import atomic_write_text
from test.fixtures.cao_server import (
    CaoServer,
    _pick_free_port,
    _start_cao_server,
)
from test.harness.live_supervisor_matrix import (
    LiveProviderLane,
    run_live_preflight,
    select_live_lanes,
)

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


def _profile_text(lane: LiveProviderLane, *, supervisor: bool, port: int | None = None) -> str:
    name = lane.supervisor_profile if supervisor else lane.worker_profile
    role = "supervisor" if supervisor else "developer"
    prompt = (
        "You are the lifecycle-owning supervisor. Call the assign tool (assign or mcp__cao-mcp-server__assign) "
        f"exactly once with agent_profile={lane.worker_profile!r} and message='run smoke task'. "
        "Become idle so the callback can arrive in your inbox. "
        "After the worker callback arrives, delete that worker terminal using delete_terminal "
        f"(or mcp__cao-mcp-server__delete_terminal), then return exactly {FINAL_MARKER}. "
        "Do not relaunch, retry, or change models. Keep all messages concise."
        if supervisor
        else "Complete the assigned smoke task, then call send_message (or mcp__cao-mcp-server__send_message) "
        f"with receiver_id omitted and message exactly {WORKER_MARKER}. Do not spawn agents. Keep output concise."
    )
    codex_config = ""
    if lane.name == "codex":
        codex_config = (
            "\ncodexConfig:\n" '  model_reasoning_effort: "max"\n' '  service_tier: "priority"\n'
        )
    mcp_env = ""
    if port is not None:
        mcp_env = "    env:\n" f'      CAO_API_PORT: "{port}"\n' '      CAO_API_HOST: "127.0.0.1"\n'
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Exact-model live {role} harness profile\n"
        f"provider: {lane.provider}\n"
        f"role: {role}\n"
        f"model: {lane.model}\n"
        "permissionMode: bypassPermissions\n"
        "allowedTools:\n"
        '  - "@cao-mcp-server"\n'
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    command: cao-mcp-server\n"
        f"{mcp_env}"
        f"{codex_config}"
        "---\n"
        f"{prompt}\n"
    )


def _copy_provider_auth(home_dir: Path, lanes: tuple[LiveProviderLane, ...]) -> None:
    """Copy only explicit login state; never alias mutable operator config."""
    real_home = Path.home()
    provider_dirs = {"codex": ".codex", "grok_cli": ".grok"}
    for provider_dir in sorted(
        {provider_dirs[lane.provider] for lane in lanes if lane.provider in provider_dirs}
    ):
        source_dir = real_home / provider_dir
        target_dir = home_dir / provider_dir
        for name in ("auth.json", "version.json"):
            source = source_dir / name
            target = target_dir / name
            if source.is_file() and not target.exists():
                target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write_text(target, source.read_text(encoding="utf-8"))


def _seed_exact_profiles(
    home_dir: Path, lanes: tuple[LiveProviderLane, ...], port: int | None = None
) -> None:
    _copy_provider_auth(home_dir, lanes)
    profile_dir = home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        (profile_dir / f"{lane.supervisor_profile}.md").write_text(
            _profile_text(lane, supervisor=True, port=port),
            encoding="utf-8",
        )
        (profile_dir / f"{lane.worker_profile}.md").write_text(
            _profile_text(lane, supervisor=False, port=port),
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


def _remove_private_home(base_tmp: Path) -> None:
    """Retry a transient removal failure and never hide retained auth copies."""
    for attempt in range(2):
        try:
            shutil.rmtree(base_tmp)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt:
                raise RuntimeError(f"private matrix home cleanup failed: {base_tmp}") from None


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
            json={
                "env_vars": {
                    "CAO_API_PORT": str(server.port),
                    "CAO_API_HOST": "127.0.0.1",
                }
            },
            timeout=300,
        )
        if create.status_code not in {200, 201}:
            print(
                f"[{lane.name}] launch failed: status={create.status_code} "
                f"body_len={len(create.content)}",
                flush=True,
            )
            return LaneEvidence(
                lane=lane.name,
                passed=False,
                reason=f"supervisor_launch_failed_{create.status_code}",
            )
        supervisor_id = str(create.json()["id"])
        print(
            f"[{lane.name}] supervisor launched as terminal {supervisor_id}, awaiting idle...",
            flush=True,
        )

        deadline = time.monotonic() + LANE_TIMEOUT_S
        while time.monotonic() < deadline:
            status = _terminal_status(server, supervisor_id)
            if status in READY_STATES:
                break
            if status == "error":
                print(f"[{lane.name}] supervisor initialization error", flush=True)
                return LaneEvidence(
                    lane=lane.name,
                    passed=False,
                    reason="supervisor_initialization_error",
                )
            time.sleep(2)
        else:
            print(f"[{lane.name}] supervisor initialization timeout", flush=True)
            return LaneEvidence(
                lane=lane.name,
                passed=False,
                reason="supervisor_initialization_timeout",
            )

        print(f"[{lane.name}] supervisor ready (status={status}), delivering task...", flush=True)
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
            print(f"[{lane.name}] task delivery failed: {sent.status_code}", flush=True)
            return LaneEvidence(lane=lane.name, passed=False, reason="task_delivery_failed")
        delivered_at = time.monotonic()

        print(
            f"[{lane.name}] task delivered, awaiting worker assignment and callback...", flush=True
        )
        seen_worker_ids: set[str] = set()
        callback_seen = False
        last_progress_log = time.monotonic()
        while time.monotonic() < deadline:
            now = time.monotonic()
            elapsed = int(time.monotonic() - delivered_at)
            if now - last_progress_log >= 15:
                sup_st = _terminal_status(server, supervisor_id)
                workers_st = sorted(_terminal_status(server, wid) for wid in seen_worker_ids)
                output_len = len(_full_output(server, supervisor_id))
                callback_count = len(_delivered_callbacks(server, supervisor_id))
                print(
                    f"[{lane.name}] t+{elapsed}s sup_st={sup_st} workers_st={workers_st} "
                    f"callback_seen={callback_seen} output_len={output_len} "
                    f"callback_count={callback_count}",
                    flush=True,
                )
                last_progress_log = now

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
                print(
                    f"[{lane.name}] PASSED! (callback seen, worker deleted, supervisor finished)",
                    flush=True,
                )
                return LaneEvidence(lane=lane.name, passed=True, reason="passed")
            if _terminal_status(server, supervisor_id) == "error":
                print(f"[{lane.name}] supervisor runtime error", flush=True)
                return LaneEvidence(lane=lane.name, passed=False, reason="supervisor_runtime_error")
            if (
                _terminal_status(server, supervisor_id) in READY_STATES
                and not seen_worker_ids
                and elapsed > 45
            ):
                print(f"[{lane.name}] supervisor finished without assigning worker", flush=True)
                break
            time.sleep(3)

        if not seen_worker_ids:
            reason = "worker_not_assigned"
        elif not callback_seen:
            reason = "callback_not_delivered"
        else:
            reason = "fan_in_or_cleanup_timeout"
        return LaneEvidence(lane=lane.name, passed=False, reason=reason)
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        print(f"[{lane.name}] harness boundary failure: {type(exc).__name__}", flush=True)
        return LaneEvidence(
            lane=lane.name, passed=False, reason=f"harness_boundary_failure_{type(exc).__name__}"
        )
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
    lanes = select_live_lanes(preflight_env.get("CAO_LIVE_LANES"))

    base_tmp = Path(tempfile.mkdtemp(prefix="cao-h-", dir="/tmp"))
    home_dir = base_tmp / "lh"
    herdr_session = f"c-m-{uuid.uuid4().hex[:6]}"
    server = None
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        _seed_exact_profiles(home_dir, lanes, port=port)
        server = _start_cao_server(
            home_dir,
            port,
            extra_env={
                "CAO_TERMINAL_BACKEND": "herdr",
                "CAO_HERDR_SESSION": herdr_session,
                "XDG_CONFIG_HOME": str(home_dir / ".config"),
                "CAO_OPS_MCP_URL": f"http://127.0.0.1:{port}/mcp/ops",
                "CAO_API_PORT": str(port),
                "CAO_API_HOST": "127.0.0.1",
            },
            deadline=30,
        )
        monkeypatch.setenv("CAO_OPS_MCP_URL", f"{server.url}/mcp/ops")
        health = requests.get(f"{server.url}/health", timeout=5)
        assert health.status_code == 200
        assert health.json()["terminal_backend"] == "herdr"

        evidence = [_run_lane(server, lane) for lane in lanes]
    finally:
        try:
            try:
                if server is not None:
                    server.stop()
            finally:
                _stop_isolated_herdr(home_dir, herdr_session)
            if "evidence" in locals() and any(not item.passed for item in evidence):
                srv_log = home_dir / "server.log"
                server_log_len = srv_log.stat().st_size if srv_log.exists() else 0
                print(
                    f"matrix_failed=True server_log_present={srv_log.exists()} "
                    f"server_log_len={server_log_len}",
                    flush=True,
                )
        finally:
            _remove_private_home(base_tmp)

    failures = [item for item in evidence if not item.passed]
    assert not failures, "exact provider matrix failed: " + ", ".join(
        f"{item.lane}={item.reason}" for item in failures
    )

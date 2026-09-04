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
        mcp_env = (
            "    env:\n"
            f'      CAO_API_PORT: "{port}"\n'
            '      CAO_API_HOST: "127.0.0.1"\n'
        )
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


def _link_provider_configs(home_dir: Path) -> None:
    real_home = Path.home()
    for name in (".claude.json", ".claude", ".local"):
        source = real_home / name
        target = home_dir / name
        if source.exists() and not target.exists():
            with contextlib.suppress(Exception):
                target.symlink_to(source)
    gemini_src = real_home / ".gemini"
    gemini_tgt = home_dir / ".gemini"
    if gemini_src.exists():
        gemini_tgt.mkdir(parents=True, exist_ok=True)
        for item in gemini_src.iterdir():
            if item.name == "config":
                cfg_t = gemini_tgt / "config"
                cfg_t.mkdir(parents=True, exist_ok=True)
                for c_item in item.iterdir():
                    if c_item.name in {"mcp_config.json", "mcp_config.json.cao-ownership"}:
                        continue
                    t = cfg_t / c_item.name
                    if not t.exists():
                        with contextlib.suppress(Exception):
                            t.symlink_to(c_item)
                continue
            if item.name == "antigravity-cli":
                cli_t = gemini_tgt / "antigravity-cli"
                cli_t.mkdir(parents=True, exist_ok=True)
                for a_item in item.iterdir():
                    if a_item.name in {"mcp_config.json", "mcp_config.json.cao-ownership"}:
                        continue
                    t = cli_t / a_item.name
                    if not t.exists():
                        with contextlib.suppress(Exception):
                            t.symlink_to(a_item)
                continue
            t = gemini_tgt / item.name
            if not t.exists():
                with contextlib.suppress(Exception):
                    t.symlink_to(item)
    kimi_src = real_home / ".kimi-code"
    kimi_tgt = home_dir / ".kimi-code"
    if kimi_src.exists():
        kimi_tgt.mkdir(parents=True, exist_ok=True)
        for item in kimi_src.iterdir():
            if item.name in {"mcp.json", "mcp.json.bak"}:
                continue
            t = kimi_tgt / item.name
            if not t.exists():
                with contextlib.suppress(Exception):
                    t.symlink_to(item)
    codex_src = real_home / ".codex"
    codex_tgt = home_dir / ".codex"
    if codex_src.exists():
        codex_tgt.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "version.json"):
            s = codex_src / name
            t = codex_tgt / name
            if s.exists() and not t.exists():
                with contextlib.suppress(Exception):
                    t.symlink_to(s)
    cfg_dir = home_dir / ".config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    real_cfg = real_home / ".config"
    if real_cfg.exists():
        for name in ("gemini", "codex", ".claude", "agents"):
            source = real_cfg / name
            target = cfg_dir / name
            if source.exists() and not target.exists():
                with contextlib.suppress(Exception):
                    target.symlink_to(source)
    lib_dir = home_dir / "Library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("Application Support", "Keychains", "Preferences"):
        src = real_home / "Library" / sub
        tgt = lib_dir / sub
        if sub == "Application Support":
            tgt.mkdir(parents=True, exist_ok=True)
            claude_src = src / "Claude"
            claude_tgt = tgt / "Claude"
            if claude_src.exists() and not claude_tgt.exists():
                with contextlib.suppress(Exception):
                    claude_tgt.symlink_to(claude_src)
        elif src.exists() and not tgt.exists():
            with contextlib.suppress(Exception):
                tgt.symlink_to(src)


def _seed_exact_profiles(home_dir: Path, port: int | None = None) -> None:
    _link_provider_configs(home_dir)
    profile_dir = home_dir / ".aws" / "cli-agent-orchestrator" / "agent-store"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for lane in LIVE_PROVIDER_LANES:
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
            print(f"[{lane.name}] launch failed: {create.status_code} {create.text}", flush=True)
            return LaneEvidence(
                lane=lane.name,
                passed=False,
                reason=f"supervisor_launch_failed_{create.status_code}_{create.text}",
            )
        supervisor_id = str(create.json()["id"])
        print(f"[{lane.name}] supervisor launched as terminal {supervisor_id}, awaiting idle...", flush=True)

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

        print(f"[{lane.name}] task delivered, awaiting worker assignment and callback...", flush=True)
        seen_worker_ids: set[str] = set()
        callback_seen = False
        last_progress_log = time.monotonic()
        while time.monotonic() < deadline:
            now = time.monotonic()
            elapsed = int(now - (deadline - LANE_TIMEOUT_S))
            if now - last_progress_log >= 15:
                sup_st = _terminal_status(server, supervisor_id)
                workers_st = {wid: _terminal_status(server, wid) for wid in seen_worker_ids}
                tail = _full_output(server, supervisor_id)[-400:].replace("\n", " ")
                print(
                    f"[{lane.name}] t+{elapsed}s supervisor={sup_st} workers={workers_st} "
                    f"callback_seen={callback_seen} tail={tail!r}",
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
                print(f"[{lane.name}] PASSED! (callback seen, worker deleted, supervisor finished)", flush=True)
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
        print(f"[{lane.name}] harness boundary failure: {type(exc).__name__}: {exc}", flush=True)
        return LaneEvidence(lane=lane.name, passed=False, reason=f"harness_boundary_failure_{type(exc).__name__}")
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

    base_tmp = Path(tempfile.mkdtemp(prefix="cao-h-", dir="/tmp"))
    home_dir = base_tmp / "lh"
    home_dir.mkdir(parents=True, exist_ok=True)
    herdr_session = f"c-m-{uuid.uuid4().hex[:6]}"
    _seed_exact_profiles(home_dir, port=port)
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

    try:
        health = requests.get(f"{server.url}/health", timeout=5)
        assert health.status_code == 200
        assert health.json()["terminal_backend"] == "herdr"

        selected_lanes = os.environ.get("CAO_LIVE_LANES")
        lanes = [
            lane
            for lane in LIVE_PROVIDER_LANES
            if not selected_lanes or lane.name in {x.strip() for x in selected_lanes.split(",")}
        ]
        evidence = [_run_lane(server, lane) for lane in lanes]
    finally:
        server.stop()
        _stop_isolated_herdr(home_dir, herdr_session)
        if "evidence" in locals() and any(not item.passed for item in evidence):
            srv_log = home_dir / "server.log"
            if srv_log.exists():
                print(
                    f"\n--- SERVER LOG TAIL ---\n{srv_log.read_text(encoding='utf-8')[-3000:]}\n--- END SERVER LOG ---\n",
                    flush=True,
                )
        else:
            shutil.rmtree(base_tmp, ignore_errors=True)

    failures = [item for item in evidence if not item.passed]
    assert not failures, "exact provider matrix failed: " + ", ".join(
        f"{item.lane}={item.reason}" for item in failures
    )

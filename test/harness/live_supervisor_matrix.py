"""Strict preflight contract for the opt-in exact-provider supervisor matrix."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

PREFLIGHT_MARKER = "cao-live-preflight-ok"
DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024


class CommandResult(BaseModel):
    """Redactable command outcome used by injected preflight runners."""

    model_config = ConfigDict(extra="forbid", strict=True)

    returncode: int
    stdout: str = ""
    stderr: str = ""


class LiveProviderLane(BaseModel):
    """One exact provider/model lane; aliases and fallback models are forbidden."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: Literal["codex", "claude", "antigravity", "kimi", "grok"]
    provider: Literal["codex", "claude_code", "antigravity_cli", "kimi_cli", "grok_cli"]
    binary: Literal["codex", "claude", "agy", "kimi", "grok"]
    model: str = Field(min_length=1)
    supervisor_profile: str = Field(min_length=1)
    worker_profile: str = Field(min_length=1)
    auth_model_probe: tuple[str, ...]
    catalog_probe: tuple[str, ...] | None = None
    catalog_model_token: str | None = None

    @field_validator("model")
    @classmethod
    def reject_model_aliases(cls, value: str) -> str:
        if value in {"opus", "sonnet", "fable", "default", "latest"}:
            raise ValueError("live lanes require an exact model identifier")
        return value


LIVE_PROVIDER_LANES: tuple[LiveProviderLane, ...] = (
    LiveProviderLane(
        name="codex",
        provider="codex",
        binary="codex",
        model="gpt-5.6-sol",
        supervisor_profile="cao_codex_supervisor",
        worker_profile="cao_codex_worker",
        auth_model_probe=(
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--model",
            "gpt-5.6-sol",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            f"Reply exactly: {PREFLIGHT_MARKER}",
        ),
    ),
    LiveProviderLane(
        name="claude",
        provider="claude_code",
        binary="claude",
        model="claude-opus-5",
        supervisor_profile="cao_claude_supervisor",
        worker_profile="cao_claude_worker",
        auth_model_probe=(
            "claude",
            "--model",
            "claude-opus-5",
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--print",
            f"Reply exactly: {PREFLIGHT_MARKER}",
        ),
    ),
    LiveProviderLane(
        name="antigravity",
        provider="antigravity_cli",
        binary="agy",
        model="gemini-3.1-pro-high",
        supervisor_profile="cao_agy_supervisor",
        worker_profile="cao_agy_worker",
        auth_model_probe=(
            "agy",
            "--model",
            "gemini-3.1-pro-high",
            "--mode",
            "plan",
            "--print-timeout",
            "60s",
            "--print",
            f"Reply exactly: {PREFLIGHT_MARKER}",
        ),
        catalog_probe=("agy", "models"),
        catalog_model_token="gemini-3.1-pro-high",
    ),
    LiveProviderLane(
        name="kimi",
        provider="kimi_cli",
        binary="kimi",
        model="kimi-code/k3",
        supervisor_profile="cao_kimi_supervisor",
        worker_profile="cao_kimi_worker",
        auth_model_probe=(
            "kimi",
            "--model",
            "kimi-code/k3",
            "--prompt",
            f"Reply exactly: {PREFLIGHT_MARKER}",
        ),
        catalog_probe=("kimi", "provider", "list"),
        catalog_model_token="kimi-code/k3",
    ),
    LiveProviderLane(
        name="grok",
        provider="grok_cli",
        binary="grok",
        model="grok-4.6",
        supervisor_profile="cao_grok_supervisor",
        worker_profile="cao_grok_worker",
        auth_model_probe=(
            "grok",
            "--model",
            "grok-4.6",
            "-p",
            f"Reply exactly: {PREFLIGHT_MARKER}",
        ),
    ),
)


class PreflightFailure(BaseModel):
    """Categorical evidence only; command output and resolved URLs stay private."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    component: str
    reason: str


class PreflightReport(BaseModel):
    """Fan-in result for all host and provider prerequisites."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    checks_passed: int = Field(ge=0)
    failures: tuple[PreflightFailure, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.failures

    def require_ready(self) -> None:
        if self.ready:
            return
        details = "; ".join(f"{failure.component}:{failure.reason}" for failure in self.failures)
        raise RuntimeError(f"live supervisor matrix preflight failed: {details}")


CommandRunner = Callable[[tuple[str, ...], Path, int], CommandResult]


def _default_runner(command: tuple[str, ...], cwd: Path, timeout: int) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _is_loopback_http_url(value: str, *, expected_path: str | None = None) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    return expected_path is None or parsed.path == expected_path


def run_live_preflight(
    repo_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
    runner: CommandRunner = _default_runner,
    lanes: tuple[LiveProviderLane, ...] = LIVE_PROVIDER_LANES,
    command_timeout_s: int = 90,
) -> PreflightReport:
    """Validate every prerequisite without skipping, relaunching, or downgrading."""

    env = os.environ if environ is None else environ
    failures: list[PreflightFailure] = []
    passed = 0

    def record(component: str, ok: bool, reason: str) -> None:
        nonlocal passed
        if ok:
            passed += 1
        else:
            failures.append(PreflightFailure(component=component, reason=reason))

    record(
        "opt_in",
        env.get("CAO_LIVE_SUPERVISOR_MATRIX") == "1",
        "CAO_LIVE_SUPERVISOR_MATRIX must equal 1",
    )

    try:
        minimum_free = int(env.get("CAO_LIVE_MATRIX_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES))
    except ValueError:
        minimum_free = DEFAULT_MIN_FREE_BYTES
        record("disk", False, "minimum free-byte setting is invalid")
    else:
        record(
            "disk",
            disk_usage(repo_root).free >= minimum_free,
            "insufficient free disk capacity",
        )

    ops_url = env.get("CAO_OPS_MCP_URL", "")
    record(
        "cao_ops_mcp",
        _is_loopback_http_url(ops_url, expected_path="/mcp/ops"),
        "CAO_OPS_MCP_URL must be a loopback /mcp/ops URL without userinfo",
    )
    serena_url = env.get("CAO_SERENA_MCP_URL", "")
    record(
        "serena_mcp",
        _is_loopback_http_url(serena_url),
        "CAO_SERENA_MCP_URL must be a loopback HTTP URL without userinfo",
    )

    selected_lanes = env.get("CAO_LIVE_LANES")
    if selected_lanes:
        allowed = {x.strip() for x in selected_lanes.split(",") if x.strip()}
        target_lanes = tuple(lane for lane in lanes if lane.name in allowed)
    else:
        target_lanes = lanes

    record(
        "lanes",
        bool(target_lanes),
        "no matching provider lanes found for preflight",
    )

    record("herdr", which("herdr") is not None, "herdr binary is missing")
    for lane in target_lanes:
        record(
            f"{lane.name}.binary",
            which(lane.binary) is not None,
            "provider binary is missing",
        )

    if failures:
        return PreflightReport(checks_passed=passed, failures=tuple(failures))

    herdr_result = runner(("herdr", "--version"), repo_root, command_timeout_s)
    record("herdr.version", herdr_result.returncode == 0, "version probe failed")

    for lane in target_lanes:
        if lane.catalog_probe is not None:
            catalog = runner(lane.catalog_probe, repo_root, command_timeout_s)
            catalog_ok = (
                catalog.returncode == 0
                and lane.catalog_model_token is not None
                and any(lane.catalog_model_token in line for line in catalog.stdout.splitlines())
            )
            record(f"{lane.name}.model_catalog", catalog_ok, "exact model is unavailable")

        probe = runner(lane.auth_model_probe, repo_root, command_timeout_s)
        output = f"{probe.stdout}\n{probe.stderr}"
        record(
            f"{lane.name}.auth_model",
            probe.returncode == 0 and PREFLIGHT_MARKER in output,
            "exact-model authentication probe failed",
        )

    return PreflightReport(checks_passed=passed, failures=tuple(failures))

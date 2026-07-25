"""Tests for the strict live supervisor matrix preflight."""

from __future__ import annotations

import shutil
from pathlib import Path
from test.harness.live_supervisor_matrix import (
    LIVE_PROVIDER_LANES,
    PREFLIGHT_MARKER,
    CommandResult,
    LiveProviderLane,
    run_live_preflight,
)

import pytest
from pydantic import ValidationError


def _environment() -> dict[str, str]:
    return {
        "CAO_LIVE_SUPERVISOR_MATRIX": "1",
        "CAO_LIVE_MATRIX_MIN_FREE_BYTES": "1024",
        "CAO_OPS_MCP_URL": "http://127.0.0.1:9889/mcp/ops",
        "CAO_SERENA_MCP_URL": "http://127.0.0.1:8765/mcp",
    }


def _disk(_path: Path) -> shutil._ntuple_diskusage:
    return shutil._ntuple_diskusage(total=10_000, used=1_000, free=9_000)


def _success_runner(
    command: tuple[str, ...],
    _cwd: Path,
    _timeout: int,
) -> CommandResult:
    if command == ("agy", "models"):
        return CommandResult(returncode=0, stdout="gemini-3.1-pro-high\n")
    if command == ("kimi", "provider", "list"):
        return CommandResult(returncode=0, stdout="Default model: kimi-code/k3\n")
    return CommandResult(returncode=0, stdout=PREFLIGHT_MARKER)


def test_exact_matrix_preflight_passes_and_probes_every_lane(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        cwd: Path,
        timeout: int,
    ) -> CommandResult:
        commands.append(command)
        return _success_runner(command, cwd, timeout)

    report = run_live_preflight(
        tmp_path,
        environ=_environment(),
        which=lambda _binary: "/bin/fake",
        disk_usage=_disk,
        runner=runner,
    )

    assert report.ready is True
    for lane in LIVE_PROVIDER_LANES:
        assert lane.auth_model_probe in commands
        assert lane.model in lane.auth_model_probe


@pytest.mark.parametrize(
    ("mutation", "component"),
    [
        ({"CAO_LIVE_SUPERVISOR_MATRIX": "0"}, "opt_in"),
        ({"CAO_OPS_MCP_URL": "http://user:secret@127.0.0.1:9889/mcp/ops"}, "cao_ops_mcp"),
        ({"CAO_OPS_MCP_URL": "http://127.0.0.1:9889/wrong"}, "cao_ops_mcp"),
        ({"CAO_SERENA_MCP_URL": "https://example.com/mcp"}, "serena_mcp"),
    ],
)
def test_environment_contract_fails_closed(
    tmp_path: Path,
    mutation: dict[str, str],
    component: str,
) -> None:
    env = _environment()
    env.update(mutation)

    report = run_live_preflight(
        tmp_path,
        environ=env,
        which=lambda _binary: "/bin/fake",
        disk_usage=_disk,
        runner=_success_runner,
    )

    assert report.ready is False
    assert component in {failure.component for failure in report.failures}
    assert "secret" not in str(report.failures)


def test_missing_binary_stops_before_any_model_probe(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    report = run_live_preflight(
        tmp_path,
        environ=_environment(),
        which=lambda binary: None if binary == "kimi" else "/bin/fake",
        disk_usage=_disk,
        runner=lambda command, _cwd, _timeout: (
            commands.append(command) or CommandResult(returncode=0)
        ),
    )

    assert report.ready is False
    assert {failure.component for failure in report.failures} == {"kimi.binary"}
    assert commands == []


def test_catalog_rejects_silent_model_downgrade(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...],
        cwd: Path,
        timeout: int,
    ) -> CommandResult:
        if command == ("agy", "models"):
            return CommandResult(returncode=0, stdout="gemini-3.1-pro-low\n")
        return _success_runner(command, cwd, timeout)

    report = run_live_preflight(
        tmp_path,
        environ=_environment(),
        which=lambda _binary: "/bin/fake",
        disk_usage=_disk,
        runner=runner,
    )

    assert report.ready is False
    assert any(failure.component == "antigravity.model_catalog" for failure in report.failures)


def test_probe_failure_is_categorical_and_redacts_command_output(tmp_path: Path) -> None:
    sensitive_text = "provider-token-value"

    def runner(
        command: tuple[str, ...],
        cwd: Path,
        timeout: int,
    ) -> CommandResult:
        if command[0] == "claude":
            return CommandResult(returncode=1, stderr=sensitive_text)
        return _success_runner(command, cwd, timeout)

    report = run_live_preflight(
        tmp_path,
        environ=_environment(),
        which=lambda _binary: "/bin/fake",
        disk_usage=_disk,
        runner=runner,
    )

    assert report.ready is False
    assert any(failure.component == "claude.auth_model" for failure in report.failures)
    assert sensitive_text not in str(report.failures)
    with pytest.raises(RuntimeError, match="claude.auth_model"):
        report.require_ready()


def test_live_lane_rejects_model_alias() -> None:
    template = LIVE_PROVIDER_LANES[1].model_dump()
    template["model"] = "opus"

    with pytest.raises(ValidationError, match="exact model identifier"):
        LiveProviderLane.model_validate(template)

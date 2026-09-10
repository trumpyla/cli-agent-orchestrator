"""The live harness must not mutate the operator's configuration."""

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from test.e2e.test_exact_provider_supervisor_matrix import _copy_provider_auth
from test.e2e import test_exact_provider_supervisor_matrix as matrix
from test.harness.live_supervisor_matrix import LIVE_PROVIDER_LANES, select_live_lanes


def test_login_copies_and_claude_settings_cannot_write_through(tmp_path, monkeypatch):
    operator_home = tmp_path / "operator"
    isolated_home = tmp_path / "isolated"
    isolated_home.mkdir()
    settings = operator_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"userSetting": true}', encoding="utf-8")
    for provider in (".codex", ".grok"):
        folder = operator_home / provider
        folder.mkdir()
        (folder / "auth.json").write_text('{"test": "synthetic-login"}', encoding="utf-8")
        (folder / "trusted_folders.toml").write_text('trusted = ["/repo"]', encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: operator_home)
    _copy_provider_auth(isolated_home, LIVE_PROVIDER_LANES)
    assert not (isolated_home / ".claude").exists()
    for provider in (".codex", ".grok"):
        copied = isolated_home / provider / "auth.json"
        assert not copied.is_symlink()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert not (copied.parent / "trusted_folders.toml").exists()
        copied.write_text("changed", encoding="utf-8")
        assert (operator_home / provider / "auth.json").read_text() == '{"test": "synthetic-login"}'

    monkeypatch.setattr(Path, "home", lambda: isolated_home)
    ClaudeCodeProvider._ensure_skip_bypass_prompt_setting()
    assert settings.read_text() == '{"userSetting": true}'
    assert not (isolated_home / ".claude").is_symlink()


@pytest.mark.parametrize(
    "selection,expected", [("kimi", set()), ("codex", {".codex"}), ("grok", {".grok"})]
)
def test_only_selected_auth_is_copied(tmp_path, monkeypatch, selection, expected):
    operator_home = tmp_path / "operator"
    for provider in (".codex", ".grok"):
        folder = operator_home / provider
        folder.mkdir(parents=True)
        (folder / "auth.json").write_text("synthetic login")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.setattr(Path, "home", lambda: operator_home)
    _copy_provider_auth(isolated, select_live_lanes(selection))
    assert {path.name for path in isolated.iterdir()} == expected


def test_private_home_cleanup_retries_and_surfaces_failure(tmp_path, monkeypatch):
    private_home = tmp_path / "private"
    private_home.mkdir()
    remove = matrix.shutil.rmtree
    attempts = []

    def transient(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("synthetic")
        remove(path)

    monkeypatch.setattr(matrix.shutil, "rmtree", transient)
    matrix._remove_private_home(private_home)
    assert len(attempts) == 2
    assert not private_home.exists()
    private_home.mkdir()

    def permanent(path):
        raise PermissionError("sensitive diagnostic must not escape")

    monkeypatch.setattr(matrix.shutil, "rmtree", permanent)
    with pytest.raises(RuntimeError, match="private matrix home cleanup failed") as exc:
        matrix._remove_private_home(private_home)
    assert "sensitive diagnostic" not in str(exc.value)
    assert private_home.exists()


@pytest.mark.parametrize("stage", ["seed", "start", "run", "stop"])
def test_matrix_removes_private_home_after_each_failure(tmp_path, monkeypatch, stage):
    private_home = tmp_path / "private"
    private_home.mkdir()
    monkeypatch.setenv("CAO_LIVE_SUPERVISOR_MATRIX", "1")
    monkeypatch.setenv("CAO_LIVE_LANES", "codex")
    monkeypatch.setattr(matrix, "_pick_free_port", lambda: 12345)
    monkeypatch.setattr(
        matrix, "run_live_preflight", lambda *a, **k: SimpleNamespace(require_ready=lambda: None)
    )
    monkeypatch.setattr(matrix.tempfile, "mkdtemp", lambda **k: str(private_home))
    stops = []
    monkeypatch.setattr(matrix, "_stop_isolated_herdr", lambda *a: stops.append("herdr"))

    def seed(home, lanes, **kwargs):
        (home / "synthetic-auth").write_text("synthetic")
        if stage == "seed":
            raise RuntimeError("seed failed")

    def stop():
        stops.append("server")
        if stage == "stop":
            raise RuntimeError("stop failed")

    def start(*args, **kwargs):
        if stage == "start":
            raise RuntimeError("start failed")
        return SimpleNamespace(url="http://test.invalid", stop=stop)

    def run(*args):
        if stage == "run":
            raise RuntimeError("run failed")
        return matrix.LaneEvidence("codex", True, "ok")

    monkeypatch.setattr(matrix, "_seed_exact_profiles", seed)
    monkeypatch.setattr(matrix, "_start_cao_server", start)
    monkeypatch.setattr(matrix, "_run_lane", run)
    monkeypatch.setattr(
        matrix.requests,
        "get",
        lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: {"terminal_backend": "herdr"}
        ),
    )
    with pytest.raises(RuntimeError, match=f"{stage} failed"):
        matrix.test_exact_provider_supervisor_matrix(tmp_path, monkeypatch)
    assert not private_home.exists()
    assert "herdr" in stops
    if stage in {"run", "stop"}:
        assert "server" in stops

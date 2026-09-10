"""Kimi private runtime ownership survives manager loss and partial teardown."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kimi_cli import ProviderError
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.runtime_resource_cleanup import build_runtime_resource_cleanup
from test.services.test_runtime_cleanup_debt import teardown_world

_REAL_DISMANTLE = terminal_service.dismantle_terminal_runtime


@pytest.mark.parametrize("after_mkdir", [False, True])
def test_kill_at_directory_creation_preserves_restart_ownership(tmp_path, monkeypatch, after_mkdir):
    monkeypatch.setattr("cli_agent_orchestrator.providers.kimi_cli.CAO_HOME_DIR", tmp_path)
    original = KimiCliProvider("owned", "s", "w")
    target = original._runtime_directory()
    marker = target.parent / (target.name + ".owner")
    mkdir = Path.mkdir

    def killed(path, *args, **kwargs):
        if path == target:
            assert marker.read_text() == "owned"
            if after_mkdir:
                mkdir(path, *args, **kwargs)
            raise SystemExit("simulated process death")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", killed)
    with pytest.raises(SystemExit):
        original._prepare_runtime_directory()
    monkeypatch.setattr(Path, "mkdir", mkdir)
    restored = KimiCliProvider("owned", "s", "w")
    assert restored.cleanup() is True
    assert not target.exists()
    assert not marker.exists()
    assert restored._prepare_runtime_directory().is_dir()
    assert restored.cleanup() is True


def test_marker_publication_failure_creates_no_directory_and_can_retry(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr("cli_agent_orchestrator.providers.kimi_cli.CAO_HOME_DIR", tmp_path)
    provider = KimiCliProvider("owned", "s", "w")
    link = os.link
    monkeypatch.setattr(os, "link", MagicMock(side_effect=OSError("publication failed")))
    with pytest.raises(ProviderError, match="ownership preparation failed"):
        provider._prepare_runtime_directory()
    assert not provider._runtime_directory().exists()
    assert list(provider._runtime_root.iterdir()) == []
    monkeypatch.setattr(os, "link", link)
    assert provider._prepare_runtime_directory().is_dir()
    assert KimiCliProvider("owned", "s", "w").cleanup() is True


def test_missing_marker_never_adopts_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent_orchestrator.providers.kimi_cli.CAO_HOME_DIR", tmp_path)
    provider = KimiCliProvider("owned", "s", "w")
    provider._runtime_root.mkdir(mode=0o700)
    target = provider._runtime_directory()
    target.mkdir(mode=0o700)
    (target / "keep").write_text("unowned")
    with pytest.raises(ProviderError, match="ownership is unverified"):
        provider._prepare_runtime_directory()
    assert provider.cleanup() is False
    assert (target / "keep").read_text() == "unowned"
    assert not (target.parent / (target.name + ".owner")).exists()


def test_manager_loss_after_backend_close_retries_exact_kimi_runtime(
    tmp_path, monkeypatch, teardown_world
):
    backend, inventory, _, dispatch = teardown_world
    monkeypatch.setattr("cli_agent_orchestrator.providers.kimi_cli.CAO_HOME_DIR", tmp_path)
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "old").provider = "kimi_cli"
        db.commit()
    original = KimiCliProvider("old", "cao-old", "worker")
    owned = original._prepare_runtime_directory()
    (owned / "mcp.json").write_text("synthetic private config")
    (owned / "profile.txt").write_text("synthetic profile")
    other = KimiCliProvider("other", "other", "worker")._prepare_runtime_directory()
    (other / "keep").write_text("foreign")
    monkeypatch.setattr(terminal_service, "provider_manager", ProviderManager())
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", _REAL_DISMANTLE)
    monkeypatch.setattr(terminal_service, "get_herdr_inbox_service", lambda: None)
    monkeypatch.setattr(terminal_service.fifo_manager, "stop_reader", MagicMock())
    monkeypatch.setattr(terminal_service.status_monitor, "clear_terminal", MagicMock())
    real_rmtree = shutil.rmtree

    def partial_failure(path, *args, **kwargs):
        if Path(path) == owned:
            (owned / "profile.txt").unlink()
            raise OSError("synthetic partial removal failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partial_failure)
    first = build_runtime_resource_cleanup(backend=backend).sweep()
    assert first.deleted == 0
    assert inventory == []
    assert database.get_terminal_metadata("old") is not None
    assert database.list_runtime_cleanup_debt()
    assert (owned / "mcp.json").exists()

    monkeypatch.setattr(terminal_service, "provider_manager", ProviderManager())
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    second = build_runtime_resource_cleanup(backend=backend).sweep()
    assert second.deleted == 1
    assert database.get_terminal_metadata("old") is None
    assert database.list_runtime_cleanup_debt() == []
    assert not owned.exists()
    assert not (owned.parent / (owned.name + ".owner")).exists()
    assert (other / "keep").read_text() == "foreign"
    assert backend.close_workspace_by_id.call_count == 1
    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == 1


@pytest.mark.parametrize(
    "damage", ["foreign_path", "child_symlink", "marker_mismatch", "marker_symlink"]
)
def test_cleanup_refuses_unverified_target(tmp_path, monkeypatch, damage):
    monkeypatch.setattr("cli_agent_orchestrator.providers.kimi_cli.CAO_HOME_DIR", tmp_path)
    provider = KimiCliProvider("owned", "s", "w")
    target = provider._prepare_runtime_directory()
    marker = target.parent / (target.name + ".owner")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("keep")
    if damage == "foreign_path":
        provider._temp_dir = str(foreign)
    elif damage == "child_symlink":
        target.rmdir()
        target.symlink_to(foreign, target_is_directory=True)
    elif damage == "marker_mismatch":
        marker.write_text("someone else")
    else:
        marker.unlink()
        marker.symlink_to(foreign / "keep")
    provider._initialized = True
    assert provider.cleanup() is False
    assert provider._initialized is True
    assert (foreign / "keep").read_text() == "keep"

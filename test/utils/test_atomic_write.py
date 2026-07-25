"""Tests for owner-only, durable atomic settings-file replacement."""

import stat
from pathlib import Path
from typing import Callable

import pytest

from cli_agent_orchestrator.utils import atomic_write as atomic_write_module
from cli_agent_orchestrator.utils.atomic_write import atomic_write_text


@pytest.mark.parametrize(
    "failure_seam",
    [
        pytest.param("_write_payload", id="write"),
        pytest.param("_flush_file", id="flush"),
        pytest.param("_sync_fd", id="fsync"),
        pytest.param("_replace_path", id="replace"),
    ],
)
def test_atomic_write_failure_preserves_prior_file_byte_for_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_seam: str,
) -> None:
    """Every pre-replacement failure leaves the authoritative file untouched."""
    target = tmp_path / "settings.json"
    prior = b'{\n  "sentinel": "prior-bytes"\n}\n'
    target.write_bytes(prior)

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(f"injected {failure_seam} failure")

    monkeypatch.setattr(atomic_write_module, failure_seam, fail)

    with pytest.raises(OSError, match="injected"):
        atomic_write_text(target, '{"sentinel": "replacement"}')

    assert target.read_bytes() == prior
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


def test_atomic_write_replaces_content_with_owner_only_mode(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    atomic_write_text(target, '{"enabled": false}')

    assert target.read_text() == '{"enabled": false}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "save",
    [
        pytest.param(
            "cli_agent_orchestrator.services.settings_service._save",
            id="settings-service",
        ),
        pytest.param(
            "cli_agent_orchestrator.services.config_service._save_raw",
            id="config-service",
        ),
    ],
)
def test_settings_persistence_uses_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save: str,
) -> None:
    """Both configuration writers preserve old bytes when replacement fails."""
    from cli_agent_orchestrator.services import config_service, settings_service

    del config_service
    target = tmp_path / "settings.json"
    prior = b'{"sentinel":"old-formatting"}\n'
    target.write_bytes(prior)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", target)
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(atomic_write_module, "_replace_path", fail_replace)
    module_name, function_name = save.rsplit(".", 1)
    module = __import__(module_name, fromlist=[function_name])
    save_function: Callable[[dict[str, object]], None] = getattr(module, function_name)

    with pytest.raises(OSError, match="injected replace failure"):
        save_function({"sentinel": "new"})

    assert target.read_bytes() == prior

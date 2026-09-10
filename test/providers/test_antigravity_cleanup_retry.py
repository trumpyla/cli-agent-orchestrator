"""Incomplete Antigravity ownership cleanup retains lifecycle retry state."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.manager import ProviderManager


def _broken_config(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    remote = {"serverUrl": "https://example.invalid/mcp"}
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local-tid": {"command": "local", "env": {"CAO_TERMINAL_ID": "tid"}},
                    "remote-tid": remote,
                    "user": {"command": "keep"},
                }
            }
        )
    )
    journal = Path(f"{cfg}.cao-ownership-journal")
    journal.write_text("not-json")
    return cfg, journal, remote


def test_broken_journal_removes_only_independently_owned_entries(tmp_path):
    cfg, journal, remote = _broken_config(tmp_path)
    provider = AntigravityCliProvider("tid", "s", "w")
    provider._initialized = True
    provider._mcp_server_names = ["local-tid", "remote-tid"]
    with patch.object(provider, "_mcp_config_path", return_value=cfg):
        assert provider.cleanup() is False
    assert json.loads(cfg.read_text())["mcpServers"] == {
        "remote-tid": remote,
        "user": {"command": "keep"},
    }
    assert journal.read_text() == "not-json"
    assert provider._initialized is True
    assert provider._mcp_server_names == ["local-tid", "remote-tid"]


@pytest.mark.parametrize("ours_present", [False, True])
@pytest.mark.parametrize("sibling_owner", ["other", None, "", 123])
def test_broken_journal_cleanup_converges_with_proven_siblings(
    tmp_path, ours_present, sibling_owner
):
    cfg = tmp_path / "mcp_config.json"
    sibling = {"command": "keep", "env": {"CAO_TERMINAL_ID": sibling_owner}}
    servers = {"sibling": sibling}
    if ours_present:
        servers["ours"] = {"command": "local", "env": {"CAO_TERMINAL_ID": "tid"}}
    cfg.write_text(json.dumps({"mcpServers": servers}))
    journal = Path(f"{cfg}.cao-ownership-journal")
    journal.write_text("not-json")
    provider = AntigravityCliProvider("tid", "s", "w")
    provider._mcp_server_names = ["ours"]
    with patch.object(provider, "_mcp_config_path", return_value=cfg):
        assert [provider.cleanup() for _ in range(3)] == [sibling_owner == "other"] * 3
    assert json.loads(cfg.read_text())["mcpServers"] == {"sibling": sibling}
    assert journal.read_text() == "not-json"


@pytest.mark.asyncio
async def test_async_cleanup_failure_retained_until_successful_retry(tmp_path):
    cfg, journal, remote = _broken_config(tmp_path)
    provider = AntigravityCliProvider("tid", "s", "w")
    provider._initialized = True
    manager = ProviderManager()
    manager._providers["tid"] = provider
    with patch.object(provider, "_mcp_config_path", return_value=cfg):
        assert manager.cleanup_provider("tid") is False
        await provider._mcp_cleanup_future
        assert manager.cleanup_provider("tid") is False
        assert manager._providers["tid"] is provider
        assert provider._initialized is True
        journal.write_text(
            json.dumps(
                [
                    {
                        "name": "remote-tid",
                        "owner": "tid",
                        "digest": provider._mcp_entry_digest(remote),
                    }
                ]
            )
        )
        assert manager.cleanup_provider("tid") is False
        await provider._mcp_cleanup_future
        assert manager.cleanup_provider("tid") is True
    assert "tid" not in manager._providers
    assert provider._initialized is False
    assert json.loads(cfg.read_text())["mcpServers"] == {"user": {"command": "keep"}}


@pytest.mark.asyncio
async def test_async_unexpected_exception_does_not_dispose_provider():
    provider = AntigravityCliProvider("tid", "s", "w")
    provider._initialized = True
    manager = ProviderManager()
    manager._providers["tid"] = provider
    with patch.object(provider, "_unregister_mcp_servers", side_effect=OSError("test-failure")):
        assert manager.cleanup_provider("tid") is False
        with pytest.raises(OSError):
            await provider._mcp_cleanup_future
        assert manager.cleanup_provider("tid") is False
    assert manager._providers["tid"] is provider
    assert provider._initialized is True


@pytest.mark.parametrize("ownership_valid", [False, True])
def test_restart_restores_antigravity_cleanup_and_retains_failure(tmp_path, ownership_valid):
    cfg, journal, remote = _broken_config(tmp_path)
    if ownership_valid:
        journal.write_text(
            json.dumps(
                [
                    {
                        "name": "remote-tid",
                        "owner": "tid",
                        "digest": AntigravityCliProvider._mcp_entry_digest(remote),
                    }
                ]
            )
        )
    manager = ProviderManager()
    with (
        patch(
            "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
            return_value={
                "provider": "antigravity_cli",
                "tmux_session": "s",
                "tmux_window": "w",
            },
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        assert manager.cleanup_provider("tid") is ownership_valid
    if ownership_valid:
        assert "tid" not in manager._providers
        assert json.loads(cfg.read_text())["mcpServers"] == {"user": {"command": "keep"}}
    else:
        assert isinstance(manager._providers["tid"], AntigravityCliProvider)
        assert "remote-tid" in json.loads(cfg.read_text())["mcpServers"]

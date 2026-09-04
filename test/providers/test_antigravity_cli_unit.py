"""Unit tests for the Antigravity CLI (``agy``) provider."""

import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.antigravity_cli import (
    IDLE_FOOTER_PATTERN,
    PROCESSING_FOOTER_PATTERN,
    AntigravityCliProvider,
    ProviderError,
)

_REAL_HANDLE_STARTUP_DIALOG = AntigravityCliProvider._handle_startup_dialog

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Save before autouse replaces it with a MagicMock
_real_prune_stale_mcp_entries = AntigravityCliProvider._prune_stale_mcp_entries


@pytest.fixture(autouse=True)
def _skip_startup_dialog():
    # initialize() polls the pane to accept agy's workspace-trust dialog; that
    # path is exercised separately. Stub it so the init tests stay fast and
    # independent of the (mocked) get_history return type.
    # Also stub GC pruning — exercised in its own dedicated test; other tests
    # use fake terminal IDs that don't exist in the DB and would be pruned.
    with (
        patch.object(AntigravityCliProvider, "_handle_startup_dialog", return_value=None),
        patch.object(AntigravityCliProvider, "_prune_stale_mcp_entries"),
    ):
        yield


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_provider(
    terminal_id="test-tid",
    agent_profile=None,
    allowed_tools=None,
    model=None,
    skill_prompt=None,
) -> AntigravityCliProvider:
    return AntigravityCliProvider(
        terminal_id=terminal_id,
        session_name="test-session",
        window_name="window-0",
        agent_profile=agent_profile,
        allowed_tools=allowed_tools,
        model=model,
        skill_prompt=skill_prompt,
    )


@pytest.mark.asyncio
async def test_handle_startup_dialog_accepts_plan_mode_ready_surface(monkeypatch):
    """A ready plan prompt must end startup watching without burning the timeout."""
    provider = make_provider()
    calls = 0
    ready = (
        "────────────────────\n"
        "> Plan mode: research & plan only (shift+tab to cycle)\n"
        "────────────────────\n"
        "? for shortcuts        plan · Gemini 3.1 Pro · high"
    )

    class FakeBackend:
        def get_history(self, session, window):
            nonlocal calls
            calls += 1
            return ready

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.time.monotonic",
        lambda: 0.0,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.time.sleep", lambda _: None
    )

    await _REAL_HANDLE_STARTUP_DIALOG(provider, idle_gap=0.25, outer_timeout=1.0)

    assert calls == 1


# --------------------------------------------------------------------------- #
# Status detection (against captured live agy TUI fixtures)
# --------------------------------------------------------------------------- #


def test_status_idle_fixture():
    assert make_provider().get_status(load_fixture("agy_idle.txt")) == TerminalStatus.IDLE


def test_status_processing_fixture():
    assert (
        make_provider().get_status(load_fixture("agy_processing.txt")) == TerminalStatus.PROCESSING
    )


def test_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()  # _turns -> 1
    assert p.get_status(load_fixture("agy_completed.txt")) == TerminalStatus.COMPLETED


def test_status_idle_vs_completed_split_on_turns():
    # Same completed-looking footer is IDLE before the first delivered turn.
    p = make_provider()
    assert p.get_status(load_fixture("agy_completed.txt")) == TerminalStatus.IDLE


def test_status_empty_is_unknown():
    # native=None always falls through (no dispatch-timing guess); on tmux the
    # live-read fallback is a pass-through, so an empty buffer hits agy's own
    # no-output default (UNKNOWN) directly.
    assert make_provider().get_status("") == TerminalStatus.UNKNOWN
    assert make_provider().get_status(None) == TerminalStatus.UNKNOWN


def test_status_processing_takes_priority_over_idle_footer():
    # If both footers appear in the buffer, the live "esc to cancel" tail wins.
    buf = (
        "? for shortcuts\n" * 5
        + ("x" * 3000)
        + "\n⣽  Working...\nesc to cancel   Gemini 3.1 Pro (High)"
    )
    assert make_provider().get_status(buf) == TerminalStatus.PROCESSING


def test_status_waiting_user_answer():
    buf = "Do you want to allow this action? [y/n]\n"
    assert make_provider().get_status(buf) == TerminalStatus.WAITING_USER_ANSWER


def test_status_error():
    assert make_provider().get_status("Error: something exploded\n") == TerminalStatus.ERROR


# --------------------------------------------------------------------------- #
# pyte rendered-screen status detection (get_status_from_screen)
# --------------------------------------------------------------------------- #


def test_provider_opts_into_screen_detection():
    assert make_provider().supports_screen_detection is True


def test_screen_status_idle_when_only_ready_footer():
    # A composited viewport resolves in-place redraws: only the live footer
    # remains. The bottom row carries "? for shortcuts" ⇒ IDLE pre-first-turn.
    screen = [
        "> ",
        "─" * 80,
        "? for shortcuts                                  Gemini 3.1 Pro (High)",
    ]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.IDLE


def test_screen_status_completed_after_turn():
    p = make_provider()
    p.mark_input_received()
    screen = ["> hi", "  done", "─" * 80, "? for shortcuts        Gemini 3.1 Pro (High)"]
    assert p.get_status_from_screen(screen) == TerminalStatus.COMPLETED


def test_screen_status_processing_footer():
    screen = ["⣽  Working...", "esc to cancel        Gemini 3.1 Pro (High)"]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_screen_status_waiting_user_answer():
    screen = ["Do you want to allow this action? [y/n]"]
    assert make_provider().get_status_from_screen(screen) == TerminalStatus.WAITING_USER_ANSWER


def test_screen_status_empty_is_unknown():
    assert make_provider().get_status_from_screen([]) == TerminalStatus.UNKNOWN
    assert make_provider().get_status_from_screen(["   ", ""]) == TerminalStatus.UNKNOWN


def test_screen_resolves_stale_processing_footer_regression():
    """Regression for the init-timeout bug: the append-only stream keeps both
    footers when agy overwrites "esc to cancel" with "? for shortcuts", so the
    raw get_status() latches PROCESSING. A composited pyte viewport (the screen
    path) shows only the final ready footer ⇒ IDLE, so the session reaches
    ready and POST /sessions returns instead of timing out.
    """
    p = make_provider()
    # Raw append-only stream: stale "esc to cancel" survives below the response,
    # with the live ready footer rendered last — raw path wrongly says PROCESSING.
    raw = (
        "> analyze this\n  here is the analysis\n"
        + "esc to cancel   Gemini 3.1 Pro (High)\n"
        + ("\n" * 3)
        + "? for shortcuts   Gemini 3.1 Pro (High)\n"
    )
    assert p.get_status(raw) == TerminalStatus.PROCESSING

    # Composited viewport: the in-place rewrite is resolved, leaving only the
    # ready footer ⇒ IDLE.
    screen = [
        "> analyze this",
        "  here is the analysis",
        "─" * 80,
        "> ",
        "─" * 80,
        "? for shortcuts   Gemini 3.1 Pro (High)",
    ]
    assert p.get_status_from_screen(screen) == TerminalStatus.IDLE


# --------------------------------------------------------------------------- #
# Response extraction
# --------------------------------------------------------------------------- #


def test_extract_completed_response():
    assert (
        make_provider().extract_last_message_from_script(load_fixture("agy_completed.txt"))
        == "PONG"
    )


def test_extract_ignores_plan_mode_ready_placeholder_after_response():
    """The mounted plan prompt is input chrome, not the last echoed user query."""
    rule = "─" * 30
    script = (
        f"{rule}\n"
        "> /plan ^[[200~Reply exactly: AGY_INSTALLED_READY^[[201~\n"
        "▸ Thought for 5s, 335 tokens\n"
        "  Prioritizing Tool Usage\n"
        "  AGY_INSTALLED_READY\n"
        f"{rule}\n"
        "> Plan mode: research & plan only (shift+tab to cycle)\n"
        f"{rule}\n"
        "? for shortcuts   plan · Gemini 3.1 Pro · high\n"
    )

    assert make_provider().extract_last_message_from_script(script) == "AGY_INSTALLED_READY"


def test_extract_raises_without_query():
    with pytest.raises(ValueError):
        make_provider().extract_last_message_from_script("no query here\njust text\n")


def test_extract_filters_thought_and_tool_chrome():
    # Captured from a live agy reviewer turn that called cao-mcp-server.
    out = make_provider().extract_last_message_from_script(load_fixture("agy_review_completed.txt"))
    assert "▸" not in out  # thought-process header lines filtered
    assert "●" not in out  # tool-call lines filtered
    # The collapsed-thought title line that follows each "▸ Thought" header
    # ("Prioritizing Tool Usage" / "Prioritizing Tool Specificity") is chrome,
    # not response content, and must not leak into the extracted message.
    assert "Prioritizing" not in out
    assert "CRITICAL BUG" in out  # actual review content preserved
    assert "CHANGES_REQUESTED" in out


# --------------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------------- #


def test_build_command_raises_when_binary_missing():
    with patch("cli_agent_orchestrator.providers.antigravity_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError, match="not found"):
            make_provider()._build_agy_command()


def test_build_command_includes_skip_permissions_and_model():
    with patch(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        return_value="/usr/local/bin/agy",
    ):
        cmd = make_provider(model="Gemini 3.1 Pro (High)")._build_agy_command()
    assert cmd.startswith("agy --dangerously-skip-permissions")
    assert "--model" in cmd and "Gemini 3.1 Pro (High)" in cmd


def test_build_command_injects_system_prompt_via_i(tmp_path, monkeypatch):
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(agent_profile="reviewer_gemini")._build_agy_command()
    assert "-i" in cmd
    assert "You review code." in cmd
    assert "Acknowledge your role" in cmd


def test_mcp_registration_writes_config(tmp_path, monkeypatch):
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()
        import json

        data = json.loads(cfg.read_text())
        # Key is per-terminal: "{server_name}-{terminal_id}"
        assert "cao-mcp-server-test-tid" in data["mcpServers"]
        # CAO_TERMINAL_ID forwarded so cao-mcp-server can resolve the terminal.
        assert data["mcpServers"]["cao-mcp-server-test-tid"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
        # cleanup removes our entry without clobbering the file.
        p.cleanup()
        data2 = json.loads(cfg.read_text())
        assert "cao-mcp-server-test-tid" not in data2.get("mcpServers", {})


def test_mcp_registration_resolves_bundled_command(tmp_path, monkeypatch):
    """The bundled bare `cao-mcp-server` command is resolved to a PATH-
    independent invocation before it is written to mcp_config.json. agy reads
    that file at a later launch (persisted=True), so the stable PATH launcher
    is preferred over the versioned interpreter-sibling path."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": {"command": "cao-mcp-server", "args": []}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    MOD = "cli_agent_orchestrator.utils.mcp_resolution"
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        patch(f"{MOD}._sibling_script", return_value="/versioned/venv/bin/cao-mcp-server"),
        patch(f"{MOD}.shutil.which", return_value="/home/u/.local/bin/cao-mcp-server"),
    ):
        p._build_agy_command()
        import json

        entry = json.loads(cfg.read_text())["mcpServers"]["cao-mcp-server-test-tid"]
        # persisted=True prefers the stable PATH launcher, not the bare command
        # or the versioned sibling.
        assert entry["command"] == "/home/u/.local/bin/cao-mcp-server"
        assert entry["args"] == []


def test_mcp_registration_tracks_and_cleans_third_party_ownership(tmp_path):
    """Third-party entries stay identity-free but are still CAO-owned."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="developer_gemini",
        description="Developer",
        mcpServers={"github": {"command": "github-mcp", "args": ["serve"]}},
    )
    p = make_provider(agent_profile="developer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()

        data = json.loads(cfg.read_text())
        entry = data["mcpServers"]["github-test-tid"]
        assert "CAO_TERMINAL_ID" not in entry.get("env", {})

        ownership = json.loads(Path(f"{cfg}.cao-ownership").read_text())
        assert ownership == {"version": 1, "owners": {"github-test-tid": "test-tid"}}
        assert stat.S_IMODE(Path(f"{cfg}.cao-ownership").stat().st_mode) == 0o600

        p.cleanup()

    assert "github-test-tid" not in json.loads(cfg.read_text())["mcpServers"]
    assert json.loads(Path(f"{cfg}.cao-ownership").read_text()) == {
        "version": 1,
        "owners": {},
    }


def test_stale_prune_uses_sidecar_ownership_for_identity_free_entries(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"github-old": {"command": "github-mcp"}}}))
    Path(f"{cfg}.cao-ownership").write_text(
        json.dumps({"version": 1, "owners": {"github-old": "dead-terminal"}})
    )
    servers = json.loads(cfg.read_text())["mcpServers"]
    provider = make_provider()

    with (
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value=None,
        ),
    ):
        _real_prune_stale_mcp_entries(provider, servers)

    assert servers == {}
    assert json.loads(Path(f"{cfg}.cao-ownership").read_text()) == {
        "version": 1,
        "owners": {},
    }


def test_malformed_ownership_sidecar_does_not_delete_user_entry(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"user-server": {"command": "keep"}}}))
    Path(f"{cfg}.cao-ownership").write_text("not-json")
    servers = json.loads(cfg.read_text())["mcpServers"]
    provider = make_provider()

    with (
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            side_effect=AssertionError("unowned user entry was checked"),
        ),
    ):
        _real_prune_stale_mcp_entries(provider, servers)

    assert servers == {"user-server": {"command": "keep"}}


def test_mcp_cleanup_rehydrates_sidecar_owned_entry_after_restart(tmp_path):
    """A provider restored after restart can clean up without in-memory names."""
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"github-old": {"command": "github-mcp"}}}))
    Path(f"{cfg}.cao-ownership").write_text(
        json.dumps({"version": 1, "owners": {"github-old": "test-tid"}})
    )
    provider = make_provider()

    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        provider._unregister_mcp_servers()

    assert json.loads(cfg.read_text()) == {"mcpServers": {}}
    assert json.loads(Path(f"{cfg}.cao-ownership").read_text()) == {
        "version": 1,
        "owners": {},
    }


# --------------------------------------------------------------------------- #
# Misc lifecycle
# --------------------------------------------------------------------------- #


def test_exit_cli_is_quit():
    assert make_provider().exit_cli() == "/quit"


def test_mark_input_received_increments_turns():
    p = make_provider()
    assert p._turns == 0
    p.mark_input_received()
    assert p._turns == 1


def test_footer_patterns_smoke():
    import re

    assert re.search(PROCESSING_FOOTER_PATTERN, "esc to cancel")
    assert re.search(IDLE_FOOTER_PATTERN, "? for shortcuts")


def test_blocks_orchestrated_input_while_waiting_user_answer():
    # agy approval/picker prompts consume pasted text, so the provider opts in.
    assert make_provider().blocks_orchestrated_input_while_waiting_user_answer is True


def test_paste_submit_delay_is_1_5():
    """agy needs longer than BaseProvider's 0.3s delay for bracketed paste."""
    assert make_provider().paste_submit_delay == 1.5


def test_paste_enter_count_is_one():
    """agy uses one Enter instead of BaseProvider's Claude-oriented default of two."""
    assert make_provider().paste_enter_count == 1


def test_get_idle_pattern_for_log():
    pat = make_provider().get_idle_pattern_for_log()
    import re

    assert re.search(pat, "? for shortcuts")


def test_mcp_config_path_default_location():
    # Exercises the real (un-patched) path resolution.
    path = make_provider()._mcp_config_path()
    assert path.name == "mcp_config.json"
    assert path.parent.name == "config"
    assert path.parent.parent.name == ".gemini"


# --------------------------------------------------------------------------- #
# Command building — skill prompt + soft tool restriction
# --------------------------------------------------------------------------- #


def test_build_command_appends_security_prompt_when_tool_restricted():
    from cli_agent_orchestrator.constants import SECURITY_PROMPT
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini", description="Reviewer", system_prompt="You review code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        # Read-only reviewer: restricted tools -> security prompt is appended.
        cmd = make_provider(
            agent_profile="reviewer_gemini", allowed_tools=["fs_read", "fs_list"]
        )._build_agy_command()
    assert SECURITY_PROMPT.split("\n", 1)[0] in cmd


def test_build_command_includes_skill_catalog():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="developer_claude", description="Dev", system_prompt="You write code."
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(
            agent_profile="developer_claude", skill_prompt="## Available Skills\n- foo: bar"
        )._build_agy_command()
    assert "Available Skills" in cmd


def test_mcp_registration_accepts_pydantic_mcpserver(tmp_path):
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile, McpServer

    cfg = tmp_path / "mcp_config.json"
    # Pre-existing, non-CAO server must be preserved across merge.
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "keep"}}}))
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={"cao-mcp-server": McpServer(command="uvx", args=["cao-mcp-server"])},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["cao-mcp-server-test-tid"]["env"]["CAO_TERMINAL_ID"] == "test-tid"
    assert data["mcpServers"]["other"]["command"] == "keep"  # untouched


def test_mcp_registration_preserves_profile_entry_fields(tmp_path):
    """Agy registration only resolves runtime fields; it must retain MCP options."""
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        mcpServers={
            "cao-mcp-server": {
                "type": "stdio",
                "command": "uvx",
                "args": ["cao-mcp-server"],
                "timeout": 120_000,
            }
        },
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()

    entry = json.loads(cfg.read_text())["mcpServers"]["cao-mcp-server-test-tid"]
    assert entry["type"] == "stdio"
    assert entry["timeout"] == 120_000


def test_mcp_registration_recovers_from_corrupt_config(tmp_path):
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("{ this is not valid json")
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # should not raise; starts fresh
    data = json.loads(cfg.read_text())
    assert "cao-mcp-server-test-tid" in data["mcpServers"]


@pytest.mark.parametrize("payload", ["[1, 2, 3]", '"just a string"', "42"])
def test_mcp_registration_recovers_from_non_dict_config(tmp_path, payload):
    # The shared ~/.gemini/config/mcp_config.json may hold valid JSON of an
    # unexpected shape (list/string/number). Registration must normalize it
    # rather than raise AttributeError/TypeError on setdefault()/mutation.
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(payload)
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # must not raise
    data = json.loads(cfg.read_text())
    assert isinstance(data, dict)
    assert "cao-mcp-server-test-tid" in data["mcpServers"]


def test_mcp_registration_replaces_non_dict_mcpservers(tmp_path):
    # A dict root but a non-dict "mcpServers" value must be replaced, not mutated.
    import json

    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": ["unexpected", "list"]}))
    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(agent_profile="reviewer_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p._build_agy_command()  # must not raise
    data = json.loads(cfg.read_text())
    assert isinstance(data["mcpServers"], dict)
    assert "cao-mcp-server-test-tid" in data["mcpServers"]


def test_unregister_noop_when_nothing_registered(tmp_path):
    # No servers registered -> cleanup is a no-op, never touches the filesystem.
    cfg = tmp_path / "mcp_config.json"
    p = make_provider()
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p.cleanup()  # must not raise
        assert not cfg.exists()
    assert p._mcp_server_names == []


def test_unregister_handles_missing_file(tmp_path):
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server-test-tid"]
    missing = tmp_path / "does_not_exist.json"
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=missing):
        p.cleanup()  # file gone -> just resets state
    assert p._mcp_server_names == []


# --------------------------------------------------------------------------- #
# Extraction — chrome filtering + error paths
# --------------------------------------------------------------------------- #


_RULE = "─" * 30  # input-box separator (SEPARATOR_PATTERN needs >= 20)


def test_extract_filters_survey_banner_and_tip():
    script = (
        f"{_RULE}\n"
        "> what is 2+2\n"
        "  Antigravity CLI 1.0.10\n"  # banner chrome
        "  └ Tip: press something\n"  # tip chrome
        "  ⣽ Working...\n"  # spinner chrome
        "  How's the CLI experience?\n"  # survey chrome
        "  The answer is 4.\n"  # real content
        f"{_RULE}\n"
        ">\n"
    )
    out = make_provider().extract_last_message_from_script(script)
    assert out == "The answer is 4."


def test_extract_raises_on_empty_response():
    # A query followed only by chrome -> no content -> ValueError.
    script = f"{_RULE}\n> hello\n  ⣽ Working...\n{_RULE}\n>\n"
    with pytest.raises(ValueError, match="Empty"):
        make_provider().extract_last_message_from_script(script)


def test_extract_filters_stray_footer_in_body():
    script = f"{_RULE}\n> hi\n  ? for shortcuts\n  Hello there.\n{_RULE}\n>\n"
    assert make_provider().extract_last_message_from_script(script) == "Hello there."


def test_status_unknown_when_no_markers():
    # Non-empty buffer with no footer / spinner / error markers -> UNKNOWN.
    assert make_provider().get_status("just some banner text\n") == TerminalStatus.UNKNOWN


def test_build_command_raises_on_bad_profile():
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            make_provider(agent_profile="missing")._build_agy_command()


def test_profile_model_overrides_constructor_model():
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="reviewer_gemini",
        description="Reviewer",
        system_prompt="You review code.",
        model="Gemini 3.5 Flash (High)",
    )
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
    ):
        cmd = make_provider(
            agent_profile="reviewer_gemini", model="Gemini 3.1 Pro (High)"
        )._build_agy_command()
    assert "Gemini 3.5 Flash (High)" in cmd  # profile wins
    assert "Gemini 3.1 Pro (High)" not in cmd


def test_unregister_warns_on_corrupt_config(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("{ not valid json")
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server-test-tid"]
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p.cleanup()  # corrupt file -> logged warning, state reset, no raise
    assert p._mcp_server_names == []


@pytest.mark.parametrize("payload", ["[1, 2, 3]", '"just a string"', "42"])
def test_unregister_handles_non_dict_config(tmp_path, payload):
    # Valid-but-unexpected JSON shape must not raise during teardown, and the
    # internal state must always be cleared (finally block).
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(payload)
    p = make_provider()
    p._mcp_server_names = ["cao-mcp-server-test-tid"]
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p.cleanup()  # non-dict config -> no raise, state reset
    assert p._mcp_server_names == []


def test_unregister_skips_entry_owned_by_different_terminal(tmp_path):
    """_unregister_mcp_servers must only remove entries whose
    env.CAO_TERMINAL_ID matches this terminal — entries re-registered by
    a newer terminal under the same server name must survive."""
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cao-mcp-server-test-tid": {
                        "command": "cao-mcp-server",
                        "args": [],
                        "env": {"CAO_TERMINAL_ID": "new-terminal-99"},
                    },
                    "cao-ops-mcp-server-test-tid": {
                        "command": "cao-ops-mcp-server",
                        "args": [],
                        "env": {"CAO_TERMINAL_ID": "test-tid"},
                    },
                }
            }
        )
    )
    p = make_provider()  # terminal_id="test-tid"
    p._mcp_server_names = ["cao-mcp-server-test-tid", "cao-ops-mcp-server-test-tid"]
    with patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg):
        p._unregister_mcp_servers()
    result = json.loads(cfg.read_text())
    # "cao-mcp-server-test-tid" belongs to new-terminal-99 -> must survive
    assert "cao-mcp-server-test-tid" in result["mcpServers"]
    # "cao-ops-mcp-server-test-tid" belongs to test-tid -> must be removed
    assert "cao-ops-mcp-server-test-tid" not in result["mcpServers"]


# --------------------------------------------------------------------------- #
# Concurrent initialization — regression test for MCP config cross-wiring
# --------------------------------------------------------------------------- #


def test_concurrent_inits_get_distinct_mcp_keys(tmp_path):
    """Two providers initializing concurrently must each write their own
    per-terminal key into mcp_config.json, never overwriting the other's
    entry. Regression test for the race where a shared static key caused
    terminal B's write to clobber terminal A's entry before A's agy process
    read the config."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    profile = AgentProfile(
        name="dev_gemini",
        description="Dev",
        system_prompt="You develop.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p_a = make_provider(terminal_id="terminal-A", agent_profile="dev_gemini")
    p_b = make_provider(terminal_id="terminal-B", agent_profile="dev_gemini")
    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
    ):
        p_a._build_agy_command()
        p_b._build_agy_command()

        data = json.loads(cfg.read_text())
        servers = data["mcpServers"]
        # Each terminal has its own key
        assert "cao-mcp-server-terminal-A" in servers
        assert "cao-mcp-server-terminal-B" in servers
        # Each key carries the correct CAO_TERMINAL_ID
        assert servers["cao-mcp-server-terminal-A"]["env"]["CAO_TERMINAL_ID"] == "terminal-A"
        assert servers["cao-mcp-server-terminal-B"]["env"]["CAO_TERMINAL_ID"] == "terminal-B"
        # Cleanup of A doesn't touch B
        p_a._unregister_mcp_servers()
        data2 = json.loads(cfg.read_text())
        assert "cao-mcp-server-terminal-A" not in data2["mcpServers"]
        assert "cao-mcp-server-terminal-B" in data2["mcpServers"]


# --------------------------------------------------------------------------- #
# launch.py PROVIDERS_REQUIRING_WORKSPACE_ACCESS
# --------------------------------------------------------------------------- #


def test_antigravity_cli_in_workspace_access_set():
    # agy launches with --dangerously-skip-permissions, so it must trigger the
    # unrestricted-launch warning like the other full-access providers.
    from cli_agent_orchestrator.cli.commands.launch import (
        PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
    )

    assert "antigravity_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS


# --------------------------------------------------------------------------- #
# initialize() lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_initialize_success(monkeypatch):
    p = make_provider(model="Gemini 3.1 Pro (High)")

    sent = {}

    class FakeBackend:
        def send_keys(self, session, window, command):
            sent["command"] = command

    async def fake_wait_for_shell(tid, timeout):
        return True

    async def fake_wait_until_status(tid, statuses, timeout):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        lambda b: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_until_status",
        fake_wait_until_status,
    )
    monkeypatch.setattr(p, "wait_until_input_ready", AsyncMock(return_value=True))
    to_thread = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.asyncio.to_thread", to_thread
    )

    assert await p.initialize() is True
    assert p._initialized is True
    assert p.is_input_ready is True
    assert sent["command"].startswith("agy --dangerously-skip-permissions")
    assert to_thread.await_count == 2
    assert to_thread.await_args_list[0].args[0] == p._build_agy_command
    assert to_thread.await_args_list[1].args[0].__name__ == "send_keys"


@pytest.mark.asyncio
async def test_wait_until_input_ready_requires_stable_interactive_surface(monkeypatch):
    p = make_provider()
    ready = "────────────────────\n> \n────────────────────\n? for shortcuts"

    class FakeBackend:
        def get_history(self, session, window, tail_lines):
            return ready

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )

    assert await p.wait_until_input_ready(timeout=0.2, poll_interval=0.01) is True


@pytest.mark.asyncio
async def test_wait_until_input_ready_accepts_dynamic_plan_mode_prompt_panel(monkeypatch):
    """Agy 1.1.7 may repaint footer data around a ready plan-mode placeholder."""
    provider = make_provider()
    captures = [
        (
            "render tick 1\n"
            "────────────────────\n"
            "> Plan mode: research & plan only (shift+tab to cycle)\n"
            "────────────────────\n"
            "? for shortcuts        plan · Gemini 3.1 Pro · high"
        ),
        (
            "render tick 2\n"
            "────────────────────\n"
            "> Plan mode: research & plan only (shift+tab to cycle)\n"
            "────────────────────\n"
            "? for shortcuts        plan · Gemini 3.1 Pro · high · AI Credits: 16878"
        ),
    ]

    class FakeBackend:
        calls = 0

        def get_history(self, session, window, tail_lines):
            capture = captures[min(self.calls, len(captures) - 1)]
            self.calls += 1
            return capture

    backend = FakeBackend()
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: backend
    )

    assert await provider.wait_until_input_ready(timeout=0.2, poll_interval=0.01) is True


@pytest.mark.asyncio
async def test_wait_until_input_ready_offloads_backend_capture(monkeypatch):
    """A slow Herdr pane read must not block the server event loop."""
    p = make_provider()
    ready = "────────────────────\n> \n────────────────────\n? for shortcuts"

    class FakeBackend:
        def get_history(self, session, window, tail_lines):
            return ready

    backend = FakeBackend()
    to_thread = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.asyncio.to_thread", to_thread
    )

    assert await p.wait_until_input_ready(timeout=0.2, poll_interval=0.01) is True
    assert to_thread.await_count == 2


@pytest.mark.asyncio
async def test_wait_until_input_ready_retries_transient_capture_failure(monkeypatch):
    """One backend read failure must not consume the entire readiness timeout."""
    p = make_provider()
    ready = "────────────────────\n> \n────────────────────\n? for shortcuts"
    captures = [RuntimeError("transient read failure"), ready, ready]

    class FakeBackend:
        def get_history(self, session, window, tail_lines):
            value = captures.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )

    assert await p.wait_until_input_ready(timeout=0.2, poll_interval=0.01) is True


@pytest.mark.asyncio
async def test_wait_until_input_ready_caps_persistent_capture_failures(monkeypatch):
    """A dead backend must fail promptly instead of consuming the full init timeout."""
    p = make_provider()
    calls = 0

    class FakeBackend:
        def get_history(self, session, window, tail_lines):
            nonlocal calls
            calls += 1
            raise RuntimeError("pane is gone")

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )

    assert await p.wait_until_input_ready(timeout=0.05, poll_interval=0.0) is False
    assert calls == 3


@pytest.mark.asyncio
async def test_wait_until_input_ready_rejects_changing_startup_surface(monkeypatch):
    p = make_provider()
    counter = 0

    class FakeBackend:
        def get_history(self, session, window, tail_lines):
            nonlocal counter
            counter += 1
            return f"starting frame {counter}\n? for shortcuts"

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )

    assert await p.wait_until_input_ready(timeout=0.04, poll_interval=0.01) is False
    assert p.is_input_ready is False


@pytest.mark.asyncio
async def test_initialize_raises_when_input_surface_never_settles(monkeypatch):
    p = make_provider()

    class FakeBackend:
        def send_keys(self, session, window, command):
            pass

    async def always_true(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        lambda b: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", always_true
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_until_status", always_true
    )
    monkeypatch.setattr(p, "wait_until_input_ready", AsyncMock(return_value=False))

    with pytest.raises(TimeoutError, match="input surface"):
        await p.initialize()

    assert p.is_input_ready is False


@pytest.mark.asyncio
async def test_initialize_raises_when_shell_times_out(monkeypatch):
    async def fake_wait_for_shell(tid, timeout):
        return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    with pytest.raises(TimeoutError, match="Shell"):
        await make_provider().initialize()


@pytest.mark.asyncio
async def test_initialize_raises_when_agy_times_out(monkeypatch):
    class FakeBackend:
        def send_keys(self, session, window, command):
            pass

    async def fake_wait_for_shell(tid, timeout):
        return True

    async def fake_wait_until_status(tid, statuses, timeout):
        return False

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
        lambda b: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.get_backend", lambda: FakeBackend()
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_for_shell", fake_wait_for_shell
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.antigravity_cli.wait_until_status",
        fake_wait_until_status,
    )
    with pytest.raises(TimeoutError, match="initialization timed out"):
        await make_provider().initialize()


# --------------------------------------------------------------------------- #
# GC: prune stale MCP config entries (finding 2)
# --------------------------------------------------------------------------- #


def test_prune_stale_mcp_entries_on_register(tmp_path):
    """_register_mcp_servers prunes entries whose terminal is no longer live,
    preserves entries for live terminals and non-CAO entries, and adds the
    new terminal's entry."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    # Entry for a dead terminal — should be pruned
                    "cao-mcp-server-dead-tid": {
                        "command": "cao-mcp-server",
                        "args": [],
                        "env": {"CAO_TERMINAL_ID": "dead-tid"},
                    },
                    # Entry for a live terminal — should survive
                    "cao-mcp-server-live-tid": {
                        "command": "cao-mcp-server",
                        "args": [],
                        "env": {"CAO_TERMINAL_ID": "live-tid"},
                    },
                    # Non-CAO entry (no CAO_TERMINAL_ID) — should survive
                    "user-custom-server": {
                        "command": "my-server",
                        "args": [],
                    },
                }
            }
        )
    )
    profile = AgentProfile(
        name="dev_gemini",
        description="Dev",
        system_prompt="You develop.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(terminal_id="new-tid", agent_profile="dev_gemini")

    def fake_get_terminal_metadata(tid):
        # live-tid exists, dead-tid does not, new-tid is "us" (would also
        # return truthy from a real DB since we're registering it).
        return {"id": tid} if tid in ("live-tid", "new-tid") else None

    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        # Override the autouse no-op to let the real prune logic run
        patch.object(
            AntigravityCliProvider,
            "_prune_stale_mcp_entries",
            _real_prune_stale_mcp_entries,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            side_effect=fake_get_terminal_metadata,
        ),
    ):
        p._build_agy_command()

    data = json.loads(cfg.read_text())
    servers = data["mcpServers"]
    # Dead entry pruned
    assert "cao-mcp-server-dead-tid" not in servers
    # Live entry preserved
    assert "cao-mcp-server-live-tid" in servers
    # Non-CAO entry preserved
    assert "user-custom-server" in servers
    # New entry added
    assert "cao-mcp-server-new-tid" in servers
    assert servers["cao-mcp-server-new-tid"]["env"]["CAO_TERMINAL_ID"] == "new-tid"


def test_unreadable_mcp_config_preserves_existing_ownership_records(tmp_path):
    """When mcp_config.json cannot be parsed or read cleanly, _register_mcp_servers
    does NOT prune stale entries against an empty server dict, preserving the
    existing ownership records in the sidecar."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    cfg = tmp_path / "mcp_config.json"
    # Write invalid JSON simulating a concurrent write corruption or transient read error
    cfg.write_text("{corrupt-json-")

    # Set up pre-existing ownership sidecar
    sidecar = AntigravityCliProvider._mcp_ownership_path(cfg)
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "owners": {
                    "cao-mcp-server-existing-tid": "existing-tid",
                },
            }
        )
    )

    profile = AgentProfile(
        name="dev_gemini",
        description="Dev",
        system_prompt="You develop.",
        mcpServers={"cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]}},
    )
    p = make_provider(terminal_id="new-tid", agent_profile="dev_gemini")

    with (
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.shutil.which",
            return_value="/usr/local/bin/agy",
        ),
        patch(
            "cli_agent_orchestrator.providers.antigravity_cli.load_agent_profile",
            return_value=profile,
        ),
        patch.object(AntigravityCliProvider, "_mcp_config_path", return_value=cfg),
        patch.object(
            AntigravityCliProvider,
            "_prune_stale_mcp_entries",
            side_effect=_real_prune_stale_mcp_entries,
        ) as mock_prune,
    ):
        p._build_agy_command()

    # Verify _prune_stale_mcp_entries was NOT called because config was not loaded cleanly
    mock_prune.assert_not_called()

    # Verify the existing ownership record was preserved and the new one was added
    owners_data = json.loads(sidecar.read_text())
    assert owners_data["owners"]["cao-mcp-server-existing-tid"] == "existing-tid"
    assert owners_data["owners"]["cao-mcp-server-new-tid"] == "new-tid"


# --------------------------------------------------------------------------- #
# Cleanup future exception surfacing (finding 3)
# --------------------------------------------------------------------------- #


def test_cleanup_logs_unregister_exception(caplog):
    """When _unregister_mcp_servers raises on a worker thread, the exception
    is logged (not silently swallowed)."""
    import asyncio

    from cli_agent_orchestrator.providers.antigravity_cli import _log_cleanup_exception

    # Simulate a future that completed with an exception
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    fut.set_exception(OSError("disk full"))

    with caplog.at_level("ERROR", logger="cli_agent_orchestrator.providers.antigravity_cli"):
        _log_cleanup_exception(fut)

    assert "disk full" in caplog.text
    assert "_unregister_mcp_servers" in caplog.text
    loop.close()

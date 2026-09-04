"""Tests for Kimi CLI provider.

Covers initialization, status detection, message extraction, command building,
pattern matching, and cleanup — targeting >90% code coverage.
"""

import asyncio
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import McpServer
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.kimi_cli import (
    ANSI_CODE_PATTERN,
    ERROR_PATTERN,
    IDLE_PROMPT_PATTERN,
    IDLE_PROMPT_TAIL_LINES,
    RESPONSE_BULLET_PATTERN,
    STATUS_BAR_PATTERN,
    THINKING_BULLET_RAW_PATTERN,
    USER_INPUT_BOX_END_PATTERN,
    USER_INPUT_BOX_START_PATTERN,
    WELCOME_BANNER_PATTERN,
    KimiCliProvider,
    ProviderError,
    _is_startup_output_ready,
)


def _read_kimi_mcp(provider: KimiCliProvider) -> dict:
    """Read the per-terminal ``.kimi-code/mcp.json`` written under the temp cwd."""
    path = Path(provider._temp_dir) / ".kimi-code" / "mcp.json"
    return json.loads(path.read_text())


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    """Read a test fixture file."""
    return (FIXTURES_DIR / name).read_text()


# =============================================================================
# Initialization tests
# =============================================================================


class TestKimiCliProviderInitialization:
    """Tests for KimiCliProvider initialization flow."""

    @pytest.fixture(autouse=True)
    def _skip_startup_dialog(self):
        # initialize() polls the pane to dismiss kimi's upgrade-reminder dialog;
        # that path has its own tests. Stub it so command-send/timeout tests stay
        # fast and independent of the (mocked) get_history return type.
        with patch.object(KimiCliProvider, "_handle_startup_dialog", return_value=None):
            yield

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    async def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test successful initialization sends kimi command and reaches IDLE."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        result = await provider.initialize()

        assert result is True
        assert provider._initialized is True
        mock_tmux.return_value.send_keys.assert_called_once()
        mock_wait_shell.assert_called_once()
        mock_wait_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    async def test_initialize_offloads_blocking_startup_dialog(
        self, mock_backend, mock_wait_shell, mock_wait_status
    ):
        """A blocking startup-dialog poll must not stall the server event loop."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        events: list[str] = []
        release_handler = threading.Event()

        def blocking_dialog(*, outer_timeout: float) -> None:
            events.append("handler-enter")
            if not release_handler.wait(timeout=1.0):
                events.append("handler-timeout")
            events.append("handler-exit")

        async def prove_loop_progress() -> None:
            await asyncio.sleep(0)
            events.append("loop-progress")
            release_handler.set()

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        with patch.object(provider, "_handle_startup_dialog", side_effect=blocking_dialog):
            await asyncio.gather(provider.initialize(), prove_loop_progress())

        assert "handler-timeout" not in events
        assert events.index("loop-progress") < events.index("handler-exit")
        mock_backend.return_value.send_keys.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    async def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test shell init timeout raises TimeoutError."""
        mock_wait_shell.return_value = False

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Shell initialization"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    async def test_initialize_kimi_timeout(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test Kimi CLI init timeout raises TimeoutError."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        with pytest.raises(TimeoutError, match="Kimi CLI initialization"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    async def test_initialize_with_agent_profile(
        self, mock_load, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Interactive Kimi starts without v2-only agent-file flags."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a helpful assistant"
        mock_profile.mcpServers = None
        mock_profile.provider_init_timeout = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="developer")
        result = await provider.initialize()
        assert result is True

        # Kimi 0.29 limits --agent-file to the v2 non-interactive engine.
        call_args = mock_tmux.return_value.send_keys.call_args
        command = call_args[0][2]
        assert "--agent-file" not in command
        assert "--yolo" in command
        assert provider.prepare_input("Review this") == (
            "You are a helpful assistant\n\nReview this"
        )

        # Cleanup temp files
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_initialize_with_invalid_profile(self, mock_load):
        """Test initialization with invalid agent profile raises ProviderError."""
        mock_load.side_effect = FileNotFoundError("Profile not found")

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="nonexistent")
        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_kimi_command()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    async def test_initialize_with_mcp_servers(
        self, mock_load, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Kimi 0.29 delivers MCP via .kimi-code/mcp.json, never --mcp-config."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "npx",
                "args": ["-y", "cao-mcp-server"],
            }
        }
        mock_profile.provider_init_timeout = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="developer")

        with patch(
            "cli_agent_orchestrator.providers.kimi_cli.Path.home",
            return_value=Path(tempfile.mkdtemp()),
        ):
            result = await provider.initialize()
        assert result is True

        call_args = mock_tmux.return_value.send_keys.call_args
        command = call_args[0][2]
        # Kimi 0.29 has no --mcp-config flag; the server lands in the project file.
        assert "--mcp-config" not in command
        assert "--config" not in command
        assert "cao-mcp-server" in _read_kimi_mcp(provider)["mcpServers"]

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kimi_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kimi_cli.get_backend")
    async def test_initialize_sends_kimi_command(
        self, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        """Test that initialize sends the kimi --yolo command with cd and TERM override."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        await provider.initialize()

        call_args = mock_tmux.return_value.send_keys.call_args
        command = call_args[0][2]
        assert "cd " in command
        assert "TERM=xterm-256color" in command
        assert "kimi --yolo" in command
        provider.cleanup()


# =============================================================================
# Startup readiness tests
# =============================================================================


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(
            _read_fixture("kimi_cli_idle_output.txt"),
            True,
            id="legacy-idle",
        ),
        pytest.param(
            _read_fixture("kimi_cli_completed_output.txt"),
            True,
            id="legacy-completed",
        ),
        pytest.param(
            _read_fixture("kimi_code_tui_idle_raw.txt"),
            True,
            id="redesigned-idle",
        ),
        pytest.param(
            _read_fixture("kimi_code_tui_completed_raw.txt"),
            True,
            id="redesigned-completed",
        ),
        pytest.param(
            _read_fixture("kimi_cli_processing_output.txt"),
            False,
            id="legacy-processing",
        ),
        pytest.param(
            _read_fixture("kimi_code_tui_processing_raw.txt"),
            False,
            id="redesigned-processing",
        ),
        pytest.param(
            "[Enter] Upgrade now  [q] Not now  [s] Skip reminders for version 1.2.3",
            False,
            id="supported-upgrade-dialog",
        ),
        pytest.param(
            "[s] Skip reminders for version 1.2.3\n" + _read_fixture("kimi_code_tui_idle_raw.txt"),
            True,
            id="lingering-upgrade-text-with-ready-prompt",
        ),
        pytest.param(
            "Kimi needs input from an unsupported startup dialog",
            False,
            id="unsupported-dialog",
        ),
        pytest.param(
            "Kimi process launched but emitted no recognized TUI chrome",
            False,
            id="unknown-output",
        ),
    ],
)
def test_startup_ready_predicate(output: str, expected: bool) -> None:
    """Startup readiness is a pure classification over captured output."""
    assert _is_startup_output_ready(output) is expected


@pytest.mark.asyncio
async def test_startup_dialog_dismisses_supported_upgrade_then_accepts_ready() -> None:
    """The supported upgrade menu is answered once before ready is accepted."""
    backend = MagicMock()
    backend.get_history.side_effect = [
        "[Enter] Upgrade now  [q] Not now  [s] Skip reminders for version 1.2.3",
        _read_fixture("kimi_code_tui_idle_raw.txt"),
    ]
    provider = KimiCliProvider("term-1", "session-1", "window-1")

    with (
        patch("cli_agent_orchestrator.providers.kimi_cli.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.providers.kimi_cli.time.monotonic", return_value=0.0),
        patch("cli_agent_orchestrator.providers.kimi_cli.asyncio.sleep"),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.notify_input_sent"
        ) as notify_input_sent,
    ):
        await provider._handle_startup_dialog(idle_gap=1.0, outer_timeout=10.0)

    backend.send_keys.assert_called_once_with(
        "session-1",
        "window-1",
        "s",
        enter_count=0,
    )
    notify_input_sent.assert_called_once_with("term-1")


@pytest.mark.asyncio
async def test_startup_dialog_does_not_answer_unsupported_dialog() -> None:
    """Unknown startup dialogs fail closed without sending guessed input."""
    backend = MagicMock()
    backend.get_history.return_value = "Kimi needs input from an unsupported startup dialog"
    provider = KimiCliProvider("term-1", "session-1", "window-1")

    with (
        patch("cli_agent_orchestrator.providers.kimi_cli.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.providers.kimi_cli.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 2.0],
        ),
        patch("cli_agent_orchestrator.providers.kimi_cli.asyncio.sleep"),
    ):
        await provider._handle_startup_dialog(idle_gap=1.0, outer_timeout=1.0)

    backend.send_keys.assert_not_called()


# =============================================================================
# Status detection tests
# =============================================================================


class TestKimiCliProviderStatusDetection:
    """Tests for KimiCliProvider.get_status()."""

    def test_get_status_idle(self):
        """Test IDLE detection from fresh startup output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(_read_fixture("kimi_cli_idle_output.txt")) == TerminalStatus.IDLE

    def test_get_status_idle_no_thinking(self):
        """Test IDLE detection with ✨ prompt (no-thinking mode)."""
        output = (
            "Welcome to Kimi Code CLI!\n"
            "user@my-app✨\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding)  ctrl-x: toggle mode  context: 0.0%"
        )
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_completed(self):
        """Test COMPLETED detection when response is present with prompt."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert (
            provider.get_status(_read_fixture("kimi_cli_completed_output.txt"))
            == TerminalStatus.COMPLETED
        )

    def test_get_status_completed_complex(self):
        """Test COMPLETED detection with multi-line code response."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert (
            provider.get_status(_read_fixture("kimi_cli_complex_response.txt"))
            == TerminalStatus.COMPLETED
        )

    def test_get_status_processing(self):
        """Test PROCESSING detection when no prompt at bottom."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert (
            provider.get_status(_read_fixture("kimi_cli_processing_output.txt"))
            == TerminalStatus.PROCESSING
        )

    def test_get_status_unknown_empty(self):
        """Empty output -> UNKNOWN (native=None always falls through, no guess)."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_get_status_unknown_none(self):
        """Test UNKNOWN on None output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(None) == TerminalStatus.UNKNOWN

    def test_get_status_error_pattern(self):
        """Test ERROR detection from error output fixture."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert (
            provider.get_status(_read_fixture("kimi_cli_error_output.txt")) == TerminalStatus.ERROR
        )

    def test_get_status_idle_with_ansi_codes(self):
        """Test IDLE detection with ANSI escape codes in output."""
        # Simulate raw ANSI output: bold prompt with color codes
        output = (
            "\x1b[38;5;33mWelcome to Kimi Code CLI!\x1b[0m\n"
            "\x1b[1muser@my-app💫\x1b[0m\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 0.0%"
        )
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_idle_tall_terminal(self):
        """Test IDLE detection in tall terminals (46+ rows) where prompt is far from bottom.

        In a 46-row terminal, the welcome banner takes ~12 lines, the prompt is at
        line ~14, and there are ~32 empty padding lines before the status bar. The
        IDLE_PROMPT_TAIL_LINES must be large enough to reach the prompt.
        """
        # Simulate a 46-row terminal: welcome banner + prompt + 32 empty lines + status bar
        output = (
            "╭───────────────────────────────────╮\n"
            "│ Welcome to Kimi Code CLI!          │\n"
            "│ Send /help for help information.   │\n"
            "╰───────────────────────────────────╯\n"
            "user@project💫\n"
            + "\n" * 32  # 32 empty padding lines (typical for 46-row terminal)
            + "00:05  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 0.0%\n"
        )
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_processing_streaming(self):
        """Test PROCESSING when response is mid-stream (no prompt, no error)."""
        output = (
            "╭──────────────────╮\n"
            "│ write a function  │\n"
            "╰──────────────────╯\n"
            "• Here's the function:\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_get_status_completed_long_response_no_bullets(self):
        """Test COMPLETED for long structured responses without • bullet markers.

        Kimi doesn't always use • bullets — report templates, tables, numbered lists
        produce structured output with no bullets at all. The latching flag must detect
        the user input box during PROCESSING and remember it for COMPLETED detection.
        """
        provider = KimiCliProvider("term-1", "session-1", "window-1")

        # Step 1: During PROCESSING, the user input box is visible
        processing_output = (
            "╭──────────────────╮\n"
            "│ create a report    │\n"
            "╰──────────────────╯\n"
            "  Data Analysis Report Template\n"
            "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  1. Summary section...\n"
        )
        assert provider.get_status(processing_output) == TerminalStatus.PROCESSING
        # Flag should now be latched
        assert provider._has_received_input is True

        # Step 2: After completion, the user input box has scrolled out.
        # Output now shows only the tail end of the response + idle prompt.
        completed_output = (
            "  Appendix C: Code Reference\n"
            "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  [Reference to analysis code]\n"
            "user@project💫\n"
            "\n\n"
            "19:12  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 2.9%"
        )
        assert provider.get_status(completed_output) == TerminalStatus.COMPLETED

    def test_get_status_latching_persists_after_scrollout(self):
        """Test that _has_received_input flag persists after user input box scrolls out."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")

        # Simulate user input box detected during PROCESSING
        provider._has_received_input = True

        # Now output has idle prompt but NO user input box (scrolled out)
        output = (
            "  some response content\n"
            "user@project💫\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 1.0%"
        )
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_idle_before_any_input(self):
        """Test IDLE when no user input has been received yet (fresh startup)."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider._has_received_input is False

        output = (
            "Welcome to Kimi Code CLI!\n"
            "user@project💫\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 0.0%"
        )
        assert provider.get_status(output) == TerminalStatus.IDLE
        assert provider._has_received_input is False

    def test_get_status_processing_latches_flag(self):
        """Test that user input box detected during PROCESSING latches the flag."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider._has_received_input is False

        # PROCESSING output with user input box visible
        output = (
            "╭──────────────────╮\n"
            "│ hello               │\n"
            "╰──────────────────╯\n"
            "Response content streaming...\n"
        )
        status = provider.get_status(output)
        assert status == TerminalStatus.PROCESSING
        assert provider._has_received_input is True


# =============================================================================
# Message extraction tests
# =============================================================================


class TestKimiCliProviderMessageExtraction:
    """Tests for KimiCliProvider.extract_last_message_from_script()."""

    def test_extract_message_success(self):
        """Test successful message extraction from completed output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("kimi_cli_completed_output.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "greet" in result.lower() or "function" in result.lower()

    def test_extract_message_complex_response(self):
        """Test extraction of multi-line response with code block."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = _read_fixture("kimi_cli_complex_response.txt")
        result = provider.extract_last_message_from_script(output)

        assert len(result) > 0
        assert "Calculator" in result or "calculator" in result

    def test_extract_message_no_input(self):
        """Test ValueError when no content at all (not even response text)."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # Only idle prompt, no response content
        output = "user@my-app💫\n\n\n23:14  yolo  agent (kimi-for-coding)  context: 0.0%"
        with pytest.raises(ValueError, match="No extractable content"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_long_response_fallback(self):
        """Test fallback extraction when user input box scrolled out of capture.

        For long responses (>200 lines), the user input box is not visible in the
        tmux capture. The fallback extracts everything before the idle prompt.
        """
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # Simulate long response where user input box has scrolled out
        output = (
            "  3. Statistical Analysis Results\n"
            "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  Mean: 3.0, Median: 3.0, StdDev: 1.414\n"
            "  4. Conclusions\n"
            "  The data shows a normal distribution.\n"
            "user@my-app💫\n"
            "\n\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode  context: 2.9%"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Statistical Analysis" in result
        assert "Conclusions" in result
        assert "normal distribution" in result
        # Status bar should be filtered
        assert "yolo" not in result

    def test_extract_message_empty_response(self):
        """Test ValueError on empty response after input box."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ test message      │\n"
            "╰──────────────────╯\n"
            "user@my-app💫\n"
        )
        with pytest.raises(ValueError, match="Empty Kimi CLI response"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_filters_thinking(self):
        """Test that thinking bullets (gray ANSI) are filtered from output."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # Simulate raw output with thinking and response bullets
        output = (
            "╭──────────────────╮\n"
            "│ say hello          │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThe user wants a greeting.\x1b[0m\n"
            "• Hello! \U0001f44b\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)

        assert "Hello!" in result
        # Thinking text should be filtered out
        assert "The user wants" not in result

    def test_extract_message_multiple_responses(self):
        """Test extraction picks content from last user input box."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ first question     │\n"
            "╰──────────────────╯\n"
            "• First answer\n"
            "user@my-app💫\n"
            "╭──────────────────╮\n"
            "│ second question    │\n"
            "╰──────────────────╯\n"
            "• Second answer\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Second answer" in result

    def test_extract_message_no_trailing_prompt(self):
        """Test extraction works when there's no trailing prompt."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ what is python?    │\n"
            "╰──────────────────╯\n"
            "• Python is a programming language.\n"
            "• It supports multiple paradigms.\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Python" in result
        assert "paradigm" in result.lower()

    def test_extract_message_all_thinking_falls_back(self):
        """Test fallback when all lines are filtered as thinking."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        # All bullets are thinking (gray ANSI) — should fall back to returning all content
        output = (
            "╭──────────────────╮\n"
            "│ analyze this       │\n"
            "╰──────────────────╯\n"
            "\x1b[38;5;244m• \x1b[39m\x1b[3m\x1b[38;5;244mLet me analyze the code.\x1b[0m\n"
            "\x1b[38;5;244m• \x1b[39m\x1b[3m\x1b[38;5;244mI see several patterns.\x1b[0m\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        # Should return the thinking content as fallback
        assert "analyze" in result.lower() or "pattern" in result.lower()

    def test_extract_message_with_status_bar_filtered(self):
        """Test that status bar lines are filtered from extracted content."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        output = (
            "╭──────────────────╮\n"
            "│ hello               │\n"
            "╰──────────────────╯\n"
            "• Hi there!\n"
            "23:14  yolo  agent (kimi-for-coding, thinking)  ctrl-x: toggle mode\n"
            "user@my-app💫\n"
        )
        result = provider.extract_last_message_from_script(output)
        assert "Hi there!" in result
        assert "yolo" not in result
        assert "ctrl-x" not in result


# =============================================================================
# Command building tests
# =============================================================================


class TestKimiCliProviderBuildCommand:
    """Tests for KimiCliProvider._build_kimi_command()."""

    def test_build_command_no_profile(self):
        """Test command without agent profile includes cd, TERM override, and kimi --yolo."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        command = provider._build_kimi_command()
        assert "cd " in command
        assert "TERM=xterm-256color" in command
        assert "kimi --yolo" in command
        assert provider._temp_dir is not None
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_system_prompt(self, mock_load):
        """Profile prompts are deferred to first input for interactive Kimi."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are a developer"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "kimi" in command
        assert "--yolo" in command
        assert "--agent-file" not in command
        assert provider.prepare_input("Implement it") == "You are a developer\n\nImplement it"
        assert provider.commit_prepared_input() is True
        assert provider.prepare_input("Second turn") == "Second turn"
        # Temp directory should be created
        assert provider._temp_dir is not None

        # Cleanup
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_mcp_config(self, mock_load, tmp_path):
        """MCP command entries land in .kimi-code/mcp.json; only the
        orchestration server receives CAO_TERMINAL_ID."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "uvx", "args": ["cao-mcp-server"]},
            "test-server": {"command": "npx", "args": ["test"]},
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            command = provider._build_kimi_command()

        assert "--mcp-config" not in command
        # No --config flag (modifies config.toml directly to avoid breaking OAuth)
        assert "--config" not in command
        servers = _read_kimi_mcp(provider)["mcpServers"]
        assert servers["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "term-1"
        assert servers["test-server"]["command"] == "npx"
        assert "CAO_TERMINAL_ID" not in (servers["test-server"].get("env") or {})
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_resolves_bundled_mcp_command(self, mock_load, tmp_path):
        """The bare cao-mcp-server command is resolved to a PATH-independent
        invocation in the written .kimi-code/mcp.json (wiring guard: a refactor
        that drops the resolve_mcp_server_config call must fail this test)."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"type": "stdio", "command": "cao-mcp-server", "args": []}
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        MOD = "cli_agent_orchestrator.utils.mcp_resolution"
        with (
            patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path),
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            provider._build_kimi_command()

        entry = _read_kimi_mcp(provider)["mcpServers"]["cao-mcp-server"]
        assert entry["command"] == "/venv/bin/cao-mcp-server"
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_does_not_create_v2_agent_files(self, mock_load):
        """Interactive Kimi 0.29 must not receive v2-only agent-file configuration."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Custom system prompt"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert provider._temp_dir is not None
        assert "--agent-file" not in command
        assert not os.path.exists(os.path.join(provider._temp_dir, "agent.yaml"))
        assert not os.path.exists(os.path.join(provider._temp_dir, "system.md"))
        assert provider.prepare_input("Task") == "Custom system prompt\n\nTask"

        # Cleanup
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_uses_plan_mode_for_read_only_profile(self, mock_load):
        """Reviewer profiles use Kimi's interactive plan mode as a native guardrail."""
        mock_profile = MagicMock()
        mock_profile.model = "kimi-code/k3"
        mock_profile.system_prompt = "Review only."
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="reviewer",
            allowed_tools=["@builtin", "fs_read", "fs_list", "@cao-mcp-server"],
        )

        command = provider._build_kimi_command()

        assert "--plan" in command
        assert "--yolo" not in command
        assert "--agent-file" not in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_uses_plan_mode_for_explicit_empty_allowlist(self, mock_load):
        """An explicit deny-all policy must never become Kimi yolo mode."""
        mock_profile = MagicMock()
        mock_profile.model = "kimi-code/k3"
        mock_profile.system_prompt = "Review only."
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="reviewer",
            allowed_tools=[],
        )

        command = provider._build_kimi_command()

        assert "--plan" in command
        assert "--yolo" not in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_explicit_plan_mode_cannot_be_overridden_by_write_tools(self, mock_load):
        """A profile's explicit safety mode takes precedence over inferred capabilities."""
        mock_profile = MagicMock()
        mock_profile.model = "kimi-code/k3"
        mock_profile.system_prompt = "Plan only."
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "plan"
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="reviewer",
            allowed_tools=["fs_read", "fs_write", "execute_bash"],
        )

        command = provider._build_kimi_command()

        assert "--plan" in command
        assert "--yolo" not in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_explicit_bypass_mode_allows_unattended_mcp_callbacks(self, mock_load):
        """Explicit bypass mode must override inferred read-only capabilities."""
        mock_profile = MagicMock()
        mock_profile.model = "kimi-code/k3"
        mock_profile.system_prompt = "Callback worker."
        mock_profile.mcpServers = None
        mock_profile.permissionMode = "bypassPermissions"
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="reviewer",
            allowed_tools=["fs_read", "fs_list", "@cao-mcp-server"],
        )

        command = provider._build_kimi_command()

        assert "--yolo" in command
        assert "--plan" not in command
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_with_pydantic_mcp_config(self, mock_load, tmp_path):
        """MCP servers supplied as Pydantic model objects narrow and serialize."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": McpServer(command="cao-mcp-server", args=[]),
            "my-server": McpServer(command="node", args=["server.js"]),
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            command = provider._build_kimi_command()

        assert "--mcp-config" not in command
        servers = _read_kimi_mcp(provider)["mcpServers"]
        assert servers["my-server"]["command"] == "node"
        # Only the orchestration server carries this terminal's identity.
        assert servers["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "term-1"
        assert "CAO_TERMINAL_ID" not in (servers["my-server"].get("env") or {})
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env(self, mock_load, tmp_path):
        """Test that CAO_TERMINAL_ID injection preserves existing env vars."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "cao-mcp-server",
                "args": [],
                "env": {"MY_VAR": "my_value"},
            }
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("abc123", "session-1", "window-1", agent_profile="dev")
        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            provider._build_kimi_command()

        config = _read_kimi_mcp(provider)["mcpServers"]
        assert config["cao-mcp-server"]["env"]["MY_VAR"] == "my_value"
        assert config["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "abc123"
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_replaces_stale_terminal_id(self, mock_load, tmp_path):
        """A CAO_TERMINAL_ID carried in the profile is stale and is replaced.

        The created terminal's id is authoritative; keeping the profile's value
        would make this terminal's callbacks attribute to another terminal.
        """
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "command": "cao-mcp-server",
                "args": [],
                "env": {"CAO_TERMINAL_ID": "stale-id"},
            }
        }
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("new-id", "session-1", "window-1", agent_profile="dev")
        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            provider._build_kimi_command()

        config = _read_kimi_mcp(provider)["mcpServers"]
        assert config["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "new-id"
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_tool_timeout(self, mock_load, tmp_path):
        """Test that MCP tool timeout is set to 600s in config.toml when MCP servers present.

        Uses class-level flag to ensure config is modified only once per process,
        avoiding race conditions when multiple workers are created in parallel.
        """
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "uv", "args": ["run", "cao-mcp-server"]}
        }
        mock_load.return_value = mock_profile

        # Create a fake config.toml
        fake_kimi_dir = tmp_path / ".kimi"
        fake_kimi_dir.mkdir()
        config_file = fake_kimi_dir / "config.toml"
        config_file.write_text("[mcp.client]\ntool_call_timeout_ms = 60000\n")

        # Reset class-level flag so test runs the config modification
        KimiCliProvider._mcp_timeout_configured = False

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            command = provider._build_kimi_command()

        # No --config flag in command (breaks OAuth)
        assert "--config" not in command
        # Config file should be updated to 600000
        assert "tool_call_timeout_ms = 600000" in config_file.read_text()
        # Class-level flag should be set
        assert KimiCliProvider._mcp_timeout_configured is True

        # Cleanup should NOT restore timeout (shared config, concurrent instances)
        provider.cleanup()
        assert "tool_call_timeout_ms = 600000" in config_file.read_text()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_mcp_timeout_only_once(self, mock_load, tmp_path):
        """Test that config.toml is only modified once even with multiple instances."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "uv", "args": ["run", "cao-mcp-server"]}
        }
        mock_load.return_value = mock_profile

        fake_kimi_dir = tmp_path / ".kimi"
        fake_kimi_dir.mkdir()
        config_file = fake_kimi_dir / "config.toml"
        config_file.write_text("[mcp.client]\ntool_call_timeout_ms = 60000\n")

        KimiCliProvider._mcp_timeout_configured = False

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            p1 = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
            p1._build_kimi_command()

            # Manually reset config to 60000 to verify second call doesn't write
            config_file.write_text("[mcp.client]\ntool_call_timeout_ms = 60000\n")

            p2 = KimiCliProvider("term-2", "session-1", "window-2", agent_profile="dev")
            p2._build_kimi_command()

        # Second instance should NOT have modified config (flag was already set)
        assert "tool_call_timeout_ms = 60000" in config_file.read_text()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_no_timeout_without_mcp(self, mock_load, tmp_path):
        """Test that MCP tool timeout is NOT modified when no MCP servers are configured."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "You are helpful"
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        fake_kimi_dir = tmp_path / ".kimi"
        fake_kimi_dir.mkdir()
        config_file = fake_kimi_dir / "config.toml"
        config_file.write_text("[mcp.client]\ntool_call_timeout_ms = 60000\n")

        KimiCliProvider._mcp_timeout_configured = False

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            command = provider._build_kimi_command()

        # Config file should remain unchanged
        assert "tool_call_timeout_ms = 60000" in config_file.read_text()
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_mcp_timeout_config_missing(self, mock_load, tmp_path):
        """Test graceful handling when ~/.kimi/config.toml doesn't exist."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "uv", "args": ["run", "cao-mcp-server"]}
        }
        mock_load.return_value = mock_profile

        KimiCliProvider._mcp_timeout_configured = False

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            command = provider._build_kimi_command()

        # Should still produce a valid command
        assert "kimi --yolo" in command
        assert "--mcp-config" not in command
        assert "cao-mcp-server" in _read_kimi_mcp(provider)["mcpServers"]

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_mcp_timeout_already_high(self, mock_load, tmp_path):
        """Test that timeout is not downgraded if already >= 600000."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = {
            "cao-mcp-server": {"command": "uv", "args": ["run", "cao-mcp-server"]}
        }
        mock_load.return_value = mock_profile

        fake_kimi_dir = tmp_path / ".kimi"
        fake_kimi_dir.mkdir()
        config_file = fake_kimi_dir / "config.toml"
        config_file.write_text("[mcp.client]\ntool_call_timeout_ms = 900000\n")

        KimiCliProvider._mcp_timeout_configured = False

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")

        with patch("cli_agent_orchestrator.providers.kimi_cli.Path.home", return_value=tmp_path):
            provider._build_kimi_command()

        # Should NOT downgrade an already-high timeout
        assert "tool_call_timeout_ms = 900000" in config_file.read_text()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_profile_no_system_prompt(self, mock_load):
        """Test command with profile that has no system prompt (no agent file, but temp dir exists)."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "kimi --yolo" in command
        assert "--agent-file" not in command
        assert provider._temp_dir is not None
        provider.cleanup()

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_profile_empty_system_prompt(self, mock_load):
        """Test command with profile that has empty string system prompt."""
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        command = provider._build_kimi_command()

        assert "kimi --yolo" in command
        assert "--agent-file" not in command
        assert provider._temp_dir is not None
        provider.cleanup()


# =============================================================================
# Misc / lifecycle tests
# =============================================================================


class TestKimiCliProviderModelFlag:
    """Tests that profile.model is forwarded to Kimi CLI via --model."""

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_appends_model_when_set(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = "kimi-k2-turbo"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "sess", "win", "agent")
        command = provider._build_kimi_command()

        assert "--model kimi-k2-turbo" in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_command_omits_model_when_unset(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "sess", "win", "agent")
        command = provider._build_kimi_command()

        assert "--model" not in command

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_explicit_model_override_wins_over_profile_model(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.model = "kimi-k2-turbo"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load.return_value = mock_profile

        provider = KimiCliProvider("term-1", "sess", "win", "agent", model="fable-5")
        command = provider._build_kimi_command()

        assert "--model fable-5" in command
        assert "--model kimi-k2-turbo" not in command

    def test_explicit_model_override_applies_with_no_agent_profile(self):
        """Regression: PR #501 review -- model resolution used to live
        entirely inside `if self._agent_profile is not None:`, so an
        override passed with agent_profile=None (unreachable through
        handoff/assign today, but inconsistent with codex/hermes's own
        no-profile-still-applies shape) was silently dropped."""
        provider = KimiCliProvider("term-1", "sess", "win", None, model="fable-5")
        command = provider._build_kimi_command()

        assert "--model fable-5" in command
        provider.cleanup()


class TestKimiCliProviderMisc:
    """Tests for miscellaneous KimiCliProvider methods and lifecycle."""

    def test_exit_cli(self):
        """Test exit command returns /exit."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider.exit_cli() == "/exit"

    def test_cleanup(self):
        """Test cleanup resets initialized state and latching flag."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._initialized = True
        provider._has_received_input = True
        provider.cleanup()
        assert provider._initialized is False
        assert provider._has_received_input is False

    def test_cleanup_removes_temp_dir(self):
        """Test cleanup removes temporary directory and its contents."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._temp_dir = tempfile.mkdtemp(prefix="cao_kimi_test_")
        temp_path = provider._temp_dir  # Save path before cleanup resets it

        # Create a file in temp dir to verify it's removed
        test_file = os.path.join(temp_path, "test.txt")
        with open(test_file, "w") as f:
            f.write("test")

        provider.cleanup()
        assert provider._temp_dir is None
        assert not os.path.exists(temp_path)

    def test_cleanup_nonexistent_temp_dir(self):
        """Test cleanup handles already-removed temp directory gracefully."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        provider._temp_dir = "/tmp/cao_kimi_nonexistent_12345"
        provider.cleanup()
        assert provider._temp_dir is None

    def test_provider_inherits_base(self):
        """Test provider inherits from BaseProvider."""
        from cli_agent_orchestrator.providers.base import BaseProvider

        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert isinstance(provider, BaseProvider)

    def test_provider_default_state(self):
        """Test provider default initialization state."""
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider._initialized is False
        assert provider._agent_profile is None
        assert provider._temp_dir is None
        assert provider._has_received_input is False
        assert provider.terminal_id == "term-1"
        assert provider.session_name == "session-1"
        assert provider.window_name == "window-1"

    def test_provider_with_agent_profile(self):
        """Test provider stores agent profile."""
        provider = KimiCliProvider("term-1", "session-1", "window-1", agent_profile="dev")
        assert provider._agent_profile == "dev"


# =============================================================================
# Pattern tests
# =============================================================================


class TestKimiCliProviderPatterns:
    """Tests for Kimi CLI regex patterns — validates correctness of all patterns."""

    def test_idle_prompt_pattern_thinking(self):
        """Test idle prompt pattern matches thinking mode prompt (💫)."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@my-app💫")
        assert re.search(IDLE_PROMPT_PATTERN, "haofeif@cli-agent-orchestrator💫")

    def test_idle_prompt_pattern_bare_emoji(self):
        """Test idle prompt pattern matches bare emoji (Kimi v1.20.0+ format)."""
        assert re.search(IDLE_PROMPT_PATTERN, "💫")
        assert re.search(IDLE_PROMPT_PATTERN, "✨")

    def test_idle_prompt_pattern_no_thinking(self):
        """Test idle prompt pattern matches no-thinking mode prompt (✨)."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@my-app✨")
        assert re.search(IDLE_PROMPT_PATTERN, "haofeif@project✨")

    def test_idle_prompt_pattern_with_dots_in_hostname(self):
        """Test idle prompt pattern matches hostnames with dots."""
        assert re.search(IDLE_PROMPT_PATTERN, "user@host.domain.com💫")

    def test_idle_prompt_pattern_does_not_match_random_text(self):
        """Test idle prompt pattern doesn't match arbitrary text."""
        assert not re.search(IDLE_PROMPT_PATTERN, "Hello world")
        assert not re.search(IDLE_PROMPT_PATTERN, "some random text")
        # With EOL anchor (as used in get_status), emoji followed by text doesn't match
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        assert not re.search(idle_prompt_eol, "💫 alone")

    def test_welcome_banner_pattern(self):
        """Test welcome banner detection."""
        assert re.search(WELCOME_BANNER_PATTERN, "Welcome to Kimi Code CLI!")
        assert not re.search(WELCOME_BANNER_PATTERN, "Welcome to Claude Code")

    def test_user_input_box_patterns(self):
        """Test user input box boundary detection."""
        assert re.search(USER_INPUT_BOX_START_PATTERN, "╭──────────────╮")
        assert re.search(USER_INPUT_BOX_END_PATTERN, "╰──────────────╯")
        assert not re.search(USER_INPUT_BOX_START_PATTERN, "│ text │")

    def test_response_bullet_pattern(self):
        """Test response bullet detection."""
        assert re.search(RESPONSE_BULLET_PATTERN, "• Hello world!")
        assert re.search(RESPONSE_BULLET_PATTERN, "• Here is the code")
        assert not re.search(RESPONSE_BULLET_PATTERN, "Hello world")
        assert not re.search(RESPONSE_BULLET_PATTERN, "  • indented bullet")

    def test_thinking_bullet_raw_pattern(self):
        """Test thinking bullet detection in raw ANSI output."""
        # Gray-colored bullet (thinking mode)
        raw = "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThinking...\x1b[0m"
        assert re.search(THINKING_BULLET_RAW_PATTERN, raw)
        # Gray bullet with space before •
        raw_space = "\x1b[38;5;244m •\x1b[39m"
        assert re.search(THINKING_BULLET_RAW_PATTERN, raw_space)
        # Regular bullet (response mode) — should NOT match
        assert not re.search(THINKING_BULLET_RAW_PATTERN, "• Hello world")

    def test_error_pattern(self):
        """Test error pattern detection."""
        assert re.search(ERROR_PATTERN, "Error: connection failed", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ERROR: something went wrong", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "ConnectionError: timeout", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "APIError: rate limited", re.MULTILINE)
        assert re.search(ERROR_PATTERN, "Traceback (most recent call last):", re.MULTILINE)
        assert not re.search(ERROR_PATTERN, "No errors found", re.MULTILINE)

    def test_status_bar_pattern(self):
        """Test status bar detection."""
        assert re.search(STATUS_BAR_PATTERN, "23:14  yolo  agent (kimi-for-coding, thinking)")
        assert re.search(STATUS_BAR_PATTERN, "10:30  agent (kimi-for-coding)")
        assert not re.search(STATUS_BAR_PATTERN, "Hello world")

    def test_ansi_code_stripping(self):
        """Test ANSI code pattern strips all escape sequences."""
        raw = "\x1b[1muser@app💫\x1b[0m"
        clean = re.sub(ANSI_CODE_PATTERN, "", raw)
        assert clean == "user@app💫"

        raw2 = "\x1b[38;5;244m•\x1b[39m \x1b[3m\x1b[38;5;244mThinking\x1b[0m"
        clean2 = re.sub(ANSI_CODE_PATTERN, "", raw2)
        assert clean2 == "• Thinking"

    def test_idle_prompt_tail_lines(self):
        """Test tail lines constant is reasonable for Kimi's TUI layout."""
        assert IDLE_PROMPT_TAIL_LINES >= 40  # Must cover tall terminals (46+ rows)
        assert IDLE_PROMPT_TAIL_LINES <= 100  # Not unreasonably large


# ---------------------------------------------------------------------------
# Newest "Kimi Code" TUI status detection (regression for the redesigned CLI)
#
# These fixtures are REAL pipe-pane captures (last ~8KB rolling-buffer window)
# of the redesigned Kimi Code TUI, which has no ✨/💫 prompt — readiness is the
# "agent (<model> ●)" status bar / "context: N%" footer with an empty
# "── input ──" box, and a turn-in-flight is a braille spinner ("⠧ Thinking…
# Ns · N tokens"). The legacy emoji-prompt detector timed out at init on this
# build; get_status() must classify these raw buffers correctly.
# ---------------------------------------------------------------------------
_KIMI_FIXTURES = Path(__file__).parent / "fixtures"


class TestKimiCodeNewTuiStatus:
    """get_status() against real captured raw buffers of the new Kimi Code TUI."""

    def _provider(self):
        return KimiCliProvider("term-x", "session-x", "window-x", agent_profile="developer")

    def test_new_tui_idle_raw_capture(self):
        """A freshly-initialized terminal (no task yet) reads IDLE — this is the
        init readiness signal the legacy emoji detector missed."""
        buf = (_KIMI_FIXTURES / "kimi_code_tui_idle_raw.txt").read_text(encoding="utf-8")
        assert self._provider().get_status(buf) == TerminalStatus.IDLE

    def test_new_tui_processing_raw_capture(self):
        """A turn in flight (braille spinner is the freshest output) reads PROCESSING."""
        buf = (_KIMI_FIXTURES / "kimi_code_tui_processing_raw.txt").read_text(encoding="utf-8")
        assert self._provider().get_status(buf) == TerminalStatus.PROCESSING

    def test_new_tui_completed_raw_capture(self):
        """A finished turn (response present, spinner cleared, prompt visible)
        reads COMPLETED even though stale spinner frames linger in the buffer."""
        buf = (_KIMI_FIXTURES / "kimi_code_tui_completed_raw.txt").read_text(encoding="utf-8")
        assert self._provider().get_status(buf) == TerminalStatus.COMPLETED

    def test_new_tui_stale_spinner_does_not_block_completed(self):
        """Position-based detection: a stale braille frame earlier in the buffer
        must not be mistaken for a live turn once the prompt has redrawn."""
        buf = (_KIMI_FIXTURES / "kimi_code_tui_completed_raw.txt").read_text(encoding="utf-8")
        # Sanity: the completed fixture really does contain stale braille frames.
        assert any("⠀" <= ch <= "⣿" for ch in buf)
        assert self._provider().get_status(buf) != TerminalStatus.PROCESSING


class TestKimiCodeNewTuiExtraction:
    """Extraction for the newest "Kimi Code" TUI (✨ prompt, • bullets).

    Ground truth from a live Kimi Code session: user messages render as
    ✨-prefixed lines (no ╭─ input box), responses as • bullets, and the
    footer is a "── input ──" rule + status bar. Decorative ╰─ boxes from
    boot banners (Kimi welcome box, FastMCP server banner) appear ABOVE the
    conversation — anchoring on the last box-end used to slice the
    "response" out of the boot screen and run it to end-of-capture.
    """

    NEW_TUI_CAPTURE = (
        "╭──────────────────────────────╮\n"
        "│  ▐█▛█▛█▌  Welcome to Kimi Code CLI!  │\n"
        "│  Directory: /tmp/cao_kimi_x  │\n"
        "╰──────────────────────────────╯\n"
        "╭──────────────────────────────╮\n"
        "│  🖥  Server: cao-mcp-server, 3.4.2  │\n"
        "╰──────────────────────────────╯\n"
        "✨ What is 17*23? Reply with just the number and one short sentence.\n"
        "\n"
        "• The user is asking for a simple multiplication: 17 * 23.\n"
        "\n"
        "• 391. The product of 17 and 23 is 391.\n"
        "\n"
        "── input ─────────────────────────────────\n"
        "\n"
        "\n"
        "──────────────────────────────────────────\n"
        "yolo  agent (Kimi-k2.6 ●)  /tmp/cao_kimi_x  ctrl-o: editor\n"
        "context: 4.0% (10.4k/262.1k)\n"
    )
    KIMI_029_CAPTURE = (
        "╭──────────────────────────────╮\n"
        "│  Welcome to Kimi Code!       │\n"
        "╰──────────────────────────────╯\n"
        "\x1b[1m\x1b[38;2;255;203;107m✨ ^[[200~Reply exactly: KIMI_READY^[[201~\x1b[0m\n"
        "\x1b[38;2;136;136;136m● \x1b[3mThe user requests an exact reply.\x1b[0m\n"
        "\x1b[38;2;224;224;224m● \x1b[39mKIMI_READY\n"
        "╭──────────────────────────────╮\n"
        "│ >                            │\n"
        "╰──────────────────────────────╯\n"
        "yolo plan  K3 thinking: max  /tmp/cao_kimi\n"
        "context: 5% (43.1k/1M)\n"
    )

    def test_extracts_response_after_sparkle_prompt(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(self.NEW_TUI_CAPTURE)
        assert "391" in result

    def test_extraction_excludes_footer_chrome_and_boot_banners(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        result = provider.extract_last_message_from_script(self.NEW_TUI_CAPTURE)
        assert "input ─" not in result
        assert "context:" not in result
        assert "Welcome to Kimi Code CLI" not in result
        assert "cao-mcp-server, 3.4.2" not in result

    def test_extracts_kimi_029_circle_response_before_ready_input_box(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        assert provider.extract_last_message_from_script(self.KIMI_029_CAPTURE) == "KIMI_READY"

    def test_kimi_029_circle_response_marks_rendered_screen_completed(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        clean = re.sub(ANSI_CODE_PATTERN, "", self.KIMI_029_CAPTURE)
        assert provider.get_status_from_screen(clean.splitlines()) == TerminalStatus.COMPLETED


class TestKimiCodeDispatchGrace:
    """Newest-TUI: a fresh dispatch must read PROCESSING even when the ready
    chrome (status bar) is the freshest content in the buffer.

    Real failure (supervisor-assign e2e): the paste repaints the status bar
    before the turn's first spinner frame, so the spinner-vs-ready position
    compare read COMPLETED ~130ms after send_input(); the StatusMonitor
    ready-latch then pinned the false COMPLETED for the entire turn and the
    test extracted mid-flight output.
    """

    NEW_TUI_READY_CHROME = (
        "✨ Analyze the datasets and report back.\n"
        "\n"
        "• Dispatching analysts now.\n"
        "── input ─────────────────────────────────\n"
        "\n"
        "──────────────────────────────────────────\n"
        "yolo  agent (Kimi-k2.6 ●)  /tmp/cao_kimi_x  ctrl-o: editor\n"
        "context: 4.0% (10.4k/262.1k)\n"
    )

    def test_fresh_dispatch_reads_processing(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        provider.mark_input_received()
        assert provider.get_status(self.NEW_TUI_READY_CHROME) == TerminalStatus.PROCESSING

    def test_grace_expires_to_completed_when_pane_clear(self):
        import time as _time

        provider = KimiCliProvider("test123", "test-session", "window-0")
        provider.mark_input_received()
        provider._last_dispatch_time = _time.time() - 6.0
        with patch("cli_agent_orchestrator.providers.kimi_cli.get_backend") as mock_backend:
            # Rendered pane shows the same ready chrome — no live spinner.
            mock_backend.return_value.get_history.return_value = self.NEW_TUI_READY_CHROME
            assert provider.get_status(self.NEW_TUI_READY_CHROME) == TerminalStatus.COMPLETED

    def test_ready_stream_with_live_pane_spinner_is_processing(self):
        """A ready-looking chunk boundary mid-turn: the stream shows the ready
        chrome, but the RENDERED pane still shows a live spinner — must stay
        PROCESSING (this is the frame the StatusMonitor latch would otherwise
        pin as a false COMPLETED)."""
        import time as _time

        provider = KimiCliProvider("test123", "test-session", "window-0")
        provider.mark_input_received()
        provider._last_dispatch_time = _time.time() - 6.0
        pane = self.NEW_TUI_READY_CHROME.replace("── input ─", "⠹ Using handoff({...})\n── input ─")
        with patch("cli_agent_orchestrator.providers.kimi_cli.get_backend") as mock_backend:
            mock_backend.return_value.get_history.return_value = pane
            assert provider.get_status(self.NEW_TUI_READY_CHROME) == TerminalStatus.PROCESSING

    def test_pane_read_failure_falls_back_to_stream(self):
        import time as _time

        provider = KimiCliProvider("test123", "test-session", "window-0")
        provider.mark_input_received()
        provider._last_dispatch_time = _time.time() - 6.0
        with patch("cli_agent_orchestrator.providers.kimi_cli.get_backend") as mock_backend:
            mock_backend.return_value.get_history.side_effect = RuntimeError("pane gone")
            assert provider.get_status(self.NEW_TUI_READY_CHROME) == TerminalStatus.COMPLETED

    def test_no_dispatch_unaffected(self):
        provider = KimiCliProvider("test123", "test-session", "window-0")
        # bullet in buffer latches _has_received_input → COMPLETED, as before;
        # no dispatch yet → pane confirmation is skipped entirely.
        assert provider.get_status(self.NEW_TUI_READY_CHROME) == TerminalStatus.COMPLETED


class TestKimiScreenDetection:
    """Viewport detector (get_status_from_screen) — pyte-composited screens."""

    def _p(self):
        return KimiCliProvider("test123", "test-session", "window-0")

    def test_mcp_connecting_is_processing_not_idle(self):
        """Kimi draws its status bar BEFORE accepting input; during
        'connecting to mcp servers' a premature IDLE lets the inbox paste a
        message into the boot screen where it is absorbed (observed live)."""
        screen = [
            "⠧ MCP Servers: 0/1 connected, 0 tools",
            "⠦ cao-mcp-server (connecting)",
            "── input ──────────────",
            "yolo  agent (Kimi-k2.6 ●)  /tmp/x",
            "connecting to mcp servers...",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.PROCESSING

    def test_ready_status_bar_no_bullet_is_idle(self):
        screen = [
            "▐█▛█▛█▌  Welcome to Kimi Code CLI!",
            "── input ──────────────",
            "yolo  agent (Kimi-k2.6 ●)  /tmp/x",
            "context: 1.0% (2.6k/262.1k)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.IDLE

    def test_status_bar_with_bullet_is_completed(self):
        screen = [
            "✨ What is 17*23?",
            "• 391. The product of 17 and 23 is 391.",
            "── input ──────────────",
            "yolo  agent (Kimi-k2.6 ●)  /tmp/x",
            "context: 4.0% (10.4k/262.1k)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED

    def test_response_mentioning_connecting_is_not_boot_gated(self):
        """A COMPLETED turn whose "•" response bullet merely MENTIONS the boot
        chrome ("connecting to mcp servers" / "(connecting)") must not be
        re-stranded as PROCESSING. The boot gate scans non-bullet boot chrome
        only; a whole-screen scan would pin this terminal at PROCESSING and
        InboxService (delivers on IDLE/COMPLETED) would never deliver to it."""
        screen = [
            "✨ How does kimi boot?",
            "• It logs 'connecting to mcp servers' and shows cao-mcp-server (connecting) until ready.",
            "── input ──────────────",
            "yolo  agent (Kimi-k2.6 ●)  /tmp/x",
            "context: 4.0% (10.4k/262.1k)",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.COMPLETED

    def test_live_spinner_is_processing(self):
        screen = [
            "✨ Analyze the data",
            "• Working through it.",
            "⠹ Using handoff({...})",
            "── input ──────────────",
            "yolo  agent (Kimi-k2.6 ●)  /tmp/x",
        ]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.PROCESSING

    def test_torn_down_shell_is_unknown(self):
        screen = ["Bye!", "rkram@host:/tmp/x$"]
        assert self._p().get_status_from_screen(screen) == TerminalStatus.UNKNOWN


class TestKimiCliProfileRestoration:
    """Tests for Kimi profile restoration and working_directory resolution."""

    def test_init_accepts_and_stores_working_directory(self):
        provider = KimiCliProvider(
            "term-1", "session-1", "window-1", working_directory="/custom/repo"
        )
        assert provider._working_directory == "/custom/repo"

    def test_restore_input_preparation_stores_working_directory_if_unset(self):
        provider = KimiCliProvider("term-1", "session-1", "window-1")
        assert provider._working_directory is None
        provider.restore_input_preparation(None, working_directory="/new/repo")
        assert provider._working_directory == "/new/repo"

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_restore_input_preparation_calls_load_agent_profile_with_start(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.skills = []
        mock_profile.system_prompt = "Hello"
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="coder",
            working_directory="/my/repo",
        )
        provider.restore_input_preparation(False)

        mock_load.assert_called_once_with("coder", start=Path("/my/repo"))
        assert provider._first_message_prefix == "Hello"

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_restore_input_preparation_uses_provided_profile(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.skills = []
        mock_profile.system_prompt = "Provided system prompt"

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="coder",
            working_directory="/my/repo",
        )
        provider.restore_input_preparation(False, profile=mock_profile)

        mock_load.assert_not_called()
        assert provider._first_message_prefix == "Provided system prompt"

    @patch("cli_agent_orchestrator.utils.skills.build_skill_catalog")
    def test_compose_first_message_prefix_passes_start_to_build_skill_catalog(
        self, mock_build_skills
    ):
        mock_build_skills.return_value = "\n## Skills\n- skillA"
        mock_profile = MagicMock()
        mock_profile.skills = ["skillA"]
        mock_profile.system_prompt = "Base prompt"

        provider = KimiCliProvider(
            "term-1", "session-1", "window-1", working_directory="/my/repo"
        )
        prefix = provider._compose_first_message_prefix(mock_profile)

        mock_build_skills.assert_called_once_with(["skillA"], start=Path("/my/repo"))
        assert "skillA" in prefix

    @patch("cli_agent_orchestrator.providers.kimi_cli.load_agent_profile")
    def test_build_kimi_command_passes_start_to_load_agent_profile(self, mock_load):
        mock_profile = MagicMock()
        mock_profile.skills = []
        mock_profile.system_prompt = "Custom system prompt"
        mock_profile.model = None
        mock_profile.permissionMode = "plan"
        mock_load.return_value = mock_profile

        provider = KimiCliProvider(
            "term-1",
            "session-1",
            "window-1",
            agent_profile="reviewer",
            working_directory="/my/repo",
        )
        cmd = provider._build_kimi_command()

        mock_load.assert_called_once_with("reviewer", start=Path("/my/repo"))
        assert "--plan" in cmd

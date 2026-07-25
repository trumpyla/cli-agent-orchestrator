"""Tests for the session service."""

from unittest.mock import ANY, MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend, HerdrWorkspace
from cli_agent_orchestrator.services.session_service import (
    create_session,
    delete_session,
    delete_session_automatically,
    get_session,
    list_sessions,
)


class TestCreateSession:
    """Tests for create_session function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_resolves_provider_when_omitted(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is None, resolve_provider is called and its result forwarded."""
        mock_resolve.return_value = "claude_code"
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider=None, agent_profile="my_agent")

        mock_resolve.assert_called_once_with("my_agent", fallback_provider="kiro_cli")
        assert mock_create_terminal.call_args.kwargs["provider"] == "claude_code"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_uses_explicit_provider(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is explicitly passed, resolve_provider is NOT called."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent")

        mock_resolve.assert_not_called()
        assert mock_create_terminal.call_args.kwargs["provider"] == "kiro_cli"


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_success(self, mock_get_backend, mock_list_terminals):
        """Test getting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-test", "name": "Test Session"}
        ]
        mock_list_terminals.return_value = [{"id": "terminal1", "session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_normalizes_unprefixed_name(self, mock_get_backend, mock_list_terminals):
        """Public session lookup accepts an alias but resolves the canonical CAO name."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-review"}]
        mock_list_terminals.return_value = []

        result = get_session("review")

        assert result["session"]["id"] == "cao-review"
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-review")
        mock_list_terminals.assert_called_once_with("cao-review")

    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor.get_status")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_enriches_terminals_with_live_status(
        self, mock_get_backend, mock_list_terminals, mock_get_status
    ):
        """Each terminal should carry its live status (consumed by the web UI
        and the cao-ops-mcp get_session_info tool an external supervisor polls)."""
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-test"}]
        mock_list_terminals.return_value = [
            {"id": "term-a", "tmux_session": "cao-test"},
            {"id": "term-b", "tmux_session": "cao-test"},
        ]
        mock_get_status.side_effect = lambda tid: {
            "term-a": TerminalStatus.PROCESSING,
            "term-b": TerminalStatus.COMPLETED,
        }[tid]

        result = get_session("cao-test")

        assert result["terminals"][0]["status"] == "processing"
        assert result["terminals"][1]["status"] == "completed"

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_found(self, mock_get_backend):
        """Test getting non-existent session."""
        mock_get_backend.return_value.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_in_list(self, mock_get_backend):
        """Test getting session that exists but not in list."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_error(self, mock_get_backend):
        """Test getting session with error."""
        mock_get_backend.return_value.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    """Tests for delete_session function."""

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_success(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_delete_terminal,
    ):
        """Test deleting session successfully.

        delete_session delegates per-terminal teardown (FIFO reader, status
        buffer, provider, DB) to terminal_service.delete_terminal, then kills
        the backend session and returns the Dict result shape.
        """
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # Each terminal is torn down via the event-driven delete_terminal path.
        assert mock_delete_terminal.call_count == 2
        mock_delete_terminal.assert_any_call("terminal1", registry=ANY)
        mock_delete_terminal.assert_any_call("terminal2", registry=ANY)

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_normalizes_unprefixed_name(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Shutdown by the requested alias deletes the canonical backend session."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("review")

        assert result == {"deleted": ["cao-review"], "errors": []}
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-review")
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-review")
        mock_list_terminals.assert_called_once_with("cao-review")
        mock_delete_terminal.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_rejects_absent_session(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """A missing backend session with no persisted terminals is not a successful delete."""
        mock_get_backend.return_value.session_exists.return_value = False
        mock_list_terminals.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-missing' not found"):
            delete_session("missing")

        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_delete_terminal.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_when_backend_session_already_gone(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Backend session already gone — delete_session should not raise and not
        call kill_session, but still tear down each terminal via delete_terminal."""
        mock_get_backend.return_value.session_exists.return_value = False
        mock_list_terminals.return_value = [{"id": "terminal1"}]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_delete_terminal.assert_called_once_with("terminal1", registry=ANY)

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_no_terminals(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test deleting session with no terminals."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminal.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_error(self, mock_get_backend, mock_list_terminals):
        """Test deleting session with error."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_continues_when_terminal_cleanup_fails(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test that delete_session continues even when terminal teardown fails for some terminals."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
            {"id": "terminal3"},
        ]

        # First terminal teardown fails, others succeed
        mock_delete_terminal.side_effect = [
            Exception("Terminal teardown error for terminal1"),
            None,  # terminal2 succeeds
            None,  # terminal3 succeeds
        ]

        result = delete_session("cao-test")

        # Session should still be deleted despite per-terminal teardown failure
        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # All three terminal teardowns were attempted
        assert mock_delete_terminal.call_count == 3

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_cleans_up_each_terminal(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal
    ):
        """Test that delete_session tears down every terminal in the session via delete_terminal."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "term-aaa"},
            {"id": "term-bbb"},
            {"id": "term-ccc"},
            {"id": "term-ddd"},
        ]

        result = delete_session("cao-multi-terminal")

        assert result == {"deleted": ["cao-multi-terminal"], "errors": []}
        # Verify delete_terminal was called for each terminal with the correct ID
        assert mock_delete_terminal.call_count == 4
        mock_delete_terminal.assert_any_call("term-aaa", registry=ANY)
        mock_delete_terminal.assert_any_call("term-bbb", registry=ANY)
        mock_delete_terminal.assert_any_call("term-ccc", registry=ANY)
        mock_delete_terminal.assert_any_call("term-ddd", registry=ANY)


class TestAutomaticDeleteSession:
    """Stable-ID automatic teardown closes Herdr before persistence cleanup."""

    @staticmethod
    def _workspace(workspace_id: str = "ws-old") -> HerdrWorkspace:
        return HerdrWorkspace(
            workspace_id=workspace_id,
            label="cao-old",
            agent_status="done",
            pane_count=1,
            tab_count=1,
            active_tab_id=f"{workspace_id}:1",
        )

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.prepare_terminal_for_backend_close")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    def test_backend_identity_closes_before_terminal_and_environment_cleanup(
        self,
        mock_list,
        mock_delete,
        mock_prepare,
        mock_clear_env,
    ):
        backend = MagicMock(spec=HerdrBackend)
        backend.list_workspace_inventory.return_value = [self._workspace()]
        order: list[str] = []
        backend.close_workspace_by_id.side_effect = (
            lambda _workspace_id: order.append("backend") or True
        )
        mock_list.return_value = [{"id": "terminal-old"}]
        mock_prepare.side_effect = lambda *_args: order.append("prepare")
        mock_delete.side_effect = lambda *_args, **_kwargs: order.append("terminal")
        mock_clear_env.side_effect = lambda *_args: order.append("environment")

        result = delete_session_automatically(
            "cao-old",
            expected_backend_id="ws-old",
            backend=backend,
        )

        assert result == {"deleted": ["cao-old"], "errors": []}
        assert order == ["prepare", "backend", "terminal", "environment"]
        backend.close_workspace_by_id.assert_called_once_with("ws-old")
        mock_delete.assert_called_once_with(
            "terminal-old",
            registry=None,
            backend_already_closed=True,
            prepared=True,
        )

    @pytest.mark.parametrize(
        ("inventory", "close_result", "expected_error"),
        [
            pytest.param(
                [_workspace.__func__("ws-reused")],
                True,
                "backend identity changed",
                id="label-reuse",
            ),
            pytest.param(
                [_workspace.__func__()],
                False,
                "backend close failed",
                id="close-failure",
            ),
        ],
    )
    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.prepare_terminal_for_backend_close")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    def test_identity_or_close_failure_preserves_all_persistence(
        self,
        mock_list,
        mock_delete,
        mock_prepare,
        mock_clear_env,
        inventory,
        close_result,
        expected_error,
    ):
        backend = MagicMock(spec=HerdrBackend)
        backend.list_workspace_inventory.return_value = inventory
        backend.close_workspace_by_id.return_value = close_result
        mock_list.return_value = [{"id": "terminal-old"}]

        with pytest.raises(RuntimeError, match=expected_error):
            delete_session_automatically(
                "cao-old",
                expected_backend_id="ws-old",
                backend=backend,
            )

        mock_delete.assert_not_called()
        if expected_error == "backend identity changed":
            mock_prepare.assert_not_called()
        mock_clear_env.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.prepare_terminal_for_backend_close")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    def test_authoritatively_absent_backend_resumes_persistence_cleanup(
        self,
        mock_list,
        mock_delete,
        mock_prepare,
        mock_clear_env,
    ):
        backend = MagicMock(spec=HerdrBackend)
        backend.list_workspace_inventory.return_value = []
        mock_list.return_value = [{"id": "terminal-old"}]

        result = delete_session_automatically(
            "cao-old",
            expected_backend_id="ws-old",
            backend=backend,
        )

        assert result == {"deleted": ["cao-old"], "errors": []}
        backend.close_workspace_by_id.assert_not_called()
        mock_prepare.assert_not_called()
        mock_delete.assert_called_once_with(
            "terminal-old",
            registry=None,
            backend_already_closed=True,
            prepared=False,
        )
        mock_clear_env.assert_called_once_with("cao-old")

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.prepare_terminal_for_backend_close")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    def test_crash_boundary_resumes_without_second_backend_close(
        self,
        mock_list,
        mock_delete,
        mock_prepare,
        mock_clear_env,
    ):
        backend = MagicMock(spec=HerdrBackend)
        backend.list_workspace_inventory.side_effect = [[self._workspace()], []]
        backend.close_workspace_by_id.return_value = True
        mock_list.return_value = [{"id": "terminal-old"}]
        mock_delete.side_effect = [RuntimeError("simulated crash boundary"), True]

        with pytest.raises(RuntimeError, match="simulated crash boundary"):
            delete_session_automatically(
                "cao-old",
                expected_backend_id="ws-old",
                backend=backend,
            )

        result = delete_session_automatically(
            "cao-old",
            expected_backend_id="ws-old",
            backend=backend,
        )

        assert result == {"deleted": ["cao-old"], "errors": []}
        backend.close_workspace_by_id.assert_called_once_with("ws-old")
        assert mock_delete.call_count == 2
        mock_clear_env.assert_called_once_with("cao-old")

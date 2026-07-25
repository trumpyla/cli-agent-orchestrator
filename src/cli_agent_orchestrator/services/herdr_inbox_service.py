"""HerdrInboxService — socket event-based inbox delivery for herdr backend.

Replaces the pipe-pane + file watchdog approach with herdr's native socket API.
Subscribes to pane.agent_status_changed events and delivers pending inbox
messages when a pane transitions to idle or done.

Design:
- Maintains a pane_id → terminal_id map for managed panes
- Subscribes per-pane (wildcard support is unverified; see design.md)
- Reconnects with exponential backoff on socket disconnect
- Supplements with periodic pane read for kiro-cli (working >30s check)
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Callable, Dict, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

logger = logging.getLogger(__name__)

# Exponential backoff parameters
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_MAX = 30.0  # seconds
_BACKOFF_MULTIPLIER = 2.0

# Kiro supplement check: how long in "working" before we check pane read
_KIRO_WORKING_THRESHOLD = 30.0  # seconds


class HerdrTab(BaseModel):
    """Strict identity fields required from one Herdr tab-list element."""

    model_config = ConfigDict(extra="ignore")

    tab_id: str = Field(strict=True)
    workspace_id: str = Field(strict=True)
    label: str = Field(strict=True)

    @field_validator("tab_id", "workspace_id", "label")
    @classmethod
    def _require_non_empty_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("tab identity fields must not be empty")
        return value


class HerdrPane(BaseModel):
    """Strict stable fields required from one Herdr pane-list element."""

    model_config = ConfigDict(extra="ignore")

    pane_id: str = Field(strict=True)

    @field_validator("pane_id")
    @classmethod
    def _require_non_empty_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("pane identity must not be empty")
        return value


class ReconciliationTerminal(BaseModel):
    """Strict persisted identity used by startup reconciliation."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(strict=True)
    tmux_session: str = Field(strict=True)
    tmux_window: str = Field(strict=True)
    provider: str = Field(strict=True)

    @field_validator("id", "tmux_session", "tmux_window", "provider")
    @classmethod
    def _require_non_empty_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("persisted terminal identity fields must not be empty")
        return value


class StartupReconciliationSummary(BaseModel):
    """Content-free result suitable for operational logs and health evidence."""

    model_config = ConfigDict(frozen=True)

    inventory_valid: bool
    reason: str
    examined: int = Field(default=0, ge=0)
    peer_skipped: int = Field(default=0, ge=0)
    missing_workspace: int = Field(default=0, ge=0)
    missing_tab: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    delete_failed: int = Field(default=0, ge=0)


def _parse_tab_inventory(stdout: str) -> list[HerdrTab]:
    """Parse a complete Herdr tab-list envelope or fail closed."""
    try:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise ValueError
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ValueError
        raw_tabs = result.get("tabs")
        if not isinstance(raw_tabs, list):
            raise ValueError
        tabs = [HerdrTab.model_validate(item) for item in raw_tabs if isinstance(item, dict)]
        if len(tabs) != len(raw_tabs):
            raise ValueError
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise TerminalBackendError("tab_inventory_invalid") from exc

    identities: set[tuple[str, str]] = set()
    for tab in tabs:
        identity = (tab.workspace_id, tab.label)
        if identity in identities:
            raise TerminalBackendError("tab_inventory_invalid")
        identities.add(identity)
    return tabs


def _parse_pane_inventory(stdout: str) -> list[HerdrPane]:
    """Parse a complete Herdr pane-list envelope or fail closed."""
    try:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise ValueError
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise ValueError
        raw_panes = result.get("panes")
        if not isinstance(raw_panes, list):
            raise ValueError
        panes = [HerdrPane.model_validate(item) for item in raw_panes if isinstance(item, dict)]
        if len(panes) != len(raw_panes):
            raise ValueError
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise TerminalBackendError("pane_inventory_invalid") from exc

    pane_ids = [pane.pane_id for pane in panes]
    if len(set(pane_ids)) != len(pane_ids):
        raise TerminalBackendError("pane_inventory_invalid")
    return panes


def _log_startup_reconciliation(summary: StartupReconciliationSummary) -> None:
    """Log only stable reason codes and aggregate counts."""
    logger.info(
        "startup_reconciliation inventory_valid=%s reason=%s examined=%d "
        "peer_skipped=%d missing_workspace=%d missing_tab=%d deleted=%d delete_failed=%d",
        summary.inventory_valid,
        summary.reason,
        summary.examined,
        summary.peer_skipped,
        summary.missing_workspace,
        summary.missing_tab,
        summary.deleted,
        summary.delete_failed,
    )


def _invalid_reconciliation(reason: str) -> StartupReconciliationSummary:
    summary = StartupReconciliationSummary(inventory_valid=False, reason=reason)
    _log_startup_reconciliation(summary)
    return summary


def _run_startup_reconciliation(herdr_session: str) -> StartupReconciliationSummary:
    """Synchronously reconcile persisted terminals against strict Herdr inventory.

    The async service invokes this repository boundary in a worker thread. Both
    inventories are parsed completely before any destructive action, ensuring a
    partial or malformed Herdr response cannot delete local state.
    """
    from cli_agent_orchestrator.clients.database import (
        PEER_TMUX_SESSION,
        delete_terminal,
        list_all_terminals,
    )

    try:
        workspace_result = subprocess.run(
            ["herdr", "--session", herdr_session, "workspace", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _invalid_reconciliation("workspace_command_timeout")
    except OSError:
        return _invalid_reconciliation("workspace_command_failed")
    if workspace_result.returncode != 0:
        return _invalid_reconciliation("workspace_command_failed")

    try:
        workspaces = HerdrBackend._parse_workspace_inventory(workspace_result.stdout)
    except TerminalBackendError as exc:
        reason = str(exc)
        if reason not in {"workspace_inventory_invalid", "duplicate_workspace_label"}:
            reason = "workspace_inventory_invalid"
        return _invalid_reconciliation(reason)

    try:
        tab_result = subprocess.run(
            ["herdr", "--session", herdr_session, "tab", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _invalid_reconciliation("tab_command_timeout")
    except OSError:
        return _invalid_reconciliation("tab_command_failed")
    if tab_result.returncode != 0:
        return _invalid_reconciliation("tab_command_failed")

    try:
        tabs = _parse_tab_inventory(tab_result.stdout)
    except TerminalBackendError:
        return _invalid_reconciliation("tab_inventory_invalid")

    try:
        raw_terminals = list_all_terminals()
        terminals = [
            ReconciliationTerminal.model_validate(item)
            for item in raw_terminals
            if isinstance(item, dict)
        ]
        if len(terminals) != len(raw_terminals):
            raise ValueError
        terminal_ids = [terminal.id for terminal in terminals]
        if len(set(terminal_ids)) != len(terminal_ids):
            raise ValueError
    except (ValidationError, TypeError, ValueError):
        return _invalid_reconciliation("database_inventory_invalid")

    workspace_by_label = {workspace.label: workspace for workspace in workspaces}
    live_tabs_by_workspace: dict[str, set[str]] = {}
    for tab in tabs:
        live_tabs_by_workspace.setdefault(tab.workspace_id, set()).add(tab.label)

    examined = 0
    peer_skipped = 0
    missing_workspace = 0
    missing_tab = 0
    deleted = 0
    delete_failed = 0
    candidates: list[ReconciliationTerminal] = []

    for terminal in terminals:
        if terminal.provider == "peer" or terminal.tmux_session == PEER_TMUX_SESSION:
            peer_skipped += 1
            continue
        examined += 1
        workspace = workspace_by_label.get(terminal.tmux_session)
        if workspace is None:
            missing_workspace += 1
            candidates.append(terminal)
            continue
        live_labels = live_tabs_by_workspace.get(workspace.workspace_id, set())
        if terminal.tmux_window not in live_labels:
            missing_tab += 1
            candidates.append(terminal)

    for terminal in candidates:
        try:
            if delete_terminal(terminal.id):
                deleted += 1
        except Exception:
            delete_failed += 1

    summary = StartupReconciliationSummary(
        inventory_valid=True,
        reason="ok",
        examined=examined,
        peer_skipped=peer_skipped,
        missing_workspace=missing_workspace,
        missing_tab=missing_tab,
        deleted=deleted,
        delete_failed=delete_failed,
    )
    _log_startup_reconciliation(summary)
    return summary


class HerdrInboxService:
    """Event-driven inbox delivery service using herdr socket API.

    Subscribes to agent status events for managed panes and delivers
    pending messages when agents become idle/done.
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        delivery_callback: Optional[Callable[[str], None]] = None,
        herdr_session: str = "cao",
    ) -> None:
        """Initialize the inbox service.

        Args:
            socket_path: Path to herdr socket. None = auto-detect from env.
            delivery_callback: Function to call for message delivery.
                Signature: callback(terminal_id) → checks and delivers pending messages.
            herdr_session: Name of the herdr session to connect to. Used to
                derive the default socket path and prefix CLI calls.
        """
        self._herdr_session = herdr_session
        self._socket_path = socket_path or self._default_socket_path(herdr_session)
        self._delivery_callback = delivery_callback

        # Managed pane tracking
        self._pane_to_terminal: Dict[str, str] = {}  # pane_id → terminal_id
        self._terminal_to_pane: Dict[str, str] = {}  # terminal_id → pane_id

        # Kiro-specific tracking for supplement check
        self._kiro_terminals: Set[str] = set()  # terminal_ids using kiro-cli
        self._working_since: Dict[str, float] = {}  # terminal_id → timestamp

        # Workspace tracking for lifecycle events
        self._workspace_to_session: Dict[str, str] = {}  # workspace_id → session_name

        # Connection state
        self._connected = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._backoff = _BACKOFF_BASE
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @staticmethod
    def _default_socket_path(session_name: str = "cao") -> str:
        """Determine default herdr socket path for a named session.

        The default session (name ``"default"``) uses a flat path:
        ``~/.config/herdr/herdr.sock``.

        Named sessions use a sessions subdirectory:
        ``~/.config/herdr/sessions/<session_name>/herdr.sock``.

        Args:
            session_name: Herdr session name. Defaults to ``"cao"``.
        """
        import os
        from pathlib import Path

        # Check XDG_CONFIG_HOME first, fallback to ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        if session_name == "default":
            return f"{config_home}/herdr/herdr.sock"
        return f"{config_home}/herdr/sessions/{session_name}/herdr.sock"

    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        """Register a terminal for event-based inbox delivery.

        Args:
            terminal_id: CAO terminal identifier
            pane_id: Current herdr compact pane_id
            is_kiro: Whether this terminal runs kiro-cli (enables supplement check)
        """
        self._pane_to_terminal[pane_id] = terminal_id
        self._terminal_to_pane[terminal_id] = pane_id
        if is_kiro:
            self._kiro_terminals.add(terminal_id)

        logger.info(f"Registered terminal {terminal_id} (pane={pane_id}, kiro={is_kiro})")

        # Start streaming events for the new pane by forcing a reconnect.
        #
        # herdr (0.6.8) resets the entire connection when it receives a SECOND
        # events.subscribe on a connection that already has an active
        # subscription, and it exposes no incremental "add subscription" API.
        # So we cannot subscribe the new pane on the live connection — instead we
        # close the socket, and _socket_loop reconnects and rebuilds the single
        # combined subscription (all panes + lifecycle) in one call.
        #
        # register_terminal() may be called from a synchronous/non-event-loop
        # thread, so we schedule the reconnect onto the captured loop via
        # run_coroutine_threadsafe instead of create_task.
        if self._connected and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._force_reconnect(), self._loop)

    def unregister_terminal(self, terminal_id: str) -> None:
        """Remove a terminal from managed set.

        Args:
            terminal_id: Terminal to unregister
        """
        pane_id = self._terminal_to_pane.pop(terminal_id, None)
        if pane_id:
            self._pane_to_terminal.pop(pane_id, None)
        self._kiro_terminals.discard(terminal_id)
        self._working_since.pop(terminal_id, None)
        logger.info(f"Unregistered terminal {terminal_id}")

    async def start(self) -> None:
        """Start the event loop: wait for first terminal, then connect and listen."""
        self._loop = asyncio.get_running_loop()
        # Run DB cleanup before starting the socket loop so ghost records from
        # prior server runs are removed even when no terminals are registered yet.
        await self._startup_db_cleanup()
        kiro_task = asyncio.ensure_future(self._kiro_supplement_loop())
        try:
            await self._socket_loop()
        finally:
            kiro_task.cancel()

    async def _startup_db_cleanup(self) -> None:
        """Reconcile persisted state without blocking the owning event loop."""
        await asyncio.to_thread(_run_startup_reconciliation, self._herdr_session)

    async def _kiro_supplement_loop(self) -> None:
        """Periodically check kiro terminals stuck in working state."""
        while True:
            await asyncio.sleep(10.0)
            try:
                await self.check_kiro_supplements()
            except Exception:
                logger.debug("Kiro supplement check error", exc_info=True)

    async def _socket_loop(self) -> None:
        """Connect to herdr socket and listen for events with reconnect.

        Defers connection until at least one terminal is registered. This avoids
        the disconnect/reconnect churn caused by herdr closing idle connections
        that have no active subscriptions.
        """
        while True:
            # Wait until there is at least one pane to subscribe to
            while not self._pane_to_terminal:
                await asyncio.sleep(0.5)

            try:
                await self._connect()
                self._connected = True

                # Reconcile map against live herdr state before subscribing
                await self._reconcile()

                # Subscribe to everything in ONE events.subscribe call: every
                # managed pane's agent-status plus the lifecycle events. herdr
                # resets the connection on a second events.subscribe, so this
                # must be a single combined call.
                await self._subscribe_all_events()

                self._backoff = _BACKOFF_BASE  # Reset backoff after successful setup

                # Listen for events
                await self._event_loop()

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                logger.warning(f"Herdr socket disconnected: {e}")
                self._connected = False

                # Exponential backoff
                logger.info(f"Reconnecting in {self._backoff}s...")
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)

    async def _reconcile(self) -> None:
        """Reconcile _pane_to_terminal map against live herdr state.

        Prunes stale pane entries, deletes orphaned DB terminal records,
        and kills workspaces with zero live terminals.
        """
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            get_terminal_metadata,
        )

        # Inventory subprocesses are blocking and must never own the event loop.
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["herdr", "--session", self._herdr_session, "pane", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("reconcile_failed reason=pane_command_failed")
            return
        if result.returncode != 0:
            logger.warning("reconcile_failed reason=pane_command_failed")
            return

        try:
            live_pane_ids = {pane.pane_id for pane in _parse_pane_inventory(result.stdout)}
        except TerminalBackendError:
            logger.warning("reconcile_failed reason=pane_inventory_invalid")
            return

        # Build workspace_id -> session_name mapping
        try:
            ws_result = await asyncio.to_thread(
                subprocess.run,
                ["herdr", "--session", self._herdr_session, "workspace", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("reconcile_failed reason=workspace_command_failed")
            return
        if ws_result.returncode != 0:
            logger.warning("reconcile_failed reason=workspace_command_failed")
            return
        try:
            workspaces = HerdrBackend._parse_workspace_inventory(ws_result.stdout)
        except TerminalBackendError:
            logger.warning("reconcile_failed reason=workspace_inventory_invalid")
            return
        self._workspace_to_session = {
            workspace.workspace_id: workspace.label for workspace in workspaces
        }

        # Reuse the same strict persisted-state reconciliation on every reconnect.
        await asyncio.to_thread(_run_startup_reconciliation, self._herdr_session)

        # Find stale panes: stored pane_id no longer in herdr's live pane list.
        #
        # A stale pane_id does NOT mean the terminal is dead. herdr renumbers
        # compact pane_ids when a sibling tab in the workspace closes, so a
        # still-running terminal's stored pane_id can fall out of the live list
        # while its tab is very much alive. Identity must come from the durable
        # tab label (tmux_window), never the ephemeral pane_id.
        stale_pane_ids = set(self._pane_to_terminal.keys()) - live_pane_ids
        if not stale_pane_ids:
            logger.debug("Reconcile: all panes live, nothing to prune")
            return

        # Live workspace labels, used to gate workspace teardown below: never
        # kill a workspace whose label is still present in herdr.
        live_workspace_labels = set(self._workspace_to_session.values())

        # Sessions that genuinely lost a terminal (deleted, not re-mapped).
        affected_sessions: Set[str] = set()
        remapped = 0
        deleted = 0

        for pane_id in stale_pane_ids:
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                self._pane_to_terminal.pop(pane_id, None)
                continue

            # Session/window identity before any mutation.
            meta = get_terminal_metadata(terminal_id)
            term_session: Optional[str] = meta["tmux_session"] if meta else None
            term_window: Optional[str] = meta["tmux_window"] if meta else None

            # Re-map renumbered-but-live panes instead of deleting. A live tab
            # label means the pane_id was renumbered, not closed: re-resolve the
            # current pane_id and update both maps. Only when re-resolution fails
            # do we fall through to the delete path.
            if term_window and self._label_still_live(term_window):
                try:
                    # Invalidate pane cache so get_pane_id does a fresh label-based
                    # lookup instead of returning the stale pane_id we just proved
                    # is no longer live. See PR #309 review comment.
                    backend = get_backend()
                    if hasattr(backend, "_pane_cache"):
                        backend._pane_cache.pop(terminal_id, None)
                    new_pane_id = backend.get_pane_id(terminal_id, term_session or "", term_window)
                except Exception as e:
                    logger.warning(
                        "Reconcile: tab %s live but pane re-resolve failed for %s (%s); "
                        "deleting",
                        term_window,
                        terminal_id,
                        e,
                    )
                else:
                    self._pane_to_terminal.pop(pane_id, None)
                    self._pane_to_terminal[new_pane_id] = terminal_id
                    self._terminal_to_pane[terminal_id] = new_pane_id
                    logger.info(
                        "Reconcile: re-mapped %s %s -> %s (pane renumbered, tab still live)",
                        terminal_id,
                        pane_id,
                        new_pane_id,
                    )
                    remapped += 1
                    continue

            # Tab label genuinely gone (or re-resolve failed): prune maps and
            # delete the orphaned DB record.
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            try:
                delete_terminal(terminal_id)
                deleted += 1
            except Exception as e:
                logger.warning(f"Reconcile: failed to delete terminal {terminal_id}: {e}")

            if term_session:
                affected_sessions.add(term_session)

        # Kill a workspace only when its label is gone from herdr AND no managed
        # terminal remains for the session. A live label means the workspace is
        # alive and its panes were merely renumbered — killing it would tear down
        # working agents.
        if affected_sessions:
            remaining_by_session: Dict[str, int] = {s: 0 for s in affected_sessions}
            for tid in self._terminal_to_pane:
                meta = get_terminal_metadata(tid)
                if meta and meta["tmux_session"] in remaining_by_session:
                    remaining_by_session[meta["tmux_session"]] += 1

            for session_name, remaining in remaining_by_session.items():
                if remaining == 0 and session_name not in live_workspace_labels:
                    try:
                        get_backend().kill_session(session_name)
                        logger.info(f"Reconcile: killed empty workspace {session_name}")
                    except Exception as e:
                        logger.warning(f"Reconcile: failed to kill workspace {session_name}: {e}")

        logger.info(
            "Reconcile: %d stale pane(s) — %d re-mapped, %d deleted",
            len(stale_pane_ids),
            remapped,
            deleted,
        )

    async def _connect(self) -> None:
        """Connect to the herdr socket."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        logger.info(f"Connected to herdr socket: {self._socket_path}")

    async def _subscribe_all_events(self) -> None:
        """Subscribe to all events in a SINGLE events.subscribe call.

        herdr (0.6.8) resets the entire connection when it receives a second
        events.subscribe on a connection that already has an active
        subscription. So every subscription this service needs — one
        pane.agent_status_changed per managed pane (pane_id is required; herdr
        rejects the wildcard form with invalid_request) plus the pane.closed and
        workspace.closed lifecycle events — must be sent together in one call.

        The pane_id → terminal_id mapping in _pane_to_terminal is already current:
        a socket disconnect does not change pane_ids (only a herdr server restart
        compacts them), and _reconcile() has already pruned stale panes before
        this runs.
        """
        subscriptions: list = [
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id in self._pane_to_terminal
        ]
        subscriptions.append({"type": "pane.closed"})
        subscriptions.append({"type": "workspace.closed"})

        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            f"Subscribed to {len(self._pane_to_terminal)} pane(s) + lifecycle events "
            f"in one events.subscribe call"
        )

    async def _force_reconnect(self) -> None:
        """Close the socket so _socket_loop reconnects and rebuilds the subscription.

        This is how a newly registered pane starts streaming events: herdr has no
        incremental subscribe, and a second events.subscribe on the live
        connection would reset it. Closing the writer makes the blocked
        readline() in _event_loop return EOF, which raises ConnectionError and
        drives _socket_loop through a fresh connect + combined re-subscribe.
        """
        writer = self._writer
        if writer is None:
            return
        try:
            writer.close()
        except Exception as e:
            logger.debug(f"Force reconnect: writer close raised (ignored): {e}")

    async def _event_loop(self) -> None:
        """Listen for events and dispatch delivery."""
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("Socket closed")

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            # herdr identifies the event in the "event" key. Lifecycle events use
            # underscore names (pane_closed / workspace_closed); the agent-status
            # event uses the dotted name (pane.agent_status_changed). Normalize the
            # name so routing does not depend on the separator herdr happens to use.
            # (Older code read "type" and matched dotted lifecycle names, which never
            # matched herdr's real wire format — lifecycle cleanup silently never ran.)
            raw_event = event.get("event", "") or event.get("type", "")
            event_name = raw_event.replace("_", ".")

            # Handle lifecycle events
            if event_name in ("pane.closed", "workspace.closed"):
                self._handle_lifecycle_event(event_name, event.get("data", {}))
                continue

            data = event.get("data", {})
            pane_id = data.get("pane_id", "")
            status = data.get("agent_status", "")

            # Only process events for managed panes
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                continue

            if status in ("idle", "done"):
                # Clear working timestamp
                self._working_since.pop(terminal_id, None)
                # Trigger delivery
                self._deliver(terminal_id)

            elif status == "working":
                # Track working start for kiro supplement check
                if terminal_id in self._kiro_terminals:
                    if terminal_id not in self._working_since:
                        self._working_since[terminal_id] = time.time()

    def _label_still_live(self, window_name: str) -> bool:
        """Return True if a tab with this label is still live in herdr.

        Used to disambiguate herdr's reused compact pane_ids on replayed
        pane_closed events. The tab label is unique per incarnation, so a live
        label means the close event refers to an older incarnation and is stale.

        Fails toward False (not live) when herdr can't be queried, so the caller
        proceeds with cleanup rather than leaving a possibly-closed terminal.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "tab", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_label_still_live: herdr tab list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
            tab_data = json.loads(result.stdout)
            tabs = tab_data.get("result", {}).get("tabs", [])
            live_labels = {tab.get("label", "") for tab in tabs}
            return window_name in live_labels
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_label_still_live: could not query herdr (%s)", e)
            return False

    def _resolve_session_from_herdr(self, workspace_id: str) -> Optional[str]:
        """Resolve a workspace_id to its session name from live herdr state.

        Used by workspace.closed handling when the in-memory _workspace_to_session
        map (populated only by _reconcile) does not contain the closed
        workspace_id. Queries herdr workspace list, refreshes the whole map from
        the result, and returns the label for workspace_id if found.

        Returns None when herdr cannot be queried or the workspace_id is not in
        the live list, so the caller can treat the event as unresolvable and take
        no destructive action.
        """
        try:
            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "workspace", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "_resolve_session_from_herdr: herdr workspace list failed (rc=%s): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            ws_data = json.loads(result.stdout)
            workspaces = ws_data.get("result", {}).get("workspaces", [])
            self._workspace_to_session = {ws["workspace_id"]: ws["label"] for ws in workspaces}
            return self._workspace_to_session.get(workspace_id)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning("_resolve_session_from_herdr: could not query herdr (%s)", e)
            return None

    def _handle_lifecycle_event(self, event_type: str, data: dict) -> None:
        """Handle pane.closed and workspace.closed events."""
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.clients.database import (
            delete_terminal,
            delete_terminals_by_session,
            get_terminal_metadata,
        )

        if event_type == "pane.closed":
            pane_id = data.get("pane_id", "")
            terminal_id = self._pane_to_terminal.get(pane_id)
            if not terminal_id:
                return

            # Get session before cleanup
            meta = get_terminal_metadata(terminal_id)
            session_name = meta["tmux_session"] if meta else None

            # Guard against herdr's compact pane_id reuse + event replay.
            #
            # herdr (0.6.8) reuses compact pane_ids when a tab is killed and a
            # new tab takes the same index, AND replays the ENTIRE pane_closed
            # history on every fresh events.subscribe (which register_terminal
            # triggers via _force_reconnect). So a replayed close for an OLD
            # incarnation of this pane_id arrives mapped to the terminal that now
            # occupies the reused index — deleting a live terminal.
            #
            # The tab label (tmux_window) is unique per incarnation, so confirm
            # the label is genuinely gone from herdr before deleting. If the
            # label is still live, this close is stale (replayed) — ignore it.
            # If herdr can't be queried, fall toward delete: never leave a
            # terminal we think is open when it may actually be closed.
            window_name = meta["tmux_window"] if meta else None
            if window_name and self._label_still_live(window_name):
                logger.info(
                    "pane.closed: ignoring stale close for %s (pane=%s) — "
                    "label %s still live in herdr (compact pane_id reused)",
                    terminal_id,
                    pane_id,
                    window_name,
                )
                return

            # Remove from maps
            self._pane_to_terminal.pop(pane_id, None)
            self._terminal_to_pane.pop(terminal_id, None)
            self._kiro_terminals.discard(terminal_id)
            self._working_since.pop(terminal_id, None)

            # Delete DB record
            try:
                delete_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"pane.closed: failed to delete terminal {terminal_id}: {e}")

            logger.info(f"pane.closed: cleaned up terminal {terminal_id} (pane={pane_id})")

            # If session has no more terminals in our map, kill workspace
            remaining_in_session = [
                t
                for t in self._pane_to_terminal.values()
                if (m := get_terminal_metadata(t)) and m.get("tmux_session") == session_name
            ]
            if session_name and not remaining_in_session:
                try:
                    get_backend().kill_session(session_name)
                    logger.info(f"pane.closed: killed empty workspace {session_name}")
                except Exception as e:
                    logger.warning(f"pane.closed: failed to kill workspace {session_name}: {e}")

        elif event_type == "workspace.closed":
            workspace_id = data.get("workspace_id", "")
            session_name = self._workspace_to_session.get(workspace_id)
            if not session_name:
                # The in-memory map is populated only by _reconcile(); a workspace
                # that closed before any reconcile cached it would otherwise be a
                # silent no-op, leaking the session's terminals as orphan rows.
                # Resolve the session identity from live herdr state instead of
                # trusting the map. Only treat the event as unresolvable after the
                # live query also fails to identify a session.
                session_name = self._resolve_session_from_herdr(workspace_id)
                if not session_name:
                    return

            # Delete all DB terminals for this session
            try:
                delete_terminals_by_session(session_name)
            except Exception as e:
                logger.warning(
                    f"workspace.closed: failed to delete terminals for {session_name}: {e}"
                )

            # Prune maps for terminals belonging to this session. Match on each
            # terminal's DB session rather than a pane_id/workspace_id string
            # prefix: herdr renumbers compact pane_ids and does not guarantee
            # they begin with the workspace_id, so a prefix test is unreliable.
            # This mirrors the session match used in the pane.closed handler.
            to_remove = [
                (pid, tid)
                for pid, tid in self._pane_to_terminal.items()
                if (m := get_terminal_metadata(tid)) and m.get("tmux_session") == session_name
            ]
            for pid, tid in to_remove:
                self._pane_to_terminal.pop(pid, None)
                self._terminal_to_pane.pop(tid, None)
                self._kiro_terminals.discard(tid)
                self._working_since.pop(tid, None)

            self._workspace_to_session.pop(workspace_id, None)
            logger.info(
                f"workspace.closed: cleaned up session {session_name} ({len(to_remove)} terminals)"
            )

    # TODO: _deliver() calls callback synchronously — if callback is async,
    # this will need a threadsafe bridge (out of scope for this change).
    def _deliver(self, terminal_id: str) -> None:
        """Check and deliver pending messages for a terminal."""
        if self._delivery_callback:
            try:
                self._delivery_callback(terminal_id)
            except Exception as e:
                logger.error(f"Delivery failed for terminal {terminal_id}: {e}")

    async def check_kiro_supplements(self) -> None:
        """Periodic check for kiro-cli terminals stuck in 'working' state.

        For terminals in 'working' for >30s, read pane content and check
        for permission prompt patterns.
        """
        import subprocess

        now = time.time()
        for terminal_id in list(self._working_since.keys()):
            if terminal_id not in self._kiro_terminals:
                continue

            working_duration = now - self._working_since[terminal_id]
            if working_duration < _KIRO_WORKING_THRESHOLD:
                continue

            # Read pane and check for permission prompt
            pane_id = self._terminal_to_pane.get(terminal_id)
            if not pane_id:
                continue

            result = subprocess.run(
                ["herdr", "--session", self._herdr_session, "pane", "read", pane_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue

            # Check for kiro permission prompt pattern
            # (WAITING_USER_ANSWER indicator)
            from cli_agent_orchestrator.providers.kiro_cli import TUI_PERMISSION_PATTERN

            if re.search(TUI_PERMISSION_PATTERN, result.stdout):
                logger.info(
                    f"Kiro permission prompt detected for {terminal_id} "
                    f"(working for {working_duration:.0f}s)"
                )
                self._deliver(terminal_id)
                # Reset the timer so we don't spam
                self._working_since[terminal_id] = now

    async def _send(self, message: dict) -> None:
        """Send a JSON message to the herdr socket."""
        assert self._writer is not None
        data = json.dumps(message).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()

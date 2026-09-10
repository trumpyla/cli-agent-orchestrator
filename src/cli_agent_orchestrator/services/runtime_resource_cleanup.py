"""Conservative stable-identity cleanup for completed Herdr workspaces."""

from __future__ import annotations

import fnmatch
import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend, HerdrWorkspace
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.services.config_service import CleanupConfig

logger = logging.getLogger(__name__)


class CleanupTerminal(BaseModel):
    """Strict persisted fields used for irreversible cleanup selection."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(strict=True)
    tmux_session: str = Field(strict=True)
    tmux_window: str = Field(strict=True)
    provider: str = Field(strict=True)
    last_active: datetime | None = Field(default=None, strict=True)

    @field_validator("id", "tmux_session", "tmux_window", "provider")
    @classmethod
    def _require_non_empty_identity(cls, value: str) -> str:
        if not value:
            raise ValueError("cleanup terminal identity fields must not be empty")
        return value


class CleanupCandidate(BaseModel):
    """Immutable workspace identity captured by the pure selection phase."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(strict=True)
    workspace_id: str = Field(strict=True)
    newest_activity: datetime = Field(strict=True)
    terminal_ids: list[str] | None = None
    excluded_terminal_ids: frozenset[str] = frozenset()


class CleanupSweepSummary(BaseModel):
    """Content-free bounded result emitted for each synchronous sweep."""

    model_config = ConfigDict(frozen=True)

    selected: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    reasons: dict[str, int] = Field(default_factory=dict)


class RuntimeResourceCleanup:
    """Select, revalidate, and tear down completed Herdr workspaces.

    Every external boundary is injected so the selector remains deterministic
    and the lifespan daemon can execute this whole synchronous unit off-loop.
    """

    def __init__(
        self,
        *,
        backend: object,
        config_reader: Callable[[], CleanupConfig],
        terminal_reader: Callable[[str], list[dict[str, object]]],
        pending_checker: Callable[[str], bool],
        teardown: Callable[[str, str], None],
        clock: Callable[[], datetime],
        debt_reader: Callable[[], list[dict[str, object]]] = lambda: [],
    ) -> None:
        self._backend = backend
        self._config_reader = config_reader
        self._terminal_reader = terminal_reader
        self._pending_checker = pending_checker
        self._teardown = teardown
        self._clock = clock
        self._debt_reader = debt_reader
        # Scheduling fairness is local to this engine; durable teardown debt
        # remains in SQLite and is rediscovered after a process restart.
        self._selection_cursor: tuple[datetime, str, str] | None = None

    @staticmethod
    def _parse_terminals(raw_rows: list[dict[str, object]]) -> list[CleanupTerminal]:
        terminals = [CleanupTerminal.model_validate(row) for row in raw_rows]
        terminal_ids = [terminal.id for terminal in terminals]
        if len(set(terminal_ids)) != len(terminal_ids):
            raise ValueError("duplicate terminal identity")
        return terminals

    def _has_pending(self, terminals: list[CleanupTerminal]) -> bool:
        return any(self._pending_checker(terminal.id) for terminal in terminals)

    def _select(
        self,
        workspaces: list[HerdrWorkspace],
        config: CleanupConfig,
        now: datetime,
        reasons: Counter[str],
        excluded_ids: dict[str, frozenset[str]] | None = None,
    ) -> list[CleanupCandidate]:
        if now.tzinfo is not None:
            reasons["clock_invalid"] += 1
            return []

        labels = [workspace.label for workspace in workspaces]
        if len(set(labels)) != len(labels):
            reasons["ambiguous_identity"] += 1
            return []

        candidates: list[CleanupCandidate] = []
        cutoff = now - timedelta(seconds=config.completed_session_grace_s)
        for workspace in workspaces:
            if workspace.agent_status != "done":
                reasons["status_not_done"] += 1
                continue
            if workspace.label == "__peers__" or not workspace.label.startswith(SESSION_PREFIX):
                reasons["outside_cao"] += 1
                continue
            if any(
                fnmatch.fnmatchcase(workspace.label, pattern)
                for pattern in config.preserve_patterns
            ):
                reasons["preserved"] += 1
                continue

            excluded = (excluded_ids or {}).get(workspace.label, frozenset())
            raw_rows = [
                row
                for row in self._terminal_reader(workspace.label)
                if not isinstance(row, dict) or row.get("id") not in excluded
            ]
            if not raw_rows:
                reasons["no_terminals"] += 1
                continue
            try:
                terminals = self._parse_terminals(raw_rows)
            except (ValidationError, TypeError, ValueError):
                reasons["terminal_inventory_invalid"] += 1
                continue
            if any(terminal.provider == "peer" for terminal in terminals):
                reasons["peer_terminal"] += 1
                continue
            if self._has_pending(terminals):
                reasons["pending_inbox"] += 1
                continue

            timestamps = [terminal.last_active for terminal in terminals]
            if any(timestamp is None for timestamp in timestamps):
                reasons["timestamp_invalid"] += 1
                continue
            concrete_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
            if any(timestamp.tzinfo is not None for timestamp in concrete_timestamps):
                reasons["timestamp_aware"] += 1
                continue
            newest = max(concrete_timestamps)
            if newest > now:
                reasons["timestamp_future"] += 1
                continue
            if newest >= cutoff:
                reasons["within_grace"] += 1
                continue
            candidates.append(
                CleanupCandidate(
                    label=workspace.label,
                    workspace_id=workspace.workspace_id,
                    newest_activity=newest,
                    excluded_terminal_ids=excluded,
                )
            )

        candidates.sort(key=lambda candidate: (candidate.newest_activity, candidate.label))
        return candidates

    @staticmethod
    def _candidate_order(candidate: CleanupCandidate) -> tuple[datetime, str, str]:
        return candidate.newest_activity, candidate.label, candidate.workspace_id

    def _revalidate(
        self,
        candidate: CleanupCandidate,
        reasons: Counter[str],
    ) -> bool:
        if not isinstance(self._backend, HerdrBackend):
            reasons["unsupported_backend"] += 1
            return False
        try:
            inventory = self._backend.list_workspace_inventory()
        except Exception:
            reasons["revalidation_failed"] += 1
            return False

        matches = [workspace for workspace in inventory if workspace.label == candidate.label]
        if any(
            workspace.workspace_id == candidate.workspace_id and workspace.label != candidate.label
            for workspace in inventory
        ):
            reasons["identity_changed"] += 1
            return False
        if matches:
            if len(matches) != 1 or matches[0].workspace_id != candidate.workspace_id:
                reasons["identity_changed"] += 1
                return False
            if matches[0].agent_status != "done":
                reasons["state_changed"] += 1
                return False

        raw_rows = [
            row
            for row in self._terminal_reader(candidate.label)
            if not isinstance(row, dict) or row.get("id") not in candidate.excluded_terminal_ids
        ]
        if not raw_rows:
            if candidate.terminal_ids is not None:
                return True
            reasons["terminal_state_changed"] += 1
            return False
        try:
            terminals = self._parse_terminals(raw_rows)
        except (ValidationError, TypeError, ValueError):
            reasons["terminal_state_changed"] += 1
            return False
        if any(terminal.provider == "peer" for terminal in terminals):
            reasons["terminal_state_changed"] += 1
            return False
        if candidate.terminal_ids is not None and not {t.id for t in terminals}.issubset(
            candidate.terminal_ids
        ):
            reasons["identity_changed"] += 1
            return False
        if self._has_pending(terminals):
            reasons["pending_inbox_changed"] += 1
            return False
        if not matches:
            reasons["workspace_absent"] += 1
        return True

    def sweep(self) -> CleanupSweepSummary:
        """Run one bounded sweep and isolate each teardown failure."""
        reasons: Counter[str] = Counter()
        try:
            config = self._config_reader()
        except Exception:
            summary = CleanupSweepSummary(reasons={"config_invalid": 1})
            self._log(summary)
            return summary
        if not config.completed_sessions_enabled:
            summary = CleanupSweepSummary(reasons={"disabled": 1})
            self._log(summary)
            return summary
        if not isinstance(self._backend, HerdrBackend):
            summary = CleanupSweepSummary(reasons={"unsupported_backend": 1})
            self._log(summary)
            return summary

        try:
            inventory = self._backend.list_workspace_inventory()
            now = self._clock()
        except Exception:
            summary = CleanupSweepSummary(reasons={"inventory_failed": 1})
            self._log(summary)
            return summary

        try:
            raw_debts = self._debt_reader()
            debts = []
            blocked_labels = set()
            for row in raw_debts:
                try:
                    if row.get("invalid"):
                        raise ValueError("invalid persisted debt")
                    debts.append(CleanupCandidate.model_validate(row))
                except (ValidationError, TypeError, ValueError):
                    label = row.get("label")
                    if not isinstance(label, str) or not label:
                        raise ValueError("cannot isolate invalid cleanup debt")
                    blocked_labels.add(label)
                    reasons["debt_inventory_invalid"] += 1
        except Exception:
            summary = CleanupSweepSummary(reasons={"debt_inventory_failed": 1})
            self._log(summary)
            return summary
        debts = [
            candidate
            for candidate in debts
            if candidate.label not in blocked_labels
            and candidate.label.startswith(SESSION_PREFIX)
            and not any(fnmatch.fnmatchcase(candidate.label, p) for p in config.preserve_patterns)
        ]
        debt_identities = {(candidate.label, candidate.workspace_id) for candidate in debts}
        excluded_ids = {
            candidate.label: frozenset(
                terminal_id
                for debt in debts
                if debt.label == candidate.label
                for terminal_id in (debt.terminal_ids or [])
            )
            for candidate in debts
        }
        debts = [
            candidate.model_copy(
                update={
                    "excluded_terminal_ids": frozenset(
                        terminal_id
                        for debt in debts
                        if debt.label == candidate.label
                        and debt.workspace_id != candidate.workspace_id
                        for terminal_id in (debt.terminal_ids or [])
                    )
                }
            )
            for candidate in debts
        ]
        fresh = self._select(
            [
                workspace
                for workspace in inventory
                if workspace.label not in blocked_labels
                and (workspace.label, workspace.workspace_id) not in debt_identities
            ],
            config,
            now,
            reasons,
            excluded_ids,
        )
        candidates = sorted(debts + fresh, key=self._candidate_order)
        if self._selection_cursor is not None:
            # Continue after the previous selected identity, even if its row was
            # removed. Wrap only after reaching the end of the current inventory.
            pivot = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if self._candidate_order(candidate) > self._selection_cursor
                ),
                0,
            )
            candidates = candidates[pivot:] + candidates[:pivot]
        if len(candidates) > config.max_sessions_per_sweep:
            reasons["batch_limited"] += len(candidates) - config.max_sessions_per_sweep
        candidates = candidates[: config.max_sessions_per_sweep]
        deleted = 0
        for candidate in candidates:
            # Rejected or failed debt consumes this attempt, not every future
            # sweep's budget. Advance before any external teardown boundary.
            self._selection_cursor = self._candidate_order(candidate)
            if not self._revalidate(candidate, reasons):
                continue
            try:
                self._teardown(candidate.label, candidate.workspace_id)
                deleted += 1
            except Exception:
                reasons["teardown_failed"] += 1

        summary = CleanupSweepSummary(
            selected=len(candidates),
            deleted=deleted,
            reasons=dict(sorted(reasons.items())),
        )
        self._log(summary)
        return summary

    @staticmethod
    def _log(summary: CleanupSweepSummary) -> None:
        logger.info(
            "runtime_cleanup selected=%d deleted=%d reasons=%s",
            summary.selected,
            summary.deleted,
            summary.reasons,
        )


def build_runtime_resource_cleanup(
    *,
    registry: object | None = None,
    backend: object | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RuntimeResourceCleanup:
    """Bind the pure engine to CAO repositories for lifespan execution."""
    from cli_agent_orchestrator.backends.registry import get_backend
    from cli_agent_orchestrator.clients.database import (
        get_pending_messages,
        list_runtime_cleanup_debt,
        list_terminals_by_session,
    )
    from cli_agent_orchestrator.services.config_service import ConfigService
    from cli_agent_orchestrator.services.session_service import (
        delete_session_automatically,
    )

    selected_backend = backend or get_backend()

    def teardown(label: str, workspace_id: str) -> None:
        result = delete_session_automatically(
            label,
            expected_backend_id=workspace_id,
            registry=registry,  # type: ignore[arg-type]
            backend=selected_backend,  # type: ignore[arg-type]
        )
        deferred_ids = result.get("deferred_ids") or []
        if not result.get("deleted") or deferred_ids:
            raise RuntimeError("automatic cleanup incomplete")

    return RuntimeResourceCleanup(
        backend=selected_backend,
        config_reader=lambda: ConfigService.get_config().cleanup,
        terminal_reader=list_terminals_by_session,
        pending_checker=lambda terminal_id: bool(get_pending_messages(terminal_id, limit=1)),
        teardown=teardown,
        clock=clock or datetime.now,
        debt_reader=list_runtime_cleanup_debt,
    )

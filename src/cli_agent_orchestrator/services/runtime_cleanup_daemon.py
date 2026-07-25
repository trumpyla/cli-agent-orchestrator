"""Lifespan-owned scheduling and serialization for runtime resource cleanup."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from cli_agent_orchestrator.services.config_service import CleanupConfig
from cli_agent_orchestrator.services.runtime_resource_cleanup import CleanupSweepSummary

logger = logging.getLogger(__name__)


def run_cleanup_with_file_lock(
    sweep_runner: Callable[[], CleanupSweepSummary],
    lock_path: Path,
) -> CleanupSweepSummary:
    """Run one synchronous sweep while holding a nonblocking process lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return CleanupSweepSummary(reasons={"lock_held": 1})
        try:
            return sweep_runner()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class RuntimeCleanupDaemon:
    """Delay, hot-reread, serialize, and safely await cleanup sweeps."""

    def __init__(
        self,
        *,
        policy_reader: Callable[[], CleanupConfig],
        sweep_runner: Callable[[], CleanupSweepSummary],
        lock_path: Path,
        waiter: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], datetime] = datetime.now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        skew_tolerance_s: float = 5.0,
    ) -> None:
        self._policy_reader = policy_reader
        self._sweep_runner = sweep_runner
        self._lock_path = lock_path
        self._waiter = waiter
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._skew_tolerance_s = skew_tolerance_s
        self._run_lock = asyncio.Lock()
        self.worker_task: asyncio.Task[CleanupSweepSummary] | None = None
        self.last_skip_reason: str | None = None

    async def run_once(self) -> CleanupSweepSummary:
        """Start at most one off-loop worker and shield it through cancellation."""
        if self._run_lock.locked():
            return CleanupSweepSummary(reasons={"overlap": 1})

        async with self._run_lock:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    run_cleanup_with_file_lock,
                    self._sweep_runner,
                    self._lock_path,
                )
            )
            self.worker_task = worker
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # The shield kept the thread-backed worker alive. Finish it
                # before allowing lifespan cancellation to unwind.
                await worker
                raise
            finally:
                self.worker_task = None

    async def run_forever(self) -> None:
        """Sleep before every sweep, reread policy, and reject clock anomalies."""
        previous_wall = self._wall_clock()
        previous_monotonic = self._monotonic_clock()
        while True:
            try:
                initial_policy = self._policy_reader()
                interval = float(initial_policy.sweep_interval_s)
            except Exception:
                interval = 300.0
                self.last_skip_reason = "config_invalid"

            await self._waiter(interval)

            current_wall = self._wall_clock()
            current_monotonic = self._monotonic_clock()
            wall_delta = (current_wall - previous_wall).total_seconds()
            monotonic_delta = current_monotonic - previous_monotonic
            previous_wall = current_wall
            previous_monotonic = current_monotonic
            if (
                wall_delta < 0
                or monotonic_delta < 0
                or abs(wall_delta - monotonic_delta) > self._skew_tolerance_s
            ):
                self.last_skip_reason = "clock_skew"
                logger.warning("runtime_cleanup skipped reason=clock_skew")
                continue

            try:
                current_policy = self._policy_reader()
            except Exception:
                self.last_skip_reason = "config_invalid"
                logger.warning("runtime_cleanup skipped reason=config_invalid")
                continue
            if not current_policy.completed_sessions_enabled:
                self.last_skip_reason = "disabled"
                continue

            self.last_skip_reason = None
            await self.run_once()

"""Deterministic lifecycle tests for the completed-session cleanup daemon."""

from __future__ import annotations

import asyncio
import fcntl
import threading
from datetime import datetime, timedelta

import pytest

from cli_agent_orchestrator.services.config_service import CleanupConfig
from cli_agent_orchestrator.services.runtime_cleanup_daemon import (
    RuntimeCleanupDaemon,
    run_cleanup_with_file_lock,
)
from cli_agent_orchestrator.services.runtime_resource_cleanup import CleanupSweepSummary

NOW = datetime(2026, 7, 25, 12, 0, 0)


def policy(enabled: bool = True, interval: int = 30) -> CleanupConfig:
    return CleanupConfig(
        completed_sessions_enabled=enabled,
        sweep_interval_s=interval,
    )


def test_first_sweep_is_delayed_and_policy_is_reread(tmp_path):
    async def exercise() -> None:
        entered_wait = asyncio.Event()
        release_wait = asyncio.Event()
        sweep_called = asyncio.Event()
        policy_reads = 0
        wait_calls = 0

        def read_policy() -> CleanupConfig:
            nonlocal policy_reads
            policy_reads += 1
            return policy(enabled=policy_reads == 1)

        async def waiter(_seconds: float) -> None:
            nonlocal wait_calls
            wait_calls += 1
            entered_wait.set()
            if wait_calls == 1:
                await release_wait.wait()
            else:
                await asyncio.Future()

        daemon = RuntimeCleanupDaemon(
            policy_reader=read_policy,
            sweep_runner=lambda: sweep_called.set() or CleanupSweepSummary(),
            lock_path=tmp_path / "cleanup.lock",
            waiter=waiter,
        )
        task = asyncio.create_task(daemon.run_forever())
        await entered_wait.wait()
        assert not sweep_called.is_set()

        release_wait.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert policy_reads >= 2
        assert not sweep_called.is_set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())


@pytest.mark.parametrize("_repeat", range(10), ids=lambda value: f"run-{value + 1}")
def test_run_once_is_off_loop_and_in_process_nonoverlap_skips(tmp_path, _repeat):
    async def exercise() -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocked_sweep() -> CleanupSweepSummary:
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=2)
            return CleanupSweepSummary(deleted=1)

        daemon = RuntimeCleanupDaemon(
            policy_reader=policy,
            sweep_runner=blocked_sweep,
            lock_path=tmp_path / "cleanup.lock",
        )
        first = asyncio.create_task(daemon.run_once())
        assert await asyncio.to_thread(entered.wait, 1)

        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.2)

        second = await daemon.run_once()
        assert second.reasons == {"overlap": 1}
        assert calls == 1

        release.set()
        assert (await first).deleted == 1

    asyncio.run(exercise())


def test_file_lock_contention_skips_without_running_sweep(tmp_path):
    lock_path = tmp_path / "cleanup.lock"
    lock_path.touch()
    calls = 0

    def sweep() -> CleanupSweepSummary:
        nonlocal calls
        calls += 1
        return CleanupSweepSummary()

    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        summary = run_cleanup_with_file_lock(sweep, lock_path)

    assert summary.reasons == {"lock_held": 1}
    assert calls == 0


def test_cancellation_during_worker_waits_for_thread_completion(tmp_path):
    async def exercise() -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_sweep() -> CleanupSweepSummary:
            entered.set()
            assert release.wait(timeout=2)
            return CleanupSweepSummary(deleted=1)

        daemon = RuntimeCleanupDaemon(
            policy_reader=policy,
            sweep_runner=blocked_sweep,
            lock_path=tmp_path / "cleanup.lock",
        )
        task = asyncio.create_task(daemon.run_once())
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert daemon.worker_task is not None

        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert daemon.worker_task is None

    asyncio.run(exercise())


def test_cancellation_during_initial_sleep_is_prompt(tmp_path):
    async def exercise() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def waiter(_seconds: float) -> None:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        daemon = RuntimeCleanupDaemon(
            policy_reader=policy,
            sweep_runner=lambda: CleanupSweepSummary(),
            lock_path=tmp_path / "cleanup.lock",
            waiter=waiter,
        )
        task = asyncio.create_task(daemon.run_forever())
        await entered.wait()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=0.2)
        except asyncio.CancelledError:
            pass
        assert cancelled.is_set()
        assert daemon.worker_task is None

    asyncio.run(exercise())


def test_wall_clock_jump_skips_sweep(tmp_path):
    async def exercise() -> None:
        wall_values = iter([NOW, NOW + timedelta(seconds=120)])
        mono_values = iter([10.0, 40.0])
        sweep_called = asyncio.Event()
        wake = asyncio.Event()
        wait_calls = 0

        async def waiter(_seconds: float) -> None:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                wake.set()
                return
            await asyncio.Future()

        daemon = RuntimeCleanupDaemon(
            policy_reader=policy,
            sweep_runner=lambda: sweep_called.set() or CleanupSweepSummary(),
            lock_path=tmp_path / "cleanup.lock",
            waiter=waiter,
            wall_clock=lambda: next(wall_values),
            monotonic_clock=lambda: next(mono_values),
            skew_tolerance_s=5.0,
        )
        task = asyncio.create_task(daemon.run_forever())
        await wake.wait()
        await asyncio.sleep(0)
        assert not sweep_called.is_set()
        assert daemon.last_skip_reason == "clock_skew"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

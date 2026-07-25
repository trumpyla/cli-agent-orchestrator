import anyio
import pytest
from httpx import HTTPStatusError, Request, Response

from cli_agent_orchestrator.ops_mcp_server.subscriptions import FastMcp32SessionTasks


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


class FakeSession:
    def __init__(self, task_group: anyio.abc.TaskGroup):
        self._subscription_task_group = task_group


@pytest.mark.anyio
async def test_duplicate_subscribe_idempotent():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        run_count = 0
        started_event = anyio.Event()

        async def worker():
            nonlocal run_count
            run_count += 1
            started_event.set()
            await anyio.sleep_forever()

        await tasks.subscribe("resource://1", worker)
        await started_event.wait()

        # Duplicate subscribe
        await tasks.subscribe("resource://1", worker)

        # Teardown
        await tasks.unsubscribe("resource://1")

    assert run_count == 1
    assert len(tasks._registry) == 0


@pytest.mark.anyio
async def test_unsubscribe_cancels_and_awaits():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        started_event = anyio.Event()
        cleanup_unblocked = anyio.Event()
        cleanup_completed = False

        async def worker():
            nonlocal cleanup_completed
            started_event.set()
            try:
                await anyio.sleep_forever()
            finally:
                with anyio.CancelScope(shield=True):
                    await cleanup_unblocked.wait()
                    cleanup_completed = True

        await tasks.subscribe("resource://1", worker)
        await started_event.wait()

        async def do_unsubscribe():
            await tasks.unsubscribe("resource://1")

        async with anyio.create_task_group() as tg2:
            tg2.start_soon(do_unsubscribe)
            await anyio.lowlevel.checkpoint()
            assert not cleanup_completed
            cleanup_unblocked.set()

        assert cleanup_completed is True
        assert len(tasks._registry) == 0


@pytest.mark.anyio
async def test_explicit_session_finalization_cancels_all():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        cleanup_completed_1 = False
        cleanup_completed_2 = False

        async def worker1():
            nonlocal cleanup_completed_1
            try:
                await anyio.sleep_forever()
            finally:
                cleanup_completed_1 = True

        async def worker2():
            nonlocal cleanup_completed_2
            try:
                await anyio.sleep_forever()
            finally:
                cleanup_completed_2 = True

        await tasks.subscribe("r://1", worker1)
        await tasks.subscribe("r://2", worker2)

        # Give them a moment to start and block
        await anyio.lowlevel.checkpoint()

        # Explicit cancel all
        await tasks.cancel_all()

        assert cleanup_completed_1 is True
        assert cleanup_completed_2 is True
        assert len(tasks._registry) == 0


@pytest.mark.anyio
async def test_worker_auth_failure_isolated():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        healthy_started = anyio.Event()

        async def auth_failing_worker():
            req = Request("GET", "http://example.com")
            res = Response(401, request=req)
            raise HTTPStatusError("Unauthorized", request=req, response=res)

        async def healthy_worker():
            healthy_started.set()
            await anyio.sleep_forever()

        await tasks.subscribe("r://healthy", healthy_worker)
        await healthy_started.wait()

        await tasks.subscribe("r://fail", auth_failing_worker)
        # Give it a moment to fail
        await anyio.lowlevel.checkpoint()

        # Registry should not have the failed worker
        assert "r://fail" not in tasks._registry
        # The healthy one should remain
        assert "r://healthy" in tasks._registry

        await tasks.unsubscribe("r://healthy")


@pytest.mark.anyio
async def test_worker_generic_failure_isolated():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        async def failing_worker():
            raise ValueError("Some random error")

        await tasks.subscribe("r://fail", failing_worker)
        await anyio.lowlevel.checkpoint()
        assert len(tasks._registry) == 0


@pytest.mark.anyio
async def test_inactive_task_group_rollback():
    # If the task group is cancelled/inactive before start_soon
    with anyio.CancelScope() as scope:
        pass
    # Scope is now inactive. If we use a task group inside it, it fails? No, just create a new task group and cancel it.
    async with anyio.create_task_group() as tg:
        tg.cancel_scope.cancel()

    session = FakeSession(tg)
    tasks = FastMcp32SessionTasks(session)

    async def worker():
        pass

    with pytest.raises(RuntimeError):
        await tasks.subscribe("r://1", worker)

    assert len(tasks._registry) == 0
    # And we can resubscribe if the task group is replaced
    async with anyio.create_task_group() as tg2:
        tasks = FastMcp32SessionTasks(FakeSession(tg2))
        await tasks.subscribe("r://1", worker)
        await tasks.unsubscribe("r://1")


@pytest.mark.anyio
@pytest.mark.parametrize("clients", [1, 5, 10])
async def test_concurrent_sub_unsub_races(clients):
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        async def client_lifecycle(i):
            resource = f"resource://{i % 3}"

            async def worker():
                await anyio.sleep_forever()

            await tasks.subscribe(resource, worker)
            await anyio.lowlevel.checkpoint()
            await tasks.unsubscribe(resource)

        async with anyio.create_task_group() as ctg:
            for i in range(clients):
                ctg.start_soon(client_lifecycle, i)

    assert len(tasks._registry) == 0

@pytest.mark.anyio
async def test_real_fastmcp_delete_finalization():
    from fastmcp import FastMCP
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams
    from pydantic import AnyUrl
    
    server = FastMCP("test-delete")
    low = server._mcp_server
    worker_started = anyio.Event()
    server_done = anyio.Event()
    captured = {}

    @low.subscribe_resource()
    async def subscribe(uri: AnyUrl) -> None:
        session = low.request_context.session
        tasks = FastMcp32SessionTasks(session)
        captured["tasks"] = tasks

        async def worker():
            worker_started.set()
            await anyio.sleep_forever()

        await tasks.subscribe(str(uri), worker)

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async def run_server():
            try:
                await low.run(server_read, server_write, low.create_initialization_options())
            finally:
                server_done.set()

        async with anyio.create_task_group() as outer:
            outer.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                await client.subscribe_resource(AnyUrl("cao://peers/deadbeef/inbox"))
                await worker_started.wait()
                assert len(captured["tasks"]._registry) == 1
            
            # Client disconnected
            with anyio.move_on_after(0.25) as wait_scope:
                await server_done.wait()
            
            assert not wait_scope.cancel_called, "Server exit timed out"
            assert len(captured["tasks"]._registry) == 0
            outer.cancel_scope.cancel()


@pytest.mark.anyio
async def test_cancel_all_racing_late():
    async with anyio.create_task_group() as tg:
        session = FakeSession(tg)
        tasks = FastMcp32SessionTasks(session)

        # Start a worker that will be cancelled
        worker1_started = anyio.Event()
        async def worker1():
            worker1_started.set()
            await anyio.sleep_forever()

        await tasks.subscribe("r://1", worker1)
        await worker1_started.wait()

        # Concurrently call cancel_all and subscribe
        async def do_cancel_all():
            await tasks.cancel_all()

        async def late_subscriber():
            with pytest.raises(RuntimeError, match="Session is closing"):
                await tasks.subscribe("r://2", worker1)

        async with anyio.create_task_group() as tg2:
            tg2.start_soon(do_cancel_all)
            await anyio.sleep(0.01)  # Ensure cancel_all is in progress
            tg2.start_soon(late_subscriber)
            
        assert len(tasks._registry) == 0



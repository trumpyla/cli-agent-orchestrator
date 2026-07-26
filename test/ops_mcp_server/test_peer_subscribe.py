"""Tests for the peer-inbox bridge push lanes (bi-directional bridge).

Lane #6 (universal, shipped): receive_messages(wait_seconds=) long-poll routing.
Lane #1 (future-ready): resources/subscribe seam + resource-updated consumer.
"""

import asyncio
import inspect
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cli_agent_orchestrator.ops_mcp_server.backend import HttpxRequestBackend
from cli_agent_orchestrator.ops_mcp_server.server import (
    _PEER_URI_RE,
    FastMcp32SessionTasks,
    _active_backend,
    _async_request_json,
    _consume_inbox,
    _long_poll_inbox,
    _peer_inbox_uri,
    _PeerConsumerHandle,
    _request_json,
    _setup_peer_subscribe,
    mcp,
    receive_messages,
)

PEER = "deadbeef"


class _TestSessionTaskGroup:
    """Minimal asyncio-backed model of BaseSession's owning task group."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[None]] = set()

    def start_soon(self, func, *args) -> None:
        task = asyncio.create_task(func(*args))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def cancel_all(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _test_session() -> AsyncMock:
    session = AsyncMock()
    session._task_group = _TestSessionTaskGroup()
    session._exit_stack = AsyncExitStack()
    session._cao_peer_session_tasks = None
    return session


async def _cleanup_test_sessions(*sessions: AsyncMock) -> None:
    await asyncio.gather(
        *(session._exit_stack.aclose() for session in sessions),
    )
    await asyncio.gather(
        *(session._task_group.cancel_all() for session in sessions),
    )


def _response(*, status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = json_data
    return r


def _low_level_server(captured):
    counts = {"subscribe": 0, "unsubscribe": 0}

    def _create_init_opts(*a, **k):
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                resources=SimpleNamespace(subscribe=False, listChanged=True)
            )
        )

    def _sub():
        counts["subscribe"] += 1

        def deco(fn):
            captured["subscribe"] = fn
            return fn

        return deco

    def _unsub():
        counts["unsubscribe"] += 1

        def deco(fn):
            captured["unsubscribe"] = fn
            return fn

        return deco

    return (
        SimpleNamespace(
            create_initialization_options=_create_init_opts,
            subscribe_resource=_sub,
            unsubscribe_resource=_unsub,
            request_context=SimpleNamespace(session=_test_session()),
        ),
        counts,
    )


@pytest.mark.asyncio
class TestReceiveMessagesLanes:
    async def test_wait_zero_is_immediate_pull(self):
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server._async_request_json",
            new=AsyncMock(return_value=([], None)),
        ) as mock_req:
            result = await receive_messages(PEER)
        assert result["success"] is True
        kwargs = mock_req.await_args.kwargs
        assert kwargs["params"] == {"status": "pending", "limit": 10}
        assert "timeout" not in kwargs  # no long-poll -> no timeout override

    async def test_wait_forwards_wait_and_timeout(self):
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server._async_request_json",
            new=AsyncMock(return_value=([], None)),
        ) as mock_req:
            await receive_messages(PEER, wait_seconds=60.0)
        kwargs = mock_req.await_args.kwargs
        assert kwargs["params"]["wait"] == 60.0
        assert kwargs["timeout"] == 65.0  # wait + 5 headroom

    async def test_after_id_forwards_exclusive_cursor(self):
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server._async_request_json",
            new=AsyncMock(return_value=([], None)),
        ) as mock_req:
            await receive_messages(PEER, wait_seconds=60.0, after_id=41)
        kwargs = mock_req.await_args.kwargs
        assert kwargs["params"]["after_id"] == 41

    async def test_receive_messages_uses_cancelable_async_http(self):
        """Long-poll cancellation must not strand a synchronous worker thread."""
        async_request = AsyncMock(return_value=([], None))
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server._async_request_json",
                async_request,
                create=True,
            ),
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.asyncio.to_thread",
                side_effect=AssertionError("receive_messages used a blocking worker"),
            ),
        ):
            result = await receive_messages(PEER, wait_seconds=60.0)

        assert result["success"] is True
        async_request.assert_awaited_once()


def test_peer_uri_regex_accepts_only_8hex():
    assert _PEER_URI_RE.match("cao://peers/deadbeef/inbox").group(1) == "deadbeef"
    assert _PEER_URI_RE.match("cao://peers/peer-abc/inbox") is None  # non-hex
    assert _PEER_URI_RE.match("cao://peers/DEADBEEF/inbox") is None  # uppercase
    assert _peer_inbox_uri("deadbeef") == "cao://peers/deadbeef/inbox"


def test_subscription_long_poll_is_async_and_cancelable():
    assert inspect.iscoroutinefunction(_long_poll_inbox)


@pytest.mark.asyncio
async def test_peer_inbox_resource_is_fastmcp_readable_json():
    messages = [{"id": 7, "message": "ready", "status": "pending"}]
    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server._request_json",
        return_value=(messages, None),
    ):
        result = await mcp.read_resource(f"cao://peers/{PEER}/inbox")

    assert len(result.contents) == 1
    assert json.loads(result.contents[0].content) == {
        "peer_id": PEER,
        "messages": messages,
    }


def test_setup_advertises_subscribe_and_registers_handlers():
    captured: dict = {}
    low, counts = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))

    opts = low.create_initialization_options()
    assert opts.capabilities.resources.subscribe is True  # flipped on via the seam
    assert "subscribe" in captured and "unsubscribe" in captured  # handlers registered
    assert counts == {"subscribe": 1, "unsubscribe": 1}


def test_setup_no_mcp_server_is_noop():
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=None))  # must not raise


@pytest.mark.asyncio
async def test_consumer_emits_resource_updated_without_body():
    session = AsyncMock()
    state = {"n": 0}

    def fake_poll(peer_id, wait, after_id=None):
        state["n"] += 1
        if state["n"] == 1:
            return [{"id": 1, "sender_id": "cond0001", "message": "secret"}]
        raise asyncio.CancelledError()

    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server._long_poll_inbox",
        side_effect=fake_poll,
    ):
        with pytest.raises(asyncio.CancelledError):
            await _consume_inbox(PEER, session)

    session.send_resource_updated.assert_awaited_once()
    (arg,), _ = session.send_resource_updated.await_args
    # The push carries only the resource URI — never the message body.
    assert "deadbeef" in str(arg)
    assert "secret" not in str(arg)


@pytest.mark.asyncio
async def test_consumer_notifies_once_per_new_message_id():
    session = AsyncMock()
    cursors = []
    rows = [
        [{"id": 1}],
        [{"id": 1}],
        [{"id": 2}],
    ]

    def fake_poll(peer_id, wait, after_id=None):
        cursors.append(after_id)
        if rows:
            return rows.pop(0)
        raise asyncio.CancelledError()

    with (
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server._long_poll_inbox",
            side_effect=fake_poll,
        ),
        patch("cli_agent_orchestrator.ops_mcp_server.server.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _consume_inbox(PEER, session)

    assert session.send_resource_updated.await_count == 2
    assert cursors == [0, 1, 1, 2]


@pytest.mark.asyncio
async def test_notification_failure_terminates_consumer():
    session = AsyncMock()
    session.send_resource_updated.side_effect = RuntimeError("transport closed")
    polls = [
        [{"id": 1}],
        asyncio.CancelledError(),
    ]

    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server._long_poll_inbox",
        side_effect=polls,
    ):
        await _consume_inbox(PEER, session)

    session.send_resource_updated.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscriptions_are_idempotent_per_session_and_unsubscribe_awaits_cancel():
    captured = {}
    low, _ = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))
    stopped = {}

    async def consumer(peer_id, session):
        try:
            await asyncio.Event().wait()
        finally:
            stopped[id(session)] = True

    session_one = _test_session()
    session_two = _test_session()
    try:
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server._consume_inbox",
            side_effect=consumer,
        ):
            low.request_context.session = session_one
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await asyncio.sleep(0)
            assert session_one._cao_peer_session_tasks.consumer_count == 1

            low.request_context.session = session_two
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await asyncio.sleep(0)
            assert session_one._cao_peer_session_tasks.consumer_count == 1
            assert session_two._cao_peer_session_tasks.consumer_count == 1

            low.request_context.session = session_one
            await captured["unsubscribe"](_peer_inbox_uri(PEER))
            assert stopped[id(session_one)] is True
            assert session_one._cao_peer_session_tasks.consumer_count == 0
            assert session_two._cao_peer_session_tasks.consumer_count == 1
    finally:
        await _cleanup_test_sessions(session_one, session_two)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_seam",
    [
        pytest.param("_task_group", id="owning-task-group"),
        pytest.param("_exit_stack", id="session-finalizer"),
    ],
)
async def test_subscription_fails_closed_when_session_ownership_seam_is_missing(
    missing_seam: str,
) -> None:
    captured = {}
    low, _ = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))
    session = low.request_context.session
    setattr(session, missing_seam, None)

    with pytest.raises(RuntimeError, match="FastMCP 3.2 session"):
        await captured["subscribe"](_peer_inbox_uri(PEER))

    assert session._cao_peer_session_tasks is None


@pytest.mark.asyncio
async def test_unsubscribe_absorbs_already_failed_consumer():
    """A same-tick completed handle must not fail resources/unsubscribe."""
    captured = {}
    low, _ = _low_level_server(captured)
    fastmcp = SimpleNamespace(_mcp_server=low)
    _setup_peer_subscribe(fastmcp)
    session = low.request_context.session

    session_tasks = FastMcp32SessionTasks.attach(session, None)
    handle = _PeerConsumerHandle()
    handle.ready.set()
    handle.done.set()
    session_tasks._consumers[PEER] = handle
    await captured["unsubscribe"](_peer_inbox_uri(PEER))

    assert PEER not in session_tasks._consumers
    await _cleanup_test_sessions(session)


@pytest.mark.asyncio
async def test_completed_subscription_task_is_removed():
    captured = {}
    low, _ = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))

    async def consumer(peer_id, session):
        return None

    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server._consume_inbox",
        side_effect=consumer,
    ):
        await captured["subscribe"](_peer_inbox_uri(PEER))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert low.request_context.session._cao_peer_session_tasks.consumer_count == 0
    await _cleanup_test_sessions(low.request_context.session)


@pytest.mark.asyncio
async def test_failed_subscription_task_is_removed():
    captured = {}
    low, _ = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))

    async def consumer(peer_id, session):
        raise RuntimeError("consumer failed")

    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server._consume_inbox",
        side_effect=consumer,
    ):
        await captured["subscribe"](_peer_inbox_uri(PEER))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert low.request_context.session._cao_peer_session_tasks.consumer_count == 0
    await _cleanup_test_sessions(low.request_context.session)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_auth_rejected_subscription_is_removed_and_can_be_recreated(
    status_code: int,
) -> None:
    captured = {}
    low, _ = _low_level_server(captured)
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))
    session = low.request_context.session
    authorized = False

    async def inbox_response(request: httpx.Request) -> httpx.Response:
        if not authorized:
            return httpx.Response(
                status_code,
                json={"detail": "subscription credential rejected"},
                request=request,
            )
        return httpx.Response(200, json=[], request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(inbox_response),
        base_url="http://127.0.0.1:9889",
    )
    backend = HttpxRequestBackend(
        base_url="http://127.0.0.1:9889",
        client=client,
    )
    backend_token = _active_backend.set(backend)
    try:
        await captured["subscribe"](_peer_inbox_uri(PEER))

        async def wait_for_consumer_count(expected: int) -> None:
            while session._cao_peer_session_tasks.consumer_count != expected:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_consumer_count(0), timeout=0.2)

        authorized = True
        await captured["subscribe"](_peer_inbox_uri(PEER))
        await asyncio.wait_for(wait_for_consumer_count(1), timeout=0.2)
    finally:
        _active_backend.reset(backend_token)
        await backend.aclose()
        await _cleanup_test_sessions(session)


def test_request_json_forwards_local_bearer_without_logging_it(caplog):
    with (
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.get_local_bearer",
            return_value="top-secret-token",
        ),
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data={"ok": True}),
        ) as request,
    ):
        data, error = _request_json("get", "/profiles", operation="List profiles")

    assert error is None and data == {"ok": True}
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer top-secret-token"}
    assert "top-secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_subscription_long_poll_forwards_local_bearer():
    response = _response(json_data=[])
    response.status_code = 200
    client = AsyncMock()
    client.request.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    with (
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.get_local_bearer",
            return_value="machine-token",
        ),
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.httpx.AsyncClient",
            return_value=context,
        ),
    ):
        assert await _long_poll_inbox(PEER, 25.0, 7) == []

    assert client.request.await_args.kwargs["headers"] == {"Authorization": "Bearer machine-token"}
    assert client.request.await_args.kwargs["params"]["after_id"] == 7


@pytest.mark.asyncio
async def test_async_request_without_override_disables_httpx_default_timeout():
    response = _response(json_data=[])
    client = AsyncMock()
    client.request.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client
    with patch(
        "cli_agent_orchestrator.ops_mcp_server.server.httpx.AsyncClient",
        return_value=context,
    ):
        data, error = await _async_request_json(
            "get",
            "/terminals/deadbeef/inbox/messages",
            operation="Immediate inbox pull",
        )

    assert error is None and data == []
    assert client.request.await_args.kwargs["timeout"] is None

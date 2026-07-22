"""Tests for the peer-inbox bridge push lanes (bi-directional bridge).

Lane #6 (universal, shipped): receive_messages(wait_seconds=) long-poll routing.
Lane #1 (future-ready): resources/subscribe seam + resource-updated consumer.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.ops_mcp_server.server import (
    _PEER_URI_RE,
    _consume_inbox,
    _long_poll_inbox,
    _peer_consumers,
    _peer_inbox_uri,
    _request_json,
    _setup_peer_subscribe,
    receive_messages,
)

PEER = "deadbeef"


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
            request_context=SimpleNamespace(session=AsyncMock()),
        ),
        counts,
    )


@pytest.mark.asyncio
class TestReceiveMessagesLanes:
    async def test_wait_zero_is_immediate_pull(self):
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ) as mock_req:
            result = await receive_messages(PEER)
        assert result["success"] is True
        _, kwargs = mock_req.call_args
        assert kwargs["params"] == {"status": "pending", "limit": 10}
        assert "timeout" not in kwargs  # no long-poll -> no timeout override

    async def test_wait_forwards_wait_and_timeout(self):
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ) as mock_req:
            await receive_messages(PEER, wait_seconds=60.0)
        _, kwargs = mock_req.call_args
        assert kwargs["params"]["wait"] == 60.0
        assert kwargs["timeout"] == 65.0  # wait + 5 headroom


def test_peer_uri_regex_accepts_only_8hex():
    assert _PEER_URI_RE.match("cao://peers/deadbeef/inbox").group(1) == "deadbeef"
    assert _PEER_URI_RE.match("cao://peers/peer-abc/inbox") is None  # non-hex
    assert _PEER_URI_RE.match("cao://peers/DEADBEEF/inbox") is None  # uppercase
    assert _peer_inbox_uri("deadbeef") == "cao://peers/deadbeef/inbox"


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

    session_one = AsyncMock()
    session_two = AsyncMock()
    try:
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server._consume_inbox",
            side_effect=consumer,
        ):
            low.request_context.session = session_one
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await asyncio.sleep(0)
            assert len(_peer_consumers) == 1

            low.request_context.session = session_two
            await captured["subscribe"](_peer_inbox_uri(PEER))
            await asyncio.sleep(0)
            assert len(_peer_consumers) == 2

            low.request_context.session = session_one
            await captured["unsubscribe"](_peer_inbox_uri(PEER))
            assert stopped[id(session_one)] is True
            assert len(_peer_consumers) == 1
    finally:
        tasks = list(_peer_consumers.values())
        _peer_consumers.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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

    assert _peer_consumers == {}


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

    assert _peer_consumers == {}


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


def test_subscription_long_poll_forwards_local_bearer():
    with (
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.get_local_bearer",
            return_value="machine-token",
        ),
        patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.get",
            return_value=_response(json_data=[]),
        ) as get,
    ):
        assert _long_poll_inbox(PEER, 25.0, 7) == []

    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer machine-token"}
    assert get.call_args.kwargs["params"]["after_id"] == 7

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
    _peer_inbox_uri,
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

    def _create_init_opts(*a, **k):
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                resources=SimpleNamespace(subscribe=False, listChanged=True)
            )
        )

    def _sub():
        def deco(fn):
            captured["subscribe"] = fn
            return fn
        return deco

    def _unsub():
        def deco(fn):
            captured["unsubscribe"] = fn
            return fn
        return deco

    low = SimpleNamespace(
        create_initialization_options=_create_init_opts,
        subscribe_resource=_sub,
        unsubscribe_resource=_unsub,
    )
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=low))

    opts = low.create_initialization_options()
    assert opts.capabilities.resources.subscribe is True  # flipped on via the seam
    assert "subscribe" in captured and "unsubscribe" in captured  # handlers registered


def test_setup_no_mcp_server_is_noop():
    _setup_peer_subscribe(SimpleNamespace(_mcp_server=None))  # must not raise


@pytest.mark.asyncio
async def test_consumer_emits_resource_updated_without_body():
    session = AsyncMock()
    state = {"n": 0}

    def fake_poll(peer_id, wait):
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

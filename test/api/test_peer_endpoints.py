"""Tests for the peer endpoints (bi-directional bridge):

- ``POST /peers`` — register a pane-less peer, returns an 8-hex id.
- ``POST /terminals/{id}/inbox/ack`` — explicit ack (the GET does not ack on read).

The DB layer is patched at the seam; these assert the HTTP contract, including the
``TerminalId`` 8-hex path validation.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.inbox import MessageStatus


class TestRegisterPeerEndpoint:
    def test_register_peer_returns_8hex(self, client):
        with patch("cli_agent_orchestrator.api.main.create_peer") as mock_create:
            mock_create.return_value = "deadbeef"
            resp = client.post("/peers", json={"name": "driver-x"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["peer_id"] == "deadbeef"
            assert data["name"] == "driver-x"
            assert data["mode"] == "poll"
            mock_create.assert_called_once_with(name="driver-x")

    def test_register_peer_empty_body_defaults_name_none(self, client):
        with patch("cli_agent_orchestrator.api.main.create_peer") as mock_create:
            mock_create.return_value = "aabbccdd"
            resp = client.post("/peers", json={})
            assert resp.status_code == 200
            assert resp.json()["peer_id"] == "aabbccdd"
            mock_create.assert_called_once_with(name=None)

    def test_register_peer_failure_is_500(self, client):
        with patch("cli_agent_orchestrator.api.main.create_peer") as mock_create:
            mock_create.side_effect = RuntimeError("db down")
            resp = client.post("/peers", json={})
            assert resp.status_code == 500
            assert "Failed to register peer" in resp.json()["detail"]


class TestAckInboxEndpoint:
    def test_ack_marks_and_returns_count(self, client):
        with patch("cli_agent_orchestrator.api.main.mark_messages_delivered") as mock_ack:
            mock_ack.return_value = 2
            resp = client.post("/terminals/deadbeef/inbox/ack", json={"message_ids": [1, 2]})
            assert resp.status_code == 200
            assert resp.json()["acked"] == 2
            mock_ack.assert_called_once_with("deadbeef", [1, 2])

    def test_ack_rejects_non_8hex_peer_id(self, client):
        # TerminalId ^[a-f0-9]{8}$ path validation rejects the old 'peer-abc' shape.
        resp = client.post("/terminals/peer-abc/inbox/ack", json={"message_ids": [1]})
        assert resp.status_code == 422

    def test_ack_empty_list_is_ok(self, client):
        with patch("cli_agent_orchestrator.api.main.mark_messages_delivered") as mock_ack:
            mock_ack.return_value = 0
            resp = client.post("/terminals/deadbeef/inbox/ack", json={"message_ids": []})
            assert resp.status_code == 200
            assert resp.json()["acked"] == 0


class TestInboxLongPoll:
    """The wait= long-poll lane on GET .../inbox/messages (universal pseudo-push)."""

    def test_longpoll_returns_pending_immediately(self, client):
        msg = MagicMock(
            id=1, sender_id="cond0001", receiver_id="deadbeef",
            message="hi", status=MessageStatus.PENDING, created_at=datetime(2026, 7, 6),
        )
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages", return_value=[msg]):
            resp = client.get(
                "/terminals/deadbeef/inbox/messages", params={"status": "pending", "wait": 5}
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_longpoll_times_out_empty(self, client):
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages", return_value=[]):
            resp = client.get(
                "/terminals/deadbeef/inbox/messages", params={"status": "pending", "wait": 0.3}
            )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_longpoll_rejects_delivered_filter(self, client):
        resp = client.get(
            "/terminals/deadbeef/inbox/messages", params={"status": "delivered", "wait": 1}
        )
        assert resp.status_code == 400


class TestInboxEventPublish:
    """Storing an inbox message publishes a body-free wake event."""

    def test_post_message_publishes_body_free_event(self, client):
        msg = MagicMock(
            id=7, sender_id="cond0001", receiver_id="deadbeef", created_at=datetime(2026, 7, 6)
        )
        with patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=msg), \
                patch("cli_agent_orchestrator.api.main.inbox_service"), \
                patch("cli_agent_orchestrator.api.main.bus") as mock_bus:
            resp = client.post(
                "/terminals/deadbeef/inbox/messages",
                params={"sender_id": "cond0001", "message": "secret-body"},
            )
        assert resp.status_code == 200
        mock_bus.publish.assert_called_once()
        topic, data = mock_bus.publish.call_args[0]
        assert topic == "terminal.deadbeef.inbox"
        assert data["message_id"] == 7
        assert "message" not in data  # body-free privacy guard

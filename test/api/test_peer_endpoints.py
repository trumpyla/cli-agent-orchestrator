"""Tests for the peer endpoints (bi-directional bridge):

- ``POST /peers`` — register a pane-less peer, returns an 8-hex id.
- ``POST /terminals/{id}/inbox/ack`` — explicit ack (the GET does not ack on read).

The DB layer is patched at the seam; these assert the HTTP contract, including the
``TerminalId`` 8-hex path validation.
"""

from unittest.mock import patch


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

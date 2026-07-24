"""End-to-end peer-bridge flow against the real FastAPI app + a real temp DB (no mocks).

register peer -> worker sends to peer -> driver pulls pending -> long-poll (immediate) ->
ack -> delivered. Proves the HTTP layer, the DB/service layer, and the pane-less
delivery-skip work together (a peer message is stored PENDING and never pane-injected).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import cli_agent_orchestrator.clients.database as db


@pytest.fixture
def real_db(monkeypatch):
    """Point the app's SessionLocal at ONE shared in-memory SQLite for the whole flow.

    StaticPool + check_same_thread=False so the TestClient's worker thread and the test
    thread see the same in-memory database (a plain sqlite:///:memory: gives each
    connection its own empty DB).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))


def test_peer_bridge_end_to_end(client, real_db):
    # 1) driver registers a pane-less peer
    r = client.post("/peers", json={"name": "driver-e2e"})
    assert r.status_code == 200, r.text
    peer_id = r.json()["peer_id"]
    assert len(peer_id) == 8 and r.json()["mode"] == "poll"

    # 2) a conductor/worker replies to the peer via the existing send path
    r = client.post(
        f"/terminals/{peer_id}/inbox/messages",
        params={"sender_id": "cond0001", "message": "status: ready"},
    )
    assert r.status_code == 200, r.text
    msg_id = r.json()["message_id"]

    # 3) the message is PENDING and pullable — delivery was skipped (peer has no pane)
    r = client.get(f"/terminals/{peer_id}/inbox/messages", params={"status": "pending"})
    assert r.status_code == 200
    assert [m["message"] for m in r.json()] == ["status: ready"]

    # 4) long-poll returns immediately because a message is already pending
    r = client.get(f"/terminals/{peer_id}/inbox/messages", params={"status": "pending", "wait": 2})
    assert r.status_code == 200 and len(r.json()) == 1

    # 5) ack -> the message is delivered and no longer returned as pending
    r = client.post(f"/terminals/{peer_id}/inbox/ack", json={"message_ids": [msg_id]})
    assert r.status_code == 200 and r.json()["acked"] == 1

    r = client.get(f"/terminals/{peer_id}/inbox/messages", params={"status": "pending"})
    assert r.status_code == 200 and r.json() == []


def test_ack_rejects_non_8hex_peer_end_to_end(client, real_db):
    # The TerminalId path guard makes the old 'peer-abc' shape structurally impossible.
    r = client.post("/terminals/peer-abc/inbox/ack", json={"message_ids": [1]})
    assert r.status_code == 422


def test_cursor_wait_times_out_when_only_older_or_delivered_rows_exist(client, real_db):
    peer_id = client.post("/peers", json={}).json()["peer_id"]
    created = client.post(
        f"/terminals/{peer_id}/inbox/messages",
        params={"sender_id": "cond0001", "message": "old"},
    ).json()
    message_id = created["message_id"]
    ack = client.post(f"/terminals/{peer_id}/inbox/ack", json={"message_ids": [message_id]})
    assert ack.status_code == 200

    response = client.get(
        f"/terminals/{peer_id}/inbox/messages",
        params={"after_id": message_id, "wait": 0.01},
    )

    assert response.status_code == 200
    assert response.json() == []

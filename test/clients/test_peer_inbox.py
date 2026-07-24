"""Tests for the pane-less peer inbox (bi-directional bridge) — python-testing-patterns.

A REAL in-memory SQLite is bound into ``SessionLocal``, so
``create_peer`` -> ``create_terminal`` -> ``create_inbox_message`` ->
``mark_messages_delivered`` exercises the actual SQL rather than mocks. This is the
server half of @change:add-cao-conductor-agent-bridge.
"""

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db
from cli_agent_orchestrator.clients.database import (
    Base,
    create_inbox_message,
    create_peer,
    create_terminal,
    get_inbox_messages,
    is_peer,
    mark_messages_delivered,
)
from cli_agent_orchestrator.models.inbox import MessageStatus

HEX8 = re.compile(r"^[a-f0-9]{8}$")


@pytest.fixture
def real_db(monkeypatch):
    """Bind SessionLocal to a fresh in-memory SQLite for the test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", TestSession)
    return TestSession


def test_create_peer_mints_8hex_and_is_peer(real_db):
    peer_id = create_peer("driver-x")
    assert HEX8.match(peer_id)  # TerminalId ^[a-f0-9]{8}$ shape
    assert is_peer(peer_id) is True  # provider == "peer"


def test_non_peer_terminal_is_not_peer(real_db):
    create_terminal("aabbccdd", "cao-s", "w0", "claude_code")
    assert is_peer("aabbccdd") is False
    assert is_peer("deadbeef") is False  # unknown id -> False, not an error


def test_peer_is_a_valid_receiver(real_db):
    # The app-level receiver-exists check in create_inbox_message must pass for a peer.
    peer_id = create_peer()
    msg = create_inbox_message("cond0001", peer_id, "status: ready")
    assert msg.receiver_id == peer_id
    assert msg.status == MessageStatus.PENDING


def test_receive_then_ack_marks_delivered(real_db):
    peer_id = create_peer()
    m1 = create_inbox_message("cond0001", peer_id, "one")
    m2 = create_inbox_message("cond0001", peer_id, "two")

    pending = get_inbox_messages(peer_id, status=MessageStatus.PENDING)
    assert {m.id for m in pending} == {m1.id, m2.id}

    acked = mark_messages_delivered(peer_id, [m1.id])
    assert acked == 1

    still_pending = get_inbox_messages(peer_id, status=MessageStatus.PENDING)
    assert [m.id for m in still_pending] == [m2.id]  # m1 no longer re-returned


def test_ack_empty_is_noop(real_db):
    peer_id = create_peer()
    assert mark_messages_delivered(peer_id, []) == 0


def test_ack_scoped_to_receiver(real_db):
    # Acking with the wrong peer id must not touch another peer's messages.
    peer_a = create_peer()
    peer_b = create_peer()
    m = create_inbox_message("cond0001", peer_a, "for A")
    assert mark_messages_delivered(peer_b, [m.id]) == 0  # wrong owner -> no update
    assert [x.id for x in get_inbox_messages(peer_a, status=MessageStatus.PENDING)] == [m.id]


def test_ack_updates_only_pending_messages(real_db):
    peer_id = create_peer()
    pending = create_inbox_message("cond0001", peer_id, "pending")
    delivered = create_inbox_message("cond0001", peer_id, "delivered")
    failed = create_inbox_message("cond0001", peer_id, "failed")

    with real_db() as session:
        session.query(db.InboxModel).filter(db.InboxModel.id == delivered.id).update(
            {db.InboxModel.status: MessageStatus.DELIVERED.value}
        )
        session.query(db.InboxModel).filter(db.InboxModel.id == failed.id).update(
            {db.InboxModel.status: MessageStatus.FAILED.value}
        )
        session.commit()

    assert mark_messages_delivered(peer_id, [pending.id, delivered.id, failed.id]) == 1
    statuses = {message.id: message.status for message in get_inbox_messages(peer_id, limit=10)}
    assert statuses == {
        pending.id: MessageStatus.DELIVERED,
        delivered.id: MessageStatus.DELIVERED,
        failed.id: MessageStatus.FAILED,
    }


def test_cursor_filters_by_id_and_orders_ascending(real_db):
    peer_id = create_peer()
    first = create_inbox_message("cond0001", peer_id, "one")
    second = create_inbox_message("cond0001", peer_id, "two")
    third = create_inbox_message("cond0001", peer_id, "three")

    page_one = get_inbox_messages(
        peer_id,
        limit=1,
        status=MessageStatus.PENDING,
        after_id=first.id,
    )
    page_two = get_inbox_messages(
        peer_id,
        limit=10,
        status=MessageStatus.PENDING,
        after_id=second.id,
    )

    assert [message.id for message in page_one] == [second.id]
    assert [message.id for message in page_two] == [third.id]


def test_create_peer_ids_are_unique(real_db):
    ids = {create_peer() for _ in range(20)}
    assert len(ids) == 20
    assert all(HEX8.match(i) for i in ids)


def test_create_peer_retries_integrity_collision(real_db, monkeypatch):
    """The database uniqueness constraint, not a preflight read, drives retries."""
    original_create_terminal = db.create_terminal
    generated = iter(["deadbeef", "feedface"])
    first_attempt = True

    def collide_once(**kwargs):
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            original_create_terminal(**kwargs)
            raise IntegrityError("concurrent peer insert", params={}, orig=Exception("collision"))
        return original_create_terminal(**kwargs)

    monkeypatch.setattr("secrets.token_hex", lambda _n: next(generated))
    monkeypatch.setattr(db, "create_terminal", collide_once)

    assert create_peer("driver") == "feedface"
    assert is_peer("deadbeef") is True
    assert is_peer("feedface") is True

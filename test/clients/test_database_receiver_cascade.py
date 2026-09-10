"""Transactional receiver-inbox cleanup and SQLite contention regressions."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import cli_agent_orchestrator.clients.database as database
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def sqlite_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cao.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    try:
        yield sessions
    finally:
        engine.dispose()


def _terminal(terminal_id: str, session: str = "cao-test") -> None:
    database.create_terminal(terminal_id, session, terminal_id, "mock")


def _counts(sessions: sessionmaker[Session]) -> tuple[int, int]:
    with sessions() as connection:
        return (
            connection.query(database.TerminalModel).count(),
            connection.query(database.InboxModel).count(),
        )


def test_single_terminal_delete_cascades_only_receiver_rows(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("sender01", "cao-live")
    _terminal("receiver", "cao-delete")
    _terminal("live0001", "cao-live")
    pending = database.create_inbox_message("sender01", "receiver", "pending")
    delivered = database.create_inbox_message("sender01", "receiver", "delivered")
    sent_by_deleted = database.create_inbox_message("receiver", "live0001", "preserve")
    database.update_message_status(delivered.id, MessageStatus.DELIVERED)

    assert database.delete_terminal("receiver") is True

    with sqlite_database() as connection:
        assert connection.get(database.TerminalModel, "receiver") is None
        assert (
            connection.query(database.InboxModel)
            .filter(database.InboxModel.receiver_id == "receiver")
            .count()
            == 0
        )
        assert connection.get(database.InboxModel, sent_by_deleted.id) is not None
        assert connection.get(database.InboxModel, pending.id) is None
        assert connection.get(database.InboxModel, delivered.id) is None


def test_session_delete_cascades_every_receiver(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("delete01", "cao-delete")
    _terminal("delete02", "cao-delete")
    _terminal("live0001", "cao-live")
    database.create_inbox_message("live0001", "delete01", "one")
    database.create_inbox_message("live0001", "delete02", "two")
    preserved = database.create_inbox_message("delete01", "live0001", "three")

    assert database.delete_terminals_by_session("cao-delete") == 2

    with sqlite_database() as connection:
        assert (
            connection.query(database.TerminalModel)
            .filter(database.TerminalModel.tmux_session == "cao-delete")
            .count()
            == 0
        )
        assert (
            connection.query(database.InboxModel)
            .filter(database.InboxModel.receiver_id.in_(["delete01", "delete02"]))
            .count()
            == 0
        )
        assert connection.get(database.InboxModel, preserved.id) is not None


def test_id_list_delete_cascades_only_selected_receivers(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("delete01", "cao-delete")
    _terminal("delete02", "cao-delete")
    _terminal("live0001", "cao-live")
    first = database.create_inbox_message("live0001", "delete01", "one")
    second = database.create_inbox_message("live0001", "delete02", "two")
    preserved = database.create_inbox_message("delete01", "live0001", "three")

    assert database.delete_terminals_by_ids(["delete01", "delete02"]) == 2

    with sqlite_database() as connection:
        assert connection.get(database.TerminalModel, "delete01") is None
        assert connection.get(database.TerminalModel, "delete02") is None
        assert connection.get(database.TerminalModel, "live0001") is not None
        assert connection.get(database.InboxModel, first.id) is None
        assert connection.get(database.InboxModel, second.id) is None
        assert connection.get(database.InboxModel, preserved.id) is not None


def test_id_list_delete_rolls_back_receiver_rows_on_terminal_failure(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("receiver")
    message = database.create_inbox_message("sender01", "receiver", "preserve")
    engine = sqlite_database.kw["bind"]
    delete_order: list[str] = []

    def fail_terminal_delete(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if normalized.startswith("DELETE FROM INBOX"):
            delete_order.append("inbox")
        if normalized.startswith("DELETE FROM TERMINALS"):
            delete_order.append("terminals")
            raise RuntimeError("injected terminal delete failure")

    event.listen(engine, "before_cursor_execute", fail_terminal_delete)
    try:
        with pytest.raises(RuntimeError, match="injected terminal delete failure"):
            database.delete_terminals_by_ids(["receiver"])
    finally:
        event.remove(engine, "before_cursor_execute", fail_terminal_delete)

    assert delete_order == ["inbox", "terminals"]
    with sqlite_database() as connection:
        assert connection.get(database.TerminalModel, "receiver") is not None
        assert connection.get(database.InboxModel, message.id) is not None


def test_terminal_delete_rolls_back_receiver_rows_on_terminal_failure(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("receiver")
    message = database.create_inbox_message("sender01", "receiver", "preserve")
    engine = sqlite_database.kw["bind"]

    def fail_terminal_delete(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("DELETE FROM TERMINALS"):
            raise RuntimeError("injected terminal delete failure")

    event.listen(engine, "before_cursor_execute", fail_terminal_delete)
    try:
        with pytest.raises(RuntimeError, match="injected terminal delete failure"):
            database.delete_terminal("receiver")
    finally:
        event.remove(engine, "before_cursor_execute", fail_terminal_delete)

    with sqlite_database() as connection:
        assert connection.get(database.TerminalModel, "receiver") is not None
        assert connection.get(database.InboxModel, message.id) is not None


def test_receiver_creation_racing_delete_cannot_leave_orphan(
    sqlite_database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal("receiver")
    delete_locked = threading.Event()
    creator_attempted = threading.Event()
    release_delete = threading.Event()
    original_begin = database._begin_immediate

    def gated_begin(connection: Session) -> None:
        thread_name = threading.current_thread().name
        if thread_name == "creator":
            creator_attempted.set()
        original_begin(connection)
        if thread_name == "deleter":
            delete_locked.set()
            assert release_delete.wait(timeout=2)

    monkeypatch.setattr(database, "_begin_immediate", gated_begin)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="unused") as pool:
        delete_future = pool.submit(
            lambda: _run_named("deleter", database.delete_terminal, "receiver")
        )
        assert delete_locked.wait(timeout=2)
        create_future = pool.submit(
            lambda: _run_named(
                "creator",
                database.create_inbox_message,
                "sender01",
                "receiver",
                "racing",
            )
        )
        assert creator_attempted.wait(timeout=2)
        release_delete.set()
        assert delete_future.result(timeout=2) is True
        with pytest.raises(ValueError, match="not found"):
            create_future.result(timeout=2)

    assert _counts(sqlite_database) == (0, 0)


def _run_named(name: str, function, *args):
    threading.current_thread().name = name
    return function(*args)


def test_busy_timeout_exhaustion_is_bounded(
    sqlite_database: sessionmaker[Session],
) -> None:
    _terminal("receiver")
    engine = sqlite_database.kw["bind"]

    with engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(OperationalError, match="locked"):
            database.create_inbox_message("sender01", "receiver", "blocked")
        blocker.rollback()

    assert _counts(sqlite_database) == (1, 0)

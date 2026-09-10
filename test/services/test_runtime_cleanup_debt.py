"""Restart-safe automatic teardown with an absent backend and real SQLite."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend, HerdrWorkspace
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import session_service, terminal_service
from cli_agent_orchestrator.services.config_service import CleanupConfig, ConfigService
from cli_agent_orchestrator.services.runtime_resource_cleanup import build_runtime_resource_cleanup


@pytest.fixture
def teardown_world(isolated_memory_db, monkeypatch):
    backend = object.__new__(HerdrBackend)
    inventory = [
        HerdrWorkspace(
            workspace_id="ws-old",
            label="cao-old",
            agent_status="done",
            pane_count=1,
            tab_count=1,
            active_tab_id="ws-old:1",
        )
    ]
    backend.list_workspace_inventory = lambda: list(inventory)

    def close(workspace_id):
        inventory[:] = [
            workspace for workspace in inventory if workspace.workspace_id != workspace_id
        ]
        return True

    backend.close_workspace_by_id = MagicMock(side_effect=close)
    database.create_terminal("old", "cao-old", "worker", "mock")
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "old").last_active = datetime.now() - timedelta(hours=2)
        db.commit()
    monkeypatch.setattr(
        ConfigService,
        "get_config",
        lambda: SimpleNamespace(
            cleanup=CleanupConfig(
                completed_sessions_enabled=True,
                preserve_patterns=[],
            )
        ),
    )
    # Exercise persisted metadata fallback when the registry snapshot is missing.
    monkeypatch.setattr(terminal_service, "capture_terminal_snapshot", lambda _id: None)
    dismantle = MagicMock(return_value=True)
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", dismantle)
    monkeypatch.setattr(
        terminal_service,
        "delete_terminal_row",
        lambda tid, *_args, **_kw: database.delete_terminal(tid),
    )
    monkeypatch.setattr(session_service, "clear_session_env", MagicMock())
    dispatch = MagicMock()
    monkeypatch.setattr(session_service, "dispatch_plugin_event", dispatch)
    return backend, inventory, dismantle, dispatch


@pytest.mark.parametrize(
    "failure",
    ["deferred", "runtime_error", "row_error", "env_error", "close_timeout", "close_error"],
)
def test_absent_backend_debt_resumes_after_engine_restart(
    isolated_memory_db,
    monkeypatch,
    teardown_world,
    failure,
):
    backend, inventory, dismantle, dispatch = teardown_world
    if failure == "deferred":
        dismantle.return_value = False
    elif failure == "runtime_error":
        dismantle.side_effect = RuntimeError("runtime failed")
    elif failure == "row_error":
        monkeypatch.setattr(
            terminal_service,
            "delete_terminal_row",
            MagicMock(side_effect=RuntimeError("row failed")),
        )
        monkeypatch.setattr(
            session_service,
            "delete_terminals_by_ids",
            MagicMock(side_effect=RuntimeError("sweep failed")),
        )
    elif failure == "env_error":
        session_service.clear_session_env.side_effect = RuntimeError("env failed")
    else:

        def uncertain_close(_id):
            inventory.clear()
            if failure == "close_timeout":
                raise TimeoutError("close confirmation timed out")
            return False

        backend.close_workspace_by_id.side_effect = uncertain_close

    first = build_runtime_resource_cleanup(backend=backend).sweep()
    assert first.deleted == 0
    assert inventory == []
    assert len(database.list_runtime_cleanup_debt()) == 1
    assert not any(call.args[1] == "post_kill_session" for call in dispatch.call_args_list)
    if failure.startswith("close_"):
        dismantle.assert_not_called()

    # Replace the session factory and engine connection pool, then create a new
    # sweeper: no process-local retry inventory survives this boundary.
    isolated_memory_db.dispose()
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=isolated_memory_db))
    dismantle.side_effect = None
    dismantle.return_value = True
    monkeypatch.setattr(
        terminal_service,
        "delete_terminal_row",
        lambda tid, *_args, **_kw: database.delete_terminal(tid),
    )
    monkeypatch.setattr(
        session_service, "delete_terminals_by_ids", database.delete_terminals_by_ids
    )
    session_service.clear_session_env.side_effect = None

    second = build_runtime_resource_cleanup(backend=backend).sweep()
    assert second.deleted == 1
    assert database.list_terminals_by_session("cao-old") == []
    assert database.list_runtime_cleanup_debt() == []
    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == 1
    assert build_runtime_resource_cleanup(backend=backend).sweep().selected == 0
    assert backend.close_workspace_by_id.call_count == 1
    if failure not in {"env_error"}:
        assert dismantle.call_args.args[1]["provider"] == "mock"


@pytest.mark.parametrize(
    "change", ["pending", "new_terminal", "new_workspace", "renamed_workspace"]
)
def test_debt_retry_preserves_changed_identity_and_pending_delivery(
    monkeypatch, teardown_world, change
):
    backend, inventory, dismantle, dispatch = teardown_world
    dismantle.return_value = False
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 0
    if change == "pending":
        database.create_inbox_message("sender", "old", "deliver first")
    elif change == "new_terminal":
        database.create_terminal("new", "cao-old", "other", "mock")
    else:
        inventory.append(
            HerdrWorkspace(
                workspace_id="ws-new" if change == "new_workspace" else "ws-old",
                label="cao-old" if change == "new_workspace" else "cao-renamed",
                agent_status="done",
                pane_count=1,
                tab_count=1,
                active_tab_id="tab",
            )
        )
    dismantle.reset_mock()
    dismantle.return_value = True
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 0
    dismantle.assert_not_called()
    assert len(database.list_runtime_cleanup_debt()) == 1
    dispatch.assert_not_called()


def test_automatic_by_id_fallback_cascades_receiver_and_preserves_foreign_inbox(
    monkeypatch,
    teardown_world,
):
    backend, _inventory, _dismantle, dispatch = teardown_world
    database.create_terminal("live", "cao-live", "worker", "mock")
    received = database.create_inbox_message("live", "old", "already delivered")
    database.update_message_status(received.id, MessageStatus.DELIVERED)
    sent = database.create_inbox_message("old", "live", "preserve pending delivery")
    monkeypatch.setattr(
        terminal_service, "delete_terminal_row", MagicMock(side_effect=RuntimeError("row failed"))
    )

    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 1
    with database.SessionLocal() as db:
        assert db.get(database.TerminalModel, "old") is None
        assert db.get(database.InboxModel, received.id) is None
        assert db.get(database.InboxModel, sent.id) is not None
    assert [call.args[1] for call in dispatch.call_args_list] == [
        "post_kill_terminal",
        "post_kill_session",
    ]


@pytest.mark.parametrize("defer_new", [False, True])
def test_reused_label_cleans_new_identity_without_touching_pending_old_debt(
    teardown_world, defer_new
):
    backend, inventory, dismantle, dispatch = teardown_world
    dismantle.return_value = False
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 0
    message = database.create_inbox_message("sender", "old", "still pending")
    inventory.append(
        HerdrWorkspace(
            workspace_id="ws-new",
            label="cao-old",
            agent_status="done",
            pane_count=1,
            tab_count=1,
            active_tab_id="new-tab",
        )
    )
    database.create_terminal("new", "cao-old", "new-worker", "mock")
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "new").last_active = datetime.now() - timedelta(hours=2)
        db.commit()
    dismantle.reset_mock()
    dismantle.return_value = not defer_new
    cleanup = build_runtime_resource_cleanup(backend=backend)

    if defer_new:
        assert cleanup.sweep().deleted == 0
        assert {debt["workspace_id"] for debt in database.list_runtime_cleanup_debt()} == {
            "ws-old",
            "ws-new",
        }
        assert {row["id"] for row in database.list_terminals_by_session("cao-old")} == {
            "old",
            "new",
        }
        dismantle.reset_mock()
        dismantle.return_value = True
        cleanup = build_runtime_resource_cleanup(backend=backend)

    assert cleanup.sweep().deleted == 1
    assert backend.close_workspace_by_id.call_args.args == ("ws-new",)
    assert [call.args[0] for call in dismantle.call_args_list] == ["new"]
    assert [row["id"] for row in database.list_terminals_by_session("cao-old")] == ["old"]
    assert [debt["workspace_id"] for debt in database.list_runtime_cleanup_debt()] == ["ws-old"]
    assert database.get_pending_messages("old")[0].id == message.id

    # A replacement sweeper still finds old debt after new cleanup completed.
    database.update_message_status(message.id, MessageStatus.DELIVERED)
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 1
    assert database.list_runtime_cleanup_debt() == []
    assert database.list_terminals_by_session("cao-old") == []
    assert backend.close_workspace_by_id.call_count == 2  # old once, replacement once


def test_corrupt_debt_quarantines_its_label_without_stalling_unrelated_cleanup(teardown_world):
    backend, inventory, dismantle, _dispatch = teardown_world
    with database.SessionLocal() as db:
        db.add(
            database.RuntimeCleanupDebtModel(
                session_name="cao-corrupt",
                workspace_id="ws-corrupt",
                terminal_ids="not-json",
            )
        )
        db.commit()
    inventory.append(
        HerdrWorkspace(
            workspace_id="ws-reused",
            label="cao-corrupt",
            agent_status="done",
            pane_count=1,
            tab_count=1,
            active_tab_id="other-tab",
        )
    )
    database.create_terminal("reserved", "cao-corrupt", "worker", "mock")
    with database.SessionLocal() as db:
        db.get(database.TerminalModel, "reserved").last_active = datetime.now() - timedelta(hours=2)
        db.commit()

    summary = build_runtime_resource_cleanup(backend=backend).sweep()

    assert summary.deleted == 1
    assert summary.reasons["debt_inventory_invalid"] == 1
    assert [call.args[0] for call in dismantle.call_args_list] == ["old"]
    backend.close_workspace_by_id.assert_called_once_with("ws-old")
    assert database.list_terminals_by_session("cao-corrupt")[0]["id"] == "reserved"
    assert database.list_runtime_cleanup_debt()[0]["invalid"] is True
    assert [workspace.workspace_id for workspace in inventory] == ["ws-reused"]


@pytest.mark.parametrize("failure_step", [None, "copy", "rename"])
def test_cleanup_debt_schema_upgrade_preserves_old_identity_atomically(
    isolated_memory_db,
    monkeypatch,
    failure_step,
):
    monkeypatch.setattr(database, "engine", isolated_memory_db)
    with isolated_memory_db.begin() as connection:
        connection.exec_driver_sql("DROP TABLE runtime_cleanup_debt")
        connection.exec_driver_sql(
            "CREATE TABLE runtime_cleanup_debt (session_name VARCHAR PRIMARY KEY, "
            "workspace_id VARCHAR NOT NULL, terminal_ids TEXT NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO runtime_cleanup_debt VALUES (?, ?, ?, ?)",
            ("cao-old", "ws-old", '["old"]', "2026-09-09 00:00:00"),
        )

    def reject_copy(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            failure_step == "copy" and statement.startswith("INSERT INTO runtime_cleanup_debt_v2")
        ) or (
            failure_step == "rename" and statement.startswith("ALTER TABLE runtime_cleanup_debt_v2")
        ):
            raise RuntimeError("injected migration failure")

    if failure_step:
        event.listen(isolated_memory_db, "before_cursor_execute", reject_copy)
        try:
            with pytest.raises(RuntimeError, match="injected migration failure"):
                database._migrate_runtime_cleanup_debt()
        finally:
            event.remove(isolated_memory_db, "before_cursor_execute", reject_copy)
        with isolated_memory_db.connect() as connection:
            assert connection.exec_driver_sql("SELECT * FROM runtime_cleanup_debt").fetchall() == [
                ("cao-old", "ws-old", '["old"]', "2026-09-09 00:00:00")
            ]
            assert not connection.exec_driver_sql(
                "PRAGMA table_info(runtime_cleanup_debt_v2)"
            ).fetchall()

    database._migrate_runtime_cleanup_debt()
    database._migrate_runtime_cleanup_debt()  # idempotent after restart
    database.retain_runtime_cleanup_debt("cao-old", "ws-new", ["new"])
    debts = database.list_runtime_cleanup_debt()
    assert {(row["workspace_id"], tuple(row["terminal_ids"])) for row in debts} == {
        ("ws-old", ("old",)),
        ("ws-new", ("new",)),
    }
    with pytest.raises(RuntimeError, match="another cleanup identity"):
        database.retain_runtime_cleanup_debt("cao-old", "ws-third", ["old"])
    database.clear_runtime_cleanup_debt("cao-old", "ws-new")
    assert database.list_runtime_cleanup_debt()[0]["workspace_id"] == "ws-old"


def test_current_cleanup_schema_does_not_reserve_writer_lock(isolated_memory_db, monkeypatch):
    monkeypatch.setattr(database, "engine", isolated_memory_db)
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with isolated_memory_db.connect() as writer:
        writer.exec_driver_sql("BEGIN IMMEDIATE")
        event.listen(isolated_memory_db, "before_cursor_execute", record_statement)
        try:
            database._migrate_runtime_cleanup_debt()
        finally:
            event.remove(isolated_memory_db, "before_cursor_execute", record_statement)
            writer.rollback()

    assert statements == ["PRAGMA table_info(runtime_cleanup_debt)"]


def test_cleanup_schema_rechecks_after_another_migrator_wins(isolated_memory_db, monkeypatch):
    monkeypatch.setattr(database, "engine", isolated_memory_db)
    with isolated_memory_db.begin() as connection:
        connection.exec_driver_sql("DROP TABLE runtime_cleanup_debt")
        connection.exec_driver_sql(
            "CREATE TABLE runtime_cleanup_debt (session_name VARCHAR PRIMARY KEY, "
            "workspace_id VARCHAR NOT NULL, terminal_ids TEXT NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO runtime_cleanup_debt VALUES (?, ?, ?, ?)",
            ("cao-old", "ws-old", '["old"]', "2026-09-09 00:00:00"),
        )
    raced = False
    creates = []

    def migrate_before_reservation(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal raced
        if statement == "BEGIN IMMEDIATE" and not raced:
            raced = True
            database._migrate_runtime_cleanup_debt()
        if statement.startswith("CREATE TABLE runtime_cleanup_debt_v2"):
            creates.append(statement)

    event.listen(isolated_memory_db, "before_cursor_execute", migrate_before_reservation)
    try:
        database._migrate_runtime_cleanup_debt()
    finally:
        event.remove(isolated_memory_db, "before_cursor_execute", migrate_before_reservation)

    assert raced
    assert len(creates) == 1
    assert database.list_runtime_cleanup_debt()[0]["terminal_ids"] == ["old"]


@pytest.mark.parametrize("clear_fails", [False, True])
def test_manual_completion_retires_only_its_captured_automatic_debt(
    monkeypatch,
    teardown_world,
    clear_fails,
):
    backend, inventory, dismantle, dispatch = teardown_world
    dismantle.return_value = False
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 0
    assert inventory == []
    # This unrelated same-label debt has no rows in the manual worklist and
    # still refers to a live identity under a different label.
    database.retain_runtime_cleanup_debt("cao-old", "ws-other", ["other"])
    database.create_terminal("other", "cao-renamed", "other-worker", "mock")
    inventory.append(
        HerdrWorkspace(
            workspace_id="ws-other",
            label="cao-renamed",
            agent_status="done",
            pane_count=1,
            tab_count=1,
            active_tab_id="other-tab",
        )
    )
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)
    backend.session_exists_strict = lambda _label: False
    dismantle.return_value = True
    if clear_fails:
        monkeypatch.setattr(
            session_service,
            "clear_runtime_cleanup_debt",
            MagicMock(side_effect=RuntimeError("database is locked")),
        )

    result = session_service.delete_session("cao-old")

    assert result["deleted"] == ([] if clear_fails else ["cao-old"])
    assert database.list_terminals_by_session("cao-old") == []
    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == (
        0 if clear_fails else 1
    )
    assert {debt["workspace_id"] for debt in database.list_runtime_cleanup_debt()} == (
        {"ws-old", "ws-other"} if clear_fails else {"ws-other"}
    )
    monkeypatch.setattr(
        session_service, "clear_runtime_cleanup_debt", database.clear_runtime_cleanup_debt
    )

    build_runtime_resource_cleanup(backend=backend).sweep()

    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == 1
    assert {debt["workspace_id"] for debt in database.list_runtime_cleanup_debt()} == {"ws-other"}
    assert [workspace.workspace_id for workspace in inventory] == ["ws-other"]
    assert database.get_terminal_metadata("other") is not None


@pytest.mark.parametrize("automatic_failure", ["env", "debt_clear"])
def test_manual_retry_reconciles_debt_after_all_terminal_rows_are_gone(
    monkeypatch,
    teardown_world,
    automatic_failure,
):
    backend, _inventory, _dismantle, dispatch = teardown_world
    if automatic_failure == "env":
        session_service.clear_session_env.side_effect = RuntimeError("env failed")
    else:
        monkeypatch.setattr(
            session_service,
            "clear_runtime_cleanup_debt",
            MagicMock(side_effect=RuntimeError("debt failed")),
        )
    assert build_runtime_resource_cleanup(backend=backend).sweep().deleted == 0
    assert database.list_terminals_by_session("cao-old") == []
    assert len(database.list_runtime_cleanup_debt()) == 1
    assert not any(call.args[1] == "post_kill_session" for call in dispatch.call_args_list)
    session_service.clear_session_env.side_effect = None
    monkeypatch.setattr(
        session_service, "clear_runtime_cleanup_debt", database.clear_runtime_cleanup_debt
    )
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)
    backend.session_exists_strict = lambda _label: False

    assert session_service.delete_session("cao-old")["deleted"] == ["cao-old"]
    assert database.list_runtime_cleanup_debt() == []
    assert build_runtime_resource_cleanup(backend=backend).sweep().selected == 0
    assert sum(call.args[1] == "post_kill_session" for call in dispatch.call_args_list) == 1


@pytest.mark.parametrize("identity_changes", [False, True])
def test_manual_cleanup_uses_current_inventory_while_retaining_corrupt_debt(
    monkeypatch,
    teardown_world,
    identity_changes,
):
    backend, inventory, dismantle, dispatch = teardown_world
    with database.SessionLocal() as db:
        db.add(
            database.RuntimeCleanupDebtModel(
                session_name="cao-old",
                workspace_id="ws-unknown",
                terminal_ids="corrupt-private-placeholder",
            )
        )
        db.commit()
    database.create_terminal("unrelated", "cao-other", "worker", "mock")
    inventory.append(
        HerdrWorkspace(
            workspace_id="ws-other",
            label="cao-other",
            agent_status="working",
            pane_count=1,
            tab_count=1,
            active_tab_id="other-tab",
        )
    )
    monkeypatch.setattr(session_service, "get_backend", lambda: backend)

    def capture(terminal_id):
        if identity_changes:
            inventory[0] = HerdrWorkspace(
                workspace_id="ws-replacement",
                label="cao-old",
                agent_status="done",
                pane_count=1,
                tab_count=1,
                active_tab_id="replacement-tab",
            )
        return database.get_terminal_metadata(terminal_id)

    monkeypatch.setattr(terminal_service, "capture_terminal_snapshot", capture)

    if identity_changes:
        with pytest.raises(RuntimeError, match="backend identity changed") as raised:
            session_service.delete_session("cao-old")
        assert "corrupt-private-placeholder" not in str(raised.value)
    else:
        result = session_service.delete_session("cao-old")
        assert result["deleted"] == []  # unresolved captured debt remains explicit
        assert any(
            error.get("step") == "quarantined_runtime_cleanup_debt" for error in result["errors"]
        )
        assert "corrupt-private-placeholder" not in str(result)
    assert database.list_runtime_cleanup_debt()[0]["invalid"] is True
    assert database.get_terminal_metadata("unrelated") is not None
    assert "ws-other" in {workspace.workspace_id for workspace in inventory}
    assert not any(call.args[1] == "post_kill_session" for call in dispatch.call_args_list)
    if identity_changes:
        backend.close_workspace_by_id.assert_not_called()
        dismantle.assert_not_called()
        assert database.get_terminal_metadata("old") is not None
        assert "ws-replacement" in {workspace.workspace_id for workspace in inventory}
    else:
        backend.close_workspace_by_id.assert_called_once_with("ws-old")
        dismantle.assert_called_once()
        assert database.get_terminal_metadata("old") is None
        assert [workspace.workspace_id for workspace in inventory] == ["ws-other"]
        assert [call.args[1] for call in dispatch.call_args_list] == ["post_kill_terminal"]

"""Resolution writers serialize before taking the shared audit-chain lock."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import event as sa_event, func, select, text
from sqlalchemy.dialects import postgresql

from test_add_backend import db as db, connector_fixture, make_writable
from test_attendance_force_postgres import (
    force_pg as force_pg,
    postgres_store as postgres_store,
    repair_store as repair_store,
    store as store,
)
from test_resolved_identity_snapshot import approve_alias_group, previously_blocked_approval, snapshot
from zk_add import attendance_direct_ords as direct
from zk_add.identity_conflicts import revoke_identity_resolution, valid_identity_resolutions
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision,
    AttendanceRecoveryItem,
    AuditChainHead,
    AuditEvent,
    Connector,
    IdentityConflictResolution,
    OrdsOutbox,
)


@pytest.mark.parametrize("operation", ["readonly", "stale", "revoke"])
def test_resolution_writers_lock_before_audit_but_readonly_checks_do_not(db, operation):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot(db, connector)
    resolution = approve_alias_group(db, connector)
    db.flush()
    queries = []

    def record_query(state):
        if state.is_select:
            queries.append(str(state.statement.compile(dialect=postgresql.dialect())))

    sa_event.listen(db, "do_orm_execute", record_query)
    try:
        if operation == "revoke":
            revoke_identity_resolution(
                db, zkt=connector.zkt_device, resolution_id=resolution.resolution_id,
                reason="Reviewed revocation", actor="operator",
            )
        else:
            assert valid_identity_resolutions(
                db, zkt=connector.zkt_device, groups={}, mark_stale=operation == "stale",
            ) == {}
    finally:
        sa_event.remove(db, "do_orm_execute", record_query)

    resolution_index = next(i for i, sql in enumerate(queries)
                            if "FROM add_identity_conflict_resolutions" in sql)
    locked_query = queries[resolution_index]
    if operation == "readonly":
        assert "FOR UPDATE" not in locked_query
        assert resolution.status == "ACTIVE"
        assert not any("FROM add_audit_chain_head" in sql for sql in queries)
    else:
        assert "FOR UPDATE" in locked_query
        audit_index = next(i for i, sql in enumerate(queries) if "FROM add_audit_chain_head" in sql)
        assert resolution_index < audit_index
        if operation == "stale":
            assert "ORDER BY add_identity_conflict_resolutions.id" in locked_query
        assert resolution.status == ("STALE" if operation == "stale" else "REVOKED")


def wait_for_database_lock(engine, backend_pid, future):
    deadline = monotonic() + 3
    pause = Event()
    while monotonic() < deadline:
        if future.done():
            future.result()
            raise AssertionError("The competing writer did not wait for the resolution lock")
        # A fresh transaction avoids caching an earlier pg_stat_activity snapshot.
        with engine.connect() as connection:
            waiting = connection.scalar(text(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"
            ), {"pid": backend_pid})
        if waiting == "Lock":
            return
        pause.wait(0.01)
    raise AssertionError("The competing writer never reached its database lock")


@pytest.mark.parametrize("first_writer", ["restore", "revoke"])
def test_postgres_resolution_recovery_and_revocation_share_safe_lock_order(force_pg, first_writer):
    sessions, _connector_id, _event_uid = force_pg
    with sessions() as session:
        connector, _user, resolution, row, decision, _item, _job, _outbox = (
            previously_blocked_approval(session)
        )
        connector_id, resolution_id = connector.id, resolution.id
        resolution_ref, event_id, decision_id = resolution.resolution_id, row.id, decision.id
        original_proof, original_payload = deepcopy(decision.proof), decision.payload_encrypted
        # An otherwise identical held punch has no administrator decision.
        unapproved = AttendanceEvent(**{
            **{column.name: deepcopy(getattr(row, column.name))
               for column in AttendanceEvent.__table__.columns if column.name != "id"},
            "event_uid": "d" * 64,
        })
        session.add(unapproved)
        session.flush()
        unapproved_id = unapproved.id
        session.add(OrdsOutbox(attendance_event_id=unapproved_id, status="BLOCKED_IDENTITY"))
        session.commit()

    backend_pids = Queue()

    def second_writer():
        with sessions() as session:
            backend_pids.put(session.scalar(text("SELECT pg_backend_pid()")))
            connector = session.get(Connector, connector_id)
            if first_writer == "restore":
                revoke_identity_resolution(
                    session, zkt=connector.zkt_device, resolution_id=resolution_ref,
                    reason="Reviewed concurrent revocation", actor="operator",
                )
            else:
                snapshot(session, connector)
            session.commit()

    with sessions() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        connector = holder.get(Connector, connector_id)
        holder.get(IdentityConflictResolution, resolution_id, with_for_update=True)
        future = pool.submit(second_writer)
        try:
            wait_for_database_lock(sessions.kw["bind"], backend_pids.get(timeout=3), future)
            holder.execute(text("SET LOCAL lock_timeout = '500ms'"))
            # Before the fix, revoke held this head while waiting for our resolution.
            assert holder.scalar(select(AuditChainHead).with_for_update()) is not None
            if first_writer == "restore":
                snapshot(holder, connector)
            else:
                revoke_identity_resolution(
                    holder, zkt=connector.zkt_device, resolution_id=resolution_ref,
                    reason="Reviewed concurrent revocation", actor="operator",
                )
            holder.commit()
            future.result(timeout=10)
        finally:
            holder.rollback()

    with sessions() as session:
        connector = session.get(Connector, connector_id)
        row = session.get(AttendanceEvent, event_id)
        decision = session.get(AttendanceForceReleaseDecision, decision_id)
        outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event_id))
        assert session.get(IdentityConflictResolution, resolution_id).status == "REVOKED"
        assert row.ords_status == outbox.status == (
            "PENDING" if first_writer == "restore" else "BLOCKED_IDENTITY"
        )
        assert decision.proof == original_proof and decision.payload_encrypted == original_payload
        assert direct.approved_payload(session, row, connector, decision) is not None
        assert session.get(AttendanceRecoveryItem, decision.item_id).result["direct_post_attempts"] == 1
        assert outbox.attempt_count == 3 and row.oracle_confirmed_at is None
        assert session.get(AttendanceEvent, unapproved_id).ords_status == "BLOCKED_IDENTITY"
        assert session.scalar(select(OrdsOutbox.status).where(
            OrdsOutbox.attendance_event_id == unapproved_id,
        )) == "BLOCKED_IDENTITY"
        assert session.scalar(select(AttendanceForceReleaseDecision.id).where(
            AttendanceForceReleaseDecision.attendance_event_id == unapproved_id,
        )) is None
        assert session.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "ATTENDANCE_DIRECT_ORDS_APPROVAL_REVALIDATED",
            AuditEvent.target_id == str(event_id),
        )) == int(first_writer == "restore")

"""Manual-send safety checks against the installed PostgreSQL release guards."""

from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from test_attendance_force_postgres import force_pg as force_pg
from test_attendance_repair import repair_store as repair_store
from test_safe_attendance_repair import store as store
from test_safe_attendance_repair_postgres import postgres_store as postgres_store
from zk_add import attendance_direct_ords as direct
from zk_add import attendance_release_safety as safety
from zk_add import attendance_force_delivery as delivery
from zk_add import worker
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem as Item,
    Connector,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.time_utils import utc_now

GUARDS = (
    "20260925_0036_auto_synced_cnic.py",
    "20260929_0037_reconciled_cnic_guard.py",
    "20260929_0038_saved_reconcile_cnic_guard.py",
    "20260929_0039_delayed_record_uid_guard.py",
    "20260929_0041_legacy_reconcile_cnic_guard.py",
)


def apply(engine, filename):
    path = Path(__file__).parents[2] / "apps/add_backend/migrations/versions" / filename
    spec = spec_from_file_location(filename.removesuffix(".py"), path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()


def start(event_id, key, *, send_now=False):
    return DirectOrdsStartRequest(
        event_ids=[event_id], reason="Administrator reviewed saved punch",
        password="test-password", idempotency_key=f"release-safety-pg-{key}",
        send_now_event_ids=[event_id] if send_now else [],
    )


@pytest.fixture()
def dump_row(force_pg):
    """A held DUMP_RECONNECT punch with a saved 40-byte record and retained interval."""
    sessions, _connector_id, _uid = force_pg
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        connector = db.get(Connector, event.connector_id)
        zkt = connector.zkt_device
        user = db.get(DeviceUser, event.device_user_id)
        when = utc_now() - timedelta(minutes=20)
        event.source = "DUMP_RECONNECT"
        event.device_event_time = when
        event.captured_at = when + timedelta(minutes=15)
        event.received_at = event.captured_at + timedelta(seconds=1)
        event.identity_terminal_fingerprint = "a" * 64
        event.identity_snapshot_id = zkt.identity_snapshot_id
        event.identity_resolution_status = event.ords_status = "BLOCKED_IDENTITY"
        event.manual_release_required = True
        event.cnic_encrypted = event.cnic_lookup_hash = event.captured_cnic_lookup_hash = None
        event.raw_event = {"attendance_record_uid": "28943"}
        db.add(AttendanceIdentityHistory(
            zkt_device_id=zkt.id, device_user_id=user.id, terminal_serial=zkt.serial,
            user_id=user.user_id, uid=user.uid, fingerprint="a" * 64,
            cnic_encrypted=user.cnic_encrypted, cnic_lookup_hash=user.cnic_lookup_hash,
            first_snapshot_id=zkt.identity_snapshot_id, last_snapshot_id=zkt.identity_snapshot_id,
            last_revision=zkt.identity_snapshot_revision,
            observed_from=when - timedelta(minutes=1), observed_until=when + timedelta(minutes=1),
        ))
        db.add(TerminalRecordManifest(
            connector_id=connector.id, zkt_device_id=zkt.id, terminal_serial=zkt.serial,
            generation=1, ordinal=0, canonical_source=True, record_size=40,
            raw_record_digest="b" * 64, terminal_record_key="c" * 64,
            attendance_event_id=event.id, disposition="BLOCKED_IDENTITY",
            observed_uid="28943", observed_user_id=user.user_id,
        ))
        db.commit()
        event_id = event.id
    engine = sessions.kw["bind"]
    for filename in GUARDS:
        apply(engine, filename)
    return sessions, engine, event_id


def test_preview_reports_installed_guard_without_changing_the_row(dump_row):
    from zk_add.web import attendance_identity_evidence_report

    sessions, engine, event_id = dump_row
    with sessions() as db:
        # Before 0053 the guard admits only CURRENT_RECONCILE: no automatic release.
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["database_guard"] == "REJECTED"
        assert row["auto_release_at_next_snapshot"] is False
        report = attendance_identity_evidence_report(event_id, auth=(db, None))
        assert report["auto_release_eligible"] is True
        assert report["database_guard"] == "REJECTED"
        assert "DATABASE_GUARD_REJECTS" in [hint["code"] for hint in report["hints"]]
        assert db.get(AttendanceEvent, event_id).ords_status == "BLOCKED_IDENTITY"
        db.rollback()

    apply(engine, "20261012_0053_reconciled_record_uid_sources.py")
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["database_guard"] == "ACCEPTED"
        assert row["auto_release_at_next_snapshot"] is True
        report = attendance_identity_evidence_report(event_id, auth=(db, None))
        assert report["database_guard"] == "ACCEPTED"
        assert report["saved_terminal_record"] is True
        assert "AUTO_RELEASE_AT_NEXT_SNAPSHOT" in [hint["code"] for hint in report["hints"]]
        # The probe is a SELECT inside a savepoint: nothing about the row changed.
        assert db.scalar(text(
            "SELECT ords_status || ':' || manual_release_required FROM add_attendance_events WHERE id=:id"
        ), {"id": event_id}) == "BLOCKED_IDENTITY:true"


def test_unavailable_guard_probe_leaves_the_transaction_usable(dump_row, monkeypatch):
    from zk_add import attendance_auto_release_guard

    sessions, _engine, event_id = dump_row

    def broken(session, **_kwargs):
        session.execute(text("SELECT add_function_that_does_not_exist()"))

    monkeypatch.setattr(attendance_auto_release_guard, "database_synced_cnic_release_proven", broken)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        zkt = db.get(Connector, event.connector_id).zkt_device
        user = db.get(DeviceUser, event.device_user_id)
        assert safety.database_guard_state(db, event=event, zkt=zkt, user=user) == "UNAVAILABLE"
        assert db.scalar(select(AttendanceEvent.ords_status).where(AttendanceEvent.id == event_id)) == (
            "BLOCKED_IDENTITY"
        )


def test_send_now_override_survives_postgres_delivery_guards(dump_row):
    sessions, engine, event_id = dump_row
    apply(engine, "20261012_0053_reconciled_record_uid_sources.py")
    with sessions() as db:
        direct.create(db, actor="operator", request=start(event_id, "wait"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "AUTO_RELEASE_PENDING"
        assert db.scalar(select(Decision)) is None
    with sessions() as db:
        direct.create(db, actor="operator", request=start(event_id, "now", send_now=True))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        decision = db.scalar(select(Decision))
        assert decision.proof["auto_release_override"] is True
        assert decision.proof["cnic_source"] == "SYNCED_USER"
        assert db.scalar(select(OrdsOutbox)).status == "PENDING"


def test_invalid_clock_approval_is_quarantined_under_postgres_guards(dump_row):
    sessions, _engine, event_id = dump_row
    with sessions() as db:
        direct.create(db, actor="operator", request=start(event_id, "clock"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
        assert db.scalar(select(Decision)) is not None
    with engine_begin(sessions) as conn:
        conn.execute(text("UPDATE add_attendance_events SET clock_quality='INVALID' WHERE id=:id"),
                     {"id": event_id})
    normal, forced = delivery.split_claims(worker.claim_ords_batch(5))
    assert normal == [] and forced == []
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.ords_status == db.scalar(select(OrdsOutbox)).status == "QUARANTINED_INVALID_DEVICE_TIME"
        assert event.oracle_confirmed_at is None


def engine_begin(sessions):
    return sessions.kw["bind"].begin()

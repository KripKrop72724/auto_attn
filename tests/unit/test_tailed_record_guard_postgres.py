"""PostgreSQL: 0060 admits a 49-byte record's UID exactly as 0056 admits a 40-byte one.

LHRCENTRAL's MB40-VL/ID terminals (platform ZAM180_TFT) return the 40-byte
record plus a 9-byte tail. Their first two bytes are the attendance record UID,
never the enrolled UID. A full-history punch with a retained, fingerprint-bound
interval is proven by the application; the installed guard must agree.
"""

from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from test_attendance_force_postgres import force_pg as force_pg
from test_attendance_repair import repair_store as repair_store
from test_safe_attendance_repair import store as store
from test_safe_attendance_repair_postgres import postgres_store as postgres_store
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    Connector,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.service import release_synced_cnic_attendance
from zk_add.time_utils import utc_now

VERSIONS = Path(__file__).parents[2] / "apps/add_backend/migrations/versions"
GUARDS = (
    "20260925_0036_auto_synced_cnic.py",
    "20260929_0037_reconciled_cnic_guard.py",
    "20260929_0038_saved_reconcile_cnic_guard.py",
    "20260929_0039_delayed_record_uid_guard.py",
    "20260929_0041_legacy_reconcile_cnic_guard.py",
    "20261012_0053_reconciled_record_uid_sources.py",
    "20261022_0056_identity_dump_auto_release.py",
)
TAILED_GUARD = "20261104_0060_tailed_record_uid_guard.py"


def _apply(engine, filename):
    spec = spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()


def _held_full_history_punch(sessions, record_size):
    """A held FULL_HISTORY punch whose saved record carries a record UID, not the enrolled UID."""
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        zkt = db.get(Connector, event.connector_id).zkt_device
        user = db.get(DeviceUser, event.device_user_id)
        when = utc_now() - timedelta(minutes=20)
        event.source = "FULL_HISTORY"
        event.device_event_time = when
        event.captured_at = when + timedelta(minutes=15)
        event.received_at = event.captured_at + timedelta(seconds=1)
        event.identity_terminal_fingerprint = "a" * 64
        event.identity_snapshot_id = zkt.identity_snapshot_id
        event.identity_resolution_status = event.ords_status = "BLOCKED_IDENTITY"
        event.manual_release_required = True
        event.cnic_encrypted = event.cnic_lookup_hash = event.captured_cnic_lookup_hash = None
        event.raw_event = {"attendance_record_uid": "1322"}
        db.add(AttendanceIdentityHistory(
            zkt_device_id=zkt.id, device_user_id=user.id, terminal_serial=zkt.serial,
            user_id=user.user_id, uid=user.uid, fingerprint="a" * 64,
            cnic_encrypted=user.cnic_encrypted, cnic_lookup_hash=user.cnic_lookup_hash,
            first_snapshot_id=zkt.identity_snapshot_id, last_snapshot_id=zkt.identity_snapshot_id,
            last_revision=zkt.identity_snapshot_revision,
            observed_from=when - timedelta(minutes=1), observed_until=when + timedelta(minutes=1),
        ))
        db.add(TerminalRecordManifest(
            connector_id=event.connector_id, zkt_device_id=zkt.id, terminal_serial=zkt.serial,
            generation=1, ordinal=0, canonical_source=True, record_size=record_size,
            raw_record_digest="b" * 64, terminal_record_key="c" * 64,
            attendance_event_id=event.id, disposition="BLOCKED_IDENTITY",
            observed_uid="1322", observed_user_id=user.user_id,
        ))
        db.commit()
        return event.id


def _guard_admits(sessions, event_id):
    with sessions() as db:
        return db.scalar(text(
            "SELECT add_auto_reconciled_cnic_verified("
            "jsonb_populate_record(NULL::add_attendance_events, "
            "to_jsonb(e) || jsonb_build_object("
            "'identity_resolution_status','RESOLVED_SYNCED_CNIC',"
            "'identity_repair_reason','VERIFIED_SYNCED_CNIC',"
            "'cnic_lookup_hash',u.cnic_lookup_hash,"
            "'cnic_encrypted',u.cnic_encrypted))) "
            "FROM add_attendance_events e JOIN add_device_users u "
            "ON u.id=e.device_user_id WHERE e.id=:id"
        ), {"id": event_id})


def _release(sessions, event_id):
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        zkt = db.get(Connector, event.connector_id).zkt_device
        released = release_synced_cnic_attendance(
            db, zkt=zkt, user=db.get(DeviceUser, event.device_user_id))
        db.commit()
        return released


def test_49_byte_record_uid_is_released_only_once_0060_is_installed(force_pg):
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    event_id = _held_full_history_punch(sessions, 49)
    for filename in GUARDS:
        _apply(engine, filename)
    # The application proves it; 0056 still holds it rather than failing ingestion.
    assert _guard_admits(sessions, event_id) is False
    assert _release(sessions, event_id) == 0

    _apply(engine, TAILED_GUARD)
    assert _guard_admits(sessions, event_id) is True
    assert _release(sessions, event_id) == 1
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.ords_status == "PENDING" and not event.manual_release_required
        assert db.scalar(select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.attendance_event_id == event_id)) == 1
        # The retained interval stays required: revoking it fails the guard again.
        db.scalar(select(AttendanceIdentityHistory).where(
            AttendanceIdentityHistory.device_user_id == event.device_user_id,
            AttendanceIdentityHistory.observed_from <= event.device_event_time,
            AttendanceIdentityHistory.observed_until >= event.device_event_time,
        )).revoked = True
        db.commit()
    with sessions() as db:
        assert db.scalar(text(
            "SELECT add_auto_reconciled_cnic_verified(e) FROM add_attendance_events e WHERE id=:id"
        ), {"id": event_id}) is False


@pytest.mark.parametrize("record_size,admitted", [(40, True), (16, False), (8, False)])
def test_0060_leaves_other_record_sizes_as_0056_decided(force_pg, record_size, admitted):
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    event_id = _held_full_history_punch(sessions, record_size)
    for filename in GUARDS:
        _apply(engine, filename)
    assert _guard_admits(sessions, event_id) is admitted
    _apply(engine, TAILED_GUARD)
    assert _guard_admits(sessions, event_id) is admitted

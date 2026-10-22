"""PostgreSQL: the 0056 hold guard admits a dump row only with its retained interval."""

import hashlib
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from test_attendance_force_postgres import force_pg as force_pg
from test_attendance_repair import repair_store as repair_store
from test_safe_attendance_repair import store as store
from test_safe_attendance_repair_postgres import postgres_store as postgres_store
from zk_add import attendance_identity_backlog as backlog
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    AttendanceRecoveryJob,
    AuditEvent,
    Connector,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.reconciliation import _release_source_synced_attendance
from zk_add.schemas import AttendanceEventIn, UserSnapshotRequest, UserSnapshotRow
from zk_add.service import (
    DumpIdentityRelease,
    ingest_attendance,
    release_synced_cnic_attendance,
    replace_user_snapshot,
    synced_cnic_identity_proven,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now

VERSIONS = Path(__file__).parents[2] / "apps/add_backend/migrations/versions"
EARLIER_GUARDS = (
    "20260925_0036_auto_synced_cnic.py",
    "20260929_0037_reconciled_cnic_guard.py",
    "20260929_0038_saved_reconcile_cnic_guard.py",
    "20260929_0039_delayed_record_uid_guard.py",
    "20260929_0040_manual_hold_event_diagnostic.py",
    "20260929_0041_legacy_reconcile_cnic_guard.py",
    "20261012_0053_reconciled_record_uid_sources.py",
)
DUMP_GUARD = "20261022_0056_identity_dump_auto_release.py"
FP = "a" * 64
ADMIN = "StateHealthAdmin"


def _approve_whole_zone(db, zone, key, *, approved=None):
    """The owner's check, page by page, then an approval of every group."""
    first = backlog.preview_page(db, zone_id=zone, actor=ADMIN)
    page, groups = first, {}
    while True:
        for group in page["groups"]:
            entry = groups.setdefault(
                (group["connector_id"], group["source"], group["age"]), [0, 0]
            )
            entry[0] += group["held"]
            entry[1] += group["provable"]
        if page["next_after_id"] is None:
            break
        page = backlog.preview_page(
            db, zone_id=zone, actor=ADMIN, after_id=page["next_after_id"],
            as_of=first["as_of"], until_id=first["until_id"],
            window_hours=first["window_hours"],
        )
    approval = first["approval"]
    return groups, backlog.create_release(
        db, actor=ADMIN, zone_id=zone, reason="Owner backlog release",
        typed_confirmation=f"RELEASE {zone}", idempotency_key=key,
        scope=approval["scope"], expires_at=datetime.fromisoformat(approval["expires_at"]),
        signature=approval["signature"],
        groups=[
            {"connector_id": connector_id, "source": source, "age": age}
            for connector_id, source, age in groups
        ],
        approved_count=approved or max(1, sum(entry[1] for entry in groups.values())),
        previewed_held=max(1, sum(entry[0] for entry in groups.values())),
    )


def _apply(engine, filename):
    spec = spec_from_file_location(filename.removesuffix(".py"), VERSIONS / filename)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()


def _proposed(conn_or_db, event_id, user_id):
    """The guard's verdict on the release the application would write."""
    return conn_or_db.execute(text("""
        SELECT add_auto_reconciled_cnic_verified(jsonb_populate_record(
          NULL::add_attendance_events,
          to_jsonb(e) || jsonb_build_object(
            'device_user_id', u.id,
            'identity_snapshot_id', z.identity_snapshot_id,
            'identity_resolution_status', 'RESOLVED_SYNCED_CNIC',
            'identity_repair_reason', 'VERIFIED_SYNCED_CNIC',
            'cnic_lookup_hash', u.cnic_lookup_hash,
            'cnic_encrypted', u.cnic_encrypted)))
        FROM add_attendance_events e
        JOIN add_device_users u ON u.id = :user_id
        JOIN add_zkt_devices z ON z.id = e.zkt_device_id
        WHERE e.id = :event_id
    """), {"event_id": event_id, "user_id": user_id}).scalar()


def _held_dump(sessions):
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        connector = db.get(Connector, event.connector_id)
        zkt = connector.zkt_device
        user = db.get(DeviceUser, event.device_user_id)
        when = utc_now() - timedelta(minutes=30)
        event.source = "DUMP_RECONNECT"
        event.device_event_time = when
        # Read from the terminal long after the punch: only the batch and the
        # retained interval can prove who punched.
        event.captured_at = when + timedelta(minutes=25)
        event.received_at = event.captured_at + timedelta(seconds=2)
        event.identity_terminal_fingerprint = FP
        event.device_user_id = None
        event.identity_snapshot_id = None
        event.identity_resolution_status = "BLOCKED_PROVENANCE"
        event.ords_status = "BLOCKED_IDENTITY"
        event.manual_release_required = True
        event.cnic_encrypted = event.cnic_lookup_hash = event.cnic_last4 = None
        event.captured_cnic_lookup_hash = None
        event.raw_event = {"attendance_record_uid": "28943"}
        user.terminal_identity_fingerprint = FP
        db.add(AttendanceIdentityHistory(
            zkt_device_id=zkt.id, device_user_id=user.id,
            terminal_serial=zkt.serial, user_id=user.user_id, uid=user.uid,
            fingerprint=FP, cnic_encrypted=user.cnic_encrypted,
            cnic_lookup_hash=user.cnic_lookup_hash,
            first_snapshot_id=zkt.identity_snapshot_id,
            last_snapshot_id=zkt.identity_snapshot_id,
            last_revision=zkt.identity_snapshot_revision,
            observed_from=when - timedelta(minutes=10),
            observed_until=when + timedelta(minutes=10),
        ))
        # Whole-roster continuity also spans the punch; it must not matter.
        zkt.last_identity_change_at = when - timedelta(hours=1)
        db.commit()
        return event.id, user.id


BREAKERS = {
    "revoked": "UPDATE add_attendance_identity_history SET revoked = true",
    "interval_gap": "UPDATE add_attendance_identity_history "
                    "SET observed_until = observed_from + INTERVAL '1 minute'",
    "fingerprint": f"UPDATE add_attendance_identity_history SET fingerprint = '{'b' * 64}'",
    "uid": "UPDATE add_attendance_identity_history SET uid = '99'",
    "cnic_changed": f"UPDATE add_attendance_identity_history SET cnic_lookup_hash = '{'0' * 64}'",
    # The interval covering the punch belongs to an earlier device user record.
    "recreated_user": """
        WITH former AS (
          INSERT INTO add_device_users (
            user_key, zkt_device_id, uid, user_id, display_name,
            terminal_identity_fingerprint, cnic_encrypted, cnic_lookup_hash, cnic_last4,
            shift_worker, privilege, present, lifecycle_state, source, row_version,
            observed_at, created_at, updated_at)
          SELECT 'former', zkt_device_id, uid, user_id, 'Former',
            terminal_identity_fingerprint, cnic_encrypted, cnic_lookup_hash, cnic_last4,
            shift_worker, privilege, false, 'DELETED', source, 1,
            observed_at, created_at, updated_at
          FROM add_device_users LIMIT 1
          RETURNING id)
        UPDATE add_attendance_identity_history SET device_user_id = (SELECT id FROM former)
    """,
    # Whole-roster continuity alone never admits a dump row.
    "legacy_continuity_only": "DELETE FROM add_attendance_identity_history",
    # Captured within ten minutes of the punch, with only whole-roster continuity.
    "near_capture_legacy_only": """
        UPDATE add_attendance_events
          SET captured_at = device_event_time + INTERVAL '5 minutes',
              received_at = device_event_time + INTERVAL '5 minutes 2 seconds';
        DELETE FROM add_attendance_identity_history;
    """,
    "receipt_lag": "UPDATE add_attendance_events "
                   "SET received_at = captured_at + INTERVAL '11 minutes'",
    # The third case is for legacy dump rows only: another terminal reconcile
    # source without a saved record never passes on its interval alone.
    "full_history_source": "UPDATE add_attendance_events SET source = 'FULL_HISTORY'",
    "current_reconcile_source": "UPDATE add_attendance_events SET source = 'CURRENT_RECONCILE'",
    "reconcile_15m_source": "UPDATE add_attendance_events SET source = 'RECONCILE_15M'",
    "event_uid": "UPDATE add_attendance_events SET event_uid = upper(event_uid)",
    # Any saved record decides; this one is not canonical, so it proves nothing.
    "non_canonical_saved_record": """
        INSERT INTO add_terminal_record_manifest (
          connector_id, zkt_device_id, terminal_serial, generation, ordinal, source_kind,
          canonical_source, record_size, raw_record_digest, terminal_record_key,
          occurrence_index, attendance_event_id, disposition, observed_uid, observed_user_id,
          created_at)
        SELECT e.connector_id, e.zkt_device_id, e.device_serial, 1, 0, 'BASELINE', false, 40,
          repeat('b', 64), repeat('c', 64), 1, e.id, 'EVENT', '28943', e.user_id, now()
        FROM add_attendance_events e
    """,
}


def test_dump_guard_admits_only_a_retained_interval_release(force_pg):
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    event_id, user_id = _held_dump(sessions)
    for filename in EARLIER_GUARDS:
        _apply(engine, filename)
    with engine.connect() as conn:
        assert _proposed(conn, event_id, user_id) is False

    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        connector = db.get(Connector, event.connector_id)
        zkt, user = connector.zkt_device, db.get(DeviceUser, user_id)
        assert not synced_cnic_identity_proven(db, connector, zkt, user, event)
        assert synced_cnic_identity_proven(
            db, connector, zkt, user, event, allow_reconcile_batch=True
        )
        # The older guard refuses: the row stays held without a rollback.
        assert release_synced_cnic_attendance(
            db, zkt=zkt, user=user,
            dump_release=DumpIdentityRelease(held_since=None, remaining=10),
        ) == 0
        db.commit()
        assert db.get(AttendanceEvent, event_id).ords_status == "BLOCKED_IDENTITY"

    _apply(engine, DUMP_GUARD)
    with engine.connect() as conn:
        assert _proposed(conn, event_id, user_id) is True
    for name, statement in BREAKERS.items():
        with engine.connect() as conn:
            conn.execute(text(statement))
            assert _proposed(conn, event_id, user_id) is False, name
            conn.rollback()

    with sessions() as db:
        zkt = db.get(Connector, db.get(AttendanceEvent, event_id).connector_id).zkt_device
        assert release_synced_cnic_attendance(
            db, zkt=zkt, user=db.get(DeviceUser, user_id),
            dump_release=DumpIdentityRelease(held_since=None, remaining=10),
        ) == 1
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.ords_status == "PENDING" and not event.manual_release_required
        assert event.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
        assert db.scalar(select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.attendance_event_id == event_id
        )) == 1

    # The trigger itself: an unproven raw release cannot reach delivery.
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE add_attendance_events SET ords_status = 'BLOCKED_IDENTITY',
              manual_release_required = true WHERE id = :id
        """), {"id": event_id})
        conn.execute(text("UPDATE add_attendance_identity_history SET revoked = true"))
    with pytest.raises(DBAPIError, match="explicit administrator approval"):
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE add_attendance_events SET ords_status = 'PENDING',
                  manual_release_required = false WHERE id = :id
            """), {"id": event_id})
    with engine.begin() as conn:
        conn.execute(text("UPDATE add_attendance_identity_history SET revoked = false"))
        conn.execute(text("""
            UPDATE add_attendance_events SET ords_status = 'PENDING',
              manual_release_required = false WHERE id = :id
        """), {"id": event_id})
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.ords_status == "PENDING" and not event.manual_release_required


def _snapshot(db, connector, key, observed_at):
    replace_user_snapshot(
        db, connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=key, complete=True, stable=True, observed_at=observed_at,
            users=[UserSnapshotRow(
                uid="7", user_id="1007", name="Test Person-3520212345671",
                terminal_identity_fingerprint=FP,
            )],
        ),
    )


def _dump(seed, when, captured):
    return AttendanceEventIn(
        event_uid=hashlib.sha256(seed.encode()).hexdigest(), uid="7", user_id="1007",
        terminal_serial="TEST-REPAIR-SERIAL", terminal_identity_fingerprint=FP,
        device_event_time=when, captured_at=captured, source="DUMP_RECONNECT",
        clock_quality="OK", punch=0, raw_event={"attendance_record_uid": "28943"},
    )


def test_snapshot_and_owner_backlog_release_pass_the_installed_guard(force_pg, monkeypatch):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "*")
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    for filename in (*EARLIER_GUARDS, DUMP_GUARD):
        _apply(engine, filename)
    now = utc_now()
    snapshot, dump = _snapshot, _dump

    recent = dump("pg-recent", now - timedelta(minutes=20), now - timedelta(minutes=1))
    old = dump("pg-old", now - timedelta(days=4), now - timedelta(days=4) + timedelta(minutes=3))
    with sessions() as db:
        connector = db.scalar(select(Connector))
        snapshot(db, connector, "pg-s0", now - timedelta(days=5))
        ingest_attendance(db, connector=connector, events=[old])
        db.flush()
        old_row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid))
        # ADD held it four days ago, before the rollout window.
        old_row.received_at = old.captured_at + timedelta(seconds=20)
        db.commit()
    with sessions() as db:
        connector = db.scalar(select(Connector))
        snapshot(db, connector, "pg-s1", now - timedelta(minutes=25))
        ingest_attendance(db, connector=connector, events=[recent])
        db.commit()
    with sessions() as db:
        rows = db.scalars(select(AttendanceEvent).where(
            AttendanceEvent.event_uid.in_([recent.event_uid, old.event_uid])
        )).all()
        assert [row.ords_status for row in rows] == ["BLOCKED_IDENTITY"] * 2
        connector = db.scalar(select(Connector))
        snapshot(db, connector, "pg-s2", utc_now())
        db.commit()

    def status(event_uid):
        with sessions() as db:
            row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid))
            return row.ords_status, row.manual_release_required

    assert status(recent.event_uid) == ("PENDING", False)
    assert status(old.event_uid) == ("BLOCKED_IDENTITY", True)

    with sessions() as db:
        zone = db.scalar(select(Connector)).zone_id
        groups, _job = _approve_whole_zone(db, zone, "pg-backlog-0001")
        assert sum(entry[1] for entry in groups.values()) == 1
        db.commit()
    # A snapshot transaction holding the row: the batch waits, never skips it.
    with sessions() as holder:
        holder.scalar(
            select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid)
            .with_for_update()
        )
        with sessions() as db:
            assert backlog.advance_once(db) == 0
            db.commit()
        holder.rollback()
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.action == backlog.ACTION
        ))
        assert job.status == "RELEASING" and job.cursor == 0
    for _ in range(3):
        with sessions() as db:
            backlog.advance_once(db)
            db.commit()
    assert status(old.event_uid) == ("PENDING", False)
    with sessions() as db:
        row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid))
        outbox = db.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id))
        assert outbox.delivery_type == "FULL_HISTORY"
        # Oracle acknowledges it; only then is the run finished.
        row.ords_status = outbox.status = "ACKED"
        db.commit()
    with sessions() as db:
        backlog.advance_once(db)
        db.commit()
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.action == backlog.ACTION
        ))
        assert job.status == "COMPLETED"
        assert backlog.serialize(db, job)["delivered"] == 1


def test_a_snapshot_release_and_the_owner_batch_never_release_a_punch_twice(
    force_pg, monkeypatch,
):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "*")
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    for filename in (*EARLIER_GUARDS, DUMP_GUARD):
        _apply(engine, filename)
    now = utc_now()
    recent = _dump("pg-race", now - timedelta(minutes=20), now - timedelta(minutes=1))
    with sessions() as db:
        connector = db.scalar(select(Connector))
        _snapshot(db, connector, "race-s0", now - timedelta(minutes=40))
        _snapshot(db, connector, "race-s1", now - timedelta(minutes=25))
        ingest_attendance(db, connector=connector, events=[recent])
        db.commit()
    with sessions() as db:
        # The owner approved the zone while the punch waited for its snapshot.
        _approve_whole_zone(db, db.scalar(select(Connector)).zone_id, "pg-race-1", approved=1)
        db.commit()
    with sessions() as snapshot_db:
        _snapshot(snapshot_db, snapshot_db.scalar(select(Connector)), "race-s2", utc_now())
        snapshot_db.flush()
        # The snapshot holds the released row: the batch waits, never passes it.
        with sessions() as db:
            assert backlog.advance_once(db) == 0
            db.commit()
        snapshot_db.commit()
    with sessions() as db:
        backlog.advance_once(db)
        db.commit()
    with sessions() as db:
        row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == recent.event_uid))
        assert row.ords_status == "PENDING"
        assert db.scalar(select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.attendance_event_id == row.id
        )) == 1
        [audit] = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED"
        )).all()
        assert audit.after["event_ids"] == [row.id]
        job = db.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.action == backlog.ACTION
        ))
        assert job.status == "COMPLETED"
        assert backlog.serialize(db, job)["released"] == 0


def test_chunk_release_rereads_a_row_another_transaction_released(force_pg):
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    event_id, user_id = _held_dump(sessions)
    for filename in (*EARLIER_GUARDS, DUMP_GUARD):
        _apply(engine, filename)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        # A full-history read linked the terminal's own saved record.
        db.add(TerminalRecordManifest(
            connector_id=event.connector_id, zkt_device_id=event.zkt_device_id,
            terminal_serial=event.device_serial, generation=1, ordinal=0,
            source_kind="BASELINE", canonical_source=True, record_size=40,
            raw_record_digest="b" * 64, terminal_record_key="c" * 64, occurrence_index=1,
            attendance_event_id=event.id, disposition="EVENT", observed_uid=event.uid,
            observed_user_id=event.user_id,
        ))
        db.commit()
    with sessions() as chunk_db:
        stale = chunk_db.get(AttendanceEvent, event_id)
        connector = chunk_db.get(Connector, stale.connector_id)
        assert stale.ords_status == "BLOCKED_IDENTITY"
        with sessions() as other:
            zkt = other.get(Connector, stale.connector_id).zkt_device
            assert release_synced_cnic_attendance(
                other, zkt=zkt, user=other.get(DeviceUser, user_id)
            ) == 1
            released_at = other.get(AttendanceEvent, event_id).identity_resolved_at
            other.commit()
        # The chunk still holds its earlier, held copy of the row.
        assert stale.ords_status == "BLOCKED_IDENTITY"
        assert _release_source_synced_attendance(
            chunk_db, connector=connector, events=[stale]
        ) == set()
        assert stale.ords_status == "PENDING"
        chunk_db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.identity_resolved_at == released_at
        assert db.scalar(select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.attendance_event_id == event_id
        )) == 1


def test_one_zone_check_page_runs_at_a_time(force_pg):
    sessions, _, _ = force_pg
    with sessions() as first:
        zone = first.scalar(select(Connector)).zone_id
        backlog.preview_page(first, zone_id=zone, actor=ADMIN)
        with sessions() as second:
            with pytest.raises(backlog.RecoveryError) as busy:
                backlog.preview_page(second, zone_id=zone, actor=ADMIN)
            assert busy.value.code == "PREVIEW_BUSY"
        first.rollback()
    with sessions() as second:
        assert backlog.preview_page(second, zone_id=zone, actor=ADMIN)["zone_id"] == zone


@pytest.mark.parametrize("race", ["user_read", "guard"])
def test_a_snapshot_committed_mid_batch_never_skips_an_approved_punch(
    force_pg, monkeypatch, race,
):
    """The routine 15-minute snapshot commits while the owner's batch checks a punch."""
    sessions, _, _ = force_pg
    engine = sessions.kw["bind"]
    for filename in (*EARLIER_GUARDS, DUMP_GUARD):
        _apply(engine, filename)
    now = utc_now()
    old = _dump(f"mid-{race}", now - timedelta(days=4), now - timedelta(days=4) + timedelta(minutes=3))
    with sessions() as db:
        connector = db.scalar(select(Connector))
        _snapshot(db, connector, "m-s0", now - timedelta(days=5))
        ingest_attendance(db, connector=connector, events=[old])
        db.flush()
        row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid))
        row.received_at = old.captured_at + timedelta(seconds=20)
        db.commit()
    with sessions() as db:
        _snapshot(db, db.scalar(select(Connector)), "m-s1", utc_now())
        db.commit()
    with sessions() as db:
        groups, _job = _approve_whole_zone(db, db.scalar(select(Connector)).zone_id, f"mid-{race}")
        assert sum(entry[1] for entry in groups.values()) == 1
        db.commit()

    fired = []

    def commit_snapshot():
        if not fired:
            fired.append(1)
            with sessions() as other:
                _snapshot(other, other.scalar(select(Connector)), "m-s2", utc_now())
                other.commit()

    if race == "user_read":
        original = backlog._unique_user

        def racing(session, cache, zkt_id, user_id):
            commit_snapshot()  # after the terminal was read, before the user
            return original(session, cache, zkt_id, user_id)

        monkeypatch.setattr(backlog, "_unique_user", racing)
    else:
        original = backlog.release_synced_cnic_attendance_event

        def racing(session, **arguments):
            commit_snapshot()  # after both were read: the guard sees the new roster
            return original(session, **arguments)

        monkeypatch.setattr(backlog, "release_synced_cnic_attendance_event", racing)
    with sessions() as db:
        assert backlog.advance_once(db) == 0
        db.commit()
    with sessions() as db:
        row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid))
        job = db.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.action == backlog.ACTION
        ))
        assert row.ords_status == "BLOCKED_IDENTITY" and job.cursor == 0
        run = backlog.serialize(db, job)
        assert run["kept_held"] == 0 and run["waiting_reason"] == "WAITING_FOR_ROSTER_UPDATE"
    with sessions() as db:
        assert backlog.advance_once(db) == 1
        db.commit()
    with sessions() as db:
        row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == old.event_uid))
        assert row.ords_status == "PENDING" and not row.manual_release_required
        job = db.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.action == backlog.ACTION
        ))
        run = backlog.serialize(db, job)
        assert (run["released"], run["kept_held"], run["waiting_reason"]) == (1, 0, None)

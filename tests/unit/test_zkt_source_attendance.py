"""Synthetic cutover integration; no field permit or model qualification claim."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import struct

import pytest
from sqlalchemy import event as sa_event, select

import test_zkt_raw_source_custody as source
from test_zkt_source_load import store as store
from zk_add import zkt_custody_work as work, zkt_source_attendance as derived_attendance
from zk_add.attendance_repair import _protected_digest
from zk_add.crypto import encrypt_json, encrypt_text
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, TerminalRecordManifest,
    TerminalSourceEpoch, ZktCustodyWork, ZktDerivedEvidence, ZktOccurrenceAlias,
    ZktOracleIntent, ZktSourceAttendance, ZktSourceCutover)
from zk_add.schemas import ReconciliationSourceRecord
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import source_occurrence_delivery_hold
from zk_add.zkt_decode import PAKISTAN_TIME, encode_time

source_store = source.source_store
count = source.count


def permit(db, size=16, first=0):
    """An isolated synthetic stand-in for the future proved release handoff."""
    connector = db.scalar(select(Connector))
    connector.zkt_device.model = "G3"
    epoch = db.scalar(select(TerminalSourceEpoch).order_by(TerminalSourceEpoch.id))
    row = ZktSourceCutover(connector_id=connector.id, source_epoch_id=epoch.id,
        first_new_ordinal=first, terminal_serial=connector.zkt_device.serial, model="G3", record_size=size)
    value = derived_attendance.cutover_material(row, connector, epoch,
        migration_digest="a" * 64, writer_digest="b" * 64, boot_id="synthetic-cutover")
    row.authority_digest, row.protected_authority = _protected_digest(value), encrypt_json(value)
    db.add(row)
    db.flush()
    return row


def source_bytes(size=16, user_id=1007):
    raw = bytearray(size)
    encoded = encode_time(utc_now().astimezone(PAKISTAN_TIME).replace(microsecond=0))
    if size == 16:
        struct.pack_into("<IIBB", raw, 0, user_id, encoded, 1, 0)
    elif size == 40:
        struct.pack_into("<H", raw, 0, 999)  # Attendance UID is not enrollment UID.
        value = str(user_id).encode()
        raw[2:2+len(value)] = value
        raw[26] = 1
        struct.pack_into("<I", raw, 27, encoded)
    else:
        struct.pack_into("<HBI", raw, 0, 999, 1, encoded)
    return bytes(raw)


def intake(db, *, size=16, length=2):
    connector, request = source.prepare(db, "tail", size=size, length=length)
    raw = source_bytes(size)
    digest = hashlib.sha256(raw).hexdigest()
    records = [ReconciliationSourceRecord(ordinal=index, raw_record_digest=digest,
        terminal_record_key=digest, occurrence_index=index + 1, disposition="RAW_PRESERVED",
        raw_record_b64=base64.b64encode(raw).decode()) for index in range(length)]
    request = source.sealed(request.model_copy(update={"records": records}))
    source.apply(db, connector, "tail", request)
    db.commit()
    return request


@pytest.fixture
def prepared(source_store, monkeypatch):
    monkeypatch.setattr(settings, "pii_lookup_key", "synthetic-derived-attendance-key")
    monkeypatch.setattr("zk_add.db.SessionLocal", source_store)
    return source_store


@pytest.mark.parametrize("size", [16, 40])
def test_real_ingress_and_worker_create_distinct_intents_without_rewriting_custody(prepared, size):
    with prepared() as db:
        permit(db, size=size)
        request = intake(db, size=size)
        manifests = db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.id)).all()
        original = [(row.disposition, row.protected_raw_record, row.attendance_event_id) for row in manifests]
        assert work.advance_work_batch(db, time_budget_ms=None).processed == 2
        db.commit()
        events = db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.id)).all()
        assert len(events) == count(db, OrdsOutbox) == count(db, ZktOracleIntent) == count(db, ZktSourceAttendance) == 2
        assert events[0].event_uid != events[1].event_uid
        assert events[0].device_event_time == events[1].device_event_time
        assert all(row.uid is None and row.user_id == "1007" and row.cnic_encrypted is None for row in events)
        assert all(row.manual_release_required and row.oracle_confirmed_at is None for row in events)
        assert all(row.verification_scope == "ORACLE_UID_MEMBERSHIP_V1" for row in db.scalars(select(ZktOracleIntent)))
        assert [(row.disposition, row.protected_raw_record, row.attendance_event_id) for row in manifests] == original
        assert all(row.attendance_event_id is None for row in db.scalars(select(ZktOccurrenceAlias)))
        connector = db.scalar(select(Connector))
        for alias in db.scalars(select(ZktOccurrenceAlias)):
            assert source_occurrence_delivery_hold(db, connector, alias.occurrence_id) is None
        assert all(row.state == "ATTENDANCE_CREATED" for row in db.scalars(select(ZktCustodyWork)))
        source.apply(db, connector, "tail", request)
        assert work.advance_work(db) == 0
        db.commit()
        assert count(db, AttendanceEvent) == count(db, ZktSourceAttendance) == 2


def test_plausible_source_without_cutover_does_not_activate(prepared):
    with prepared() as db:
        intake(db)
        assert work.advance_work_batch(db, time_budget_ms=None).processed == 2
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
        assert all(row.state == "WAIT_PROFILE" for row in db.scalars(select(ZktCustodyWork)))


@pytest.mark.parametrize("fault,reason", [("before", "SOURCE_BEFORE_CUTOVER"),
    ("model", "SOURCE_CUTOVER_EVIDENCE_CHANGED"), ("authority", "SOURCE_CUTOVER_EVIDENCE_CHANGED"),
    ("size", "SOURCE_PROFILE_LAYOUT_CHANGED"), ("uid-only", "SOURCE_IDENTITY_REFERENCE_UNAVAILABLE")])
def test_unproved_or_pre_cutover_source_cannot_create_attendance(prepared, fault, reason):
    with prepared() as db:
        size = 8 if fault == "uid-only" else 16
        row = permit(db, size=40 if fault == "size" else size, first=2 if fault == "before" else 0)
        if fault == "model":
            db.scalar(select(Connector)).zkt_device.model = "unknown"
        elif fault == "authority":
            row.first_new_ordinal = 1
        intake(db, size=size)
        assert work.advance_work_batch(db, time_budget_ms=None).processed == 2
        db.commit()
        assert count(db, AttendanceEvent) == count(db, ZktOracleIntent) == 0
        assert all(row.state == "HELD_SOURCE" and row.reason_code == reason for row in db.scalars(select(ZktCustodyWork)))
        assert count(db, ZktDerivedEvidence) == 2  # Original interpretation remains inspectable.


def copy_epoch(db, *, changed=False, missing=False):
    connector = db.scalar(select(Connector))
    parent = db.scalar(select(TerminalSourceEpoch).order_by(TerminalSourceEpoch.id.desc()))
    child = TerminalSourceEpoch(zkt_device_id=parent.zkt_device_id, terminal_generation=1,
        sequence=parent.sequence + 1, parent_epoch_id=parent.id)
    db.add(child)
    db.flush()
    prior = db.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.source_epoch_id == parent.id))
    raw = source_bytes(user_id=1008) if changed else base64.b64decode(source.decrypt_text(prior.protected_raw_record))
    row = TerminalRecordManifest(connector_id=connector.id, zkt_device_id=prior.zkt_device_id,
        terminal_serial=prior.terminal_serial, generation=1, source_epoch_id=child.id,
        ordinal=4 if missing else prior.ordinal, source_kind="RECOVERY_PREFIX", canonical_source=True,
        record_size=prior.record_size, raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(raw).hexdigest(), disposition="RAW_PRESERVED",
        protected_raw_record=encrypt_text(base64.b64encode(raw).decode()))
    db.add(row)
    db.flush()
    work.attach_source_work(db, connector, [row])
    db.commit()
    return row


def test_recovery_prefix_reuses_original_event_and_intent_across_three_epochs(prepared):
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        uid = db.scalar(select(AttendanceEvent.event_uid))
        for _ in range(2):
            copy_epoch(db)
            assert work.advance_work_batch(db, time_budget_ms=None).processed == 1
            db.commit()
        assert count(db, ZktSourceAttendance) == 3
        assert count(db, AttendanceEvent) == count(db, ZktOracleIntent) == count(db, OrdsOutbox) == 1
        assert db.scalar(select(AttendanceEvent.event_uid)) == uid
        assert len(set(db.scalars(select(ZktSourceAttendance.canonical_alias_id)))) == 1


@pytest.mark.parametrize("fault", ["changed", "missing"])
def test_changed_or_missing_recovery_ordinal_does_not_invent_a_punch(prepared, fault):
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        copy_epoch(db, changed=fault == "changed", missing=fault == "missing")
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        assert count(db, AttendanceEvent) == count(db, ZktOracleIntent) == 1
        row = db.scalar(select(ZktCustodyWork).order_by(ZktCustodyWork.id.desc()))
        assert row.state == "HELD_SOURCE"
        assert row.reason_code in {"SOURCE_CHANGED_OCCURRENCE_REVIEW", "SOURCE_RECOVERY_PREFIX_MISSING"}


@pytest.mark.parametrize("fault", ["event", "capture-time", "source-policy", "raw", "derived", "link"])
def test_changed_derived_evidence_cannot_authorize_delivery(prepared, fault):
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        if fault == "event":
            db.scalar(select(AttendanceEvent)).user_id = "unrelated"
        elif fault == "capture-time":
            from datetime import timedelta
            db.scalar(select(AttendanceEvent)).captured_at += timedelta(days=1)
        elif fault == "source-policy":
            row = db.scalar(select(AttendanceEvent))
            row.raw_event = {**row.raw_event, "source_policy": "OTHER"}
        elif fault == "raw":
            db.scalar(select(TerminalRecordManifest)).protected_raw_record = encrypt_text(base64.b64encode(b"x"*16).decode())
        elif fault == "derived":
            db.scalar(select(ZktDerivedEvidence)).protected_evidence = encrypt_json({"changed": True})
        else:
            db.scalar(select(ZktSourceAttendance)).facts_digest = "f"*64
        db.commit()
        alias = db.scalar(select(ZktOccurrenceAlias))
        assert source_occurrence_delivery_hold(db, db.scalar(select(Connector)), alias.occurrence_id) == "SOURCE_DERIVED_ATTENDANCE_CHANGED"


def test_intent_insert_failure_cannot_commit_partial_attendance(prepared):
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        def fail(_connection, _cursor, statement, _params, _context, _many):
            if statement.startswith("INSERT INTO add_zkt_oracle_intents"):
                raise RuntimeError("synthetic intent failure")
        engine = db.get_bind()
        sa_event.listen(engine, "before_cursor_execute", fail)
        try:
            work.advance_work_batch(db, time_budget_ms=None)
        finally:
            sa_event.remove(engine, "before_cursor_execute", fail)
        db.commit()
        assert count(db, AttendanceEvent) == count(db, ZktSourceAttendance) == count(db, OrdsOutbox) == 0
        assert count(db, TerminalRecordManifest) == count(db, ZktDerivedEvidence) == 1
        row = db.scalar(select(ZktCustodyWork))
        assert row.state == "RETRY_SYSTEM"
        row.next_attempt_at = utc_now()
        db.commit()
    # The actual processor resumes in a new transaction/session after a tick.
    with prepared() as db:
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        assert count(db, AttendanceEvent) == count(db, ZktOracleIntent) == 1


def test_overlapping_workers_create_one_event_per_ordinal(prepared):
    if prepared.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Real row locks require PostgreSQL")
    with prepared() as db:
        permit(db)
        intake(db)
    def advance():
        with prepared() as db:
            result = work.advance_work_batch(db, time_budget_ms=None)
            db.commit()
            return result.processed
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: advance(), range(2))) == 2
    with prepared() as db:
        assert count(db, AttendanceEvent) == count(db, ZktOracleIntent) == count(db, ZktSourceAttendance) == 2


def test_source_to_owned_oracle_membership_uses_existing_identity_guards(prepared, monkeypatch):
    import asyncio
    from datetime import timedelta
    from zk_add import worker, zkt_oracle_delivery as delivery
    from zk_add.crypto import cnic_lookup, encrypt_cnic
    from zk_add.models import DeviceUser, DeviceUserSnapshot, ZktOracleMembershipReceipt, ZktOracleContentReceipt
    from test_zkt_membership_delivery import response
    monkeypatch.setattr(settings, "ords_base_url", "https://synthetic.invalid/attendance")
    monkeypatch.setattr(settings, "ords_username", "synthetic")
    monkeypatch.setattr(settings, "ords_password", "synthetic")
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        connector = db.scalar(select(Connector))
        terminal = connector.zkt_device
        now = utc_now()
        snapshot = DeviceUserSnapshot(zkt_device_id=terminal.id, snapshot_id="synthetic", revision=1,
            state_hash="a"*64, complete=True, stable=True, user_count=1, observed_at=now)
        db.add(snapshot)
        db.flush()
        terminal.identity_snapshot_id = snapshot.id
        terminal.snapshot_complete = terminal.identity_snapshot_stable = True
        terminal.identity_snapshot_observed_at = now
        terminal.last_identity_change_at = now - timedelta(days=1)
        terminal.identity_snapshot_revision = 1
        db.add(DeviceUser(zkt_device_id=terminal.id, uid="42", user_id="1007", display_name="Synthetic Person",
            cnic_encrypted=encrypt_cnic("1234512345671"), cnic_lookup_hash=cnic_lookup("1234512345671"),
            cnic_last4="5671", terminal_identity_fingerprint="f"*64, snapshot_revision=1))
        db.flush()
        assert work.advance_work_batch(db, time_budget_ms=None).processed == 1
        db.commit()
        event = db.scalar(select(AttendanceEvent))
        assert event.identity_resolution_status == "RESOLVED" and not event.manual_release_required
        assert event.uid is None  # Never reuse the historical attendance UID.
        assert db.scalar(select(OrdsOutbox)).status == "PENDING"
    ordinary, owned = delivery.split_claims(worker.claim_ords_batch(10))
    assert ordinary == [] and len(owned) == 1
    async def existing(uid):
        return response(uid)
    monkeypatch.setattr(delivery, "_membership_request", existing)
    asyncio.run(delivery.deliver(owned, concurrency=1))
    with prepared() as db:
        assert db.scalar(select(AttendanceEvent)).oracle_confirmation_path == "ADD_ZKT_UID_ONLY_V1"
        assert count(db, ZktOracleMembershipReceipt) == 1 and count(db, ZktOracleContentReceipt) == 0
        assert db.scalar(select(TerminalRecordManifest)).disposition == "RAW_PRESERVED"
        assert db.scalar(select(TerminalRecordManifest)).attendance_event_id is None


def test_source_binding_migration_is_additive_and_preserves_permits_on_rollback(prepared, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from zk_add.db import Base
    from zk_add.models import ZktLegacyHandoff
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261005_0051_zkt_source_attendance.py"
    spec = importlib.util.spec_from_file_location("zkt_source_attendance_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = prepared.kw["bind"]
    with engine.begin() as connection:
        # Reconstruct the pre-0051 schema, including later dependent tables.
        ZktLegacyHandoff.__table__.drop(connection)
        ZktSourceAttendance.__table__.drop(connection)
        ZktSourceCutover.__table__.drop(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        tables = {ZktSourceAttendance.__tablename__, ZktSourceCutover.__tablename__}
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare_to: kind != "table" or name in tables})
        assert compare_metadata(context, Base.metadata) == []
        ZktLegacyHandoff.__table__.create(connection)
    with prepared() as db:
        permit(db)
        intake(db, length=1)
        work.advance_work_batch(db, time_budget_ms=None)
        db.commit()
        authority = db.scalar(select(ZktSourceCutover.protected_authority))
        identity = db.scalar(select(AttendanceEvent.event_uid))
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with prepared() as db:
        assert db.scalar(select(ZktSourceCutover.protected_authority)) == authority
        assert db.scalar(select(AttendanceEvent.event_uid)) == identity
        alias = db.scalar(select(ZktOccurrenceAlias))
        assert derived_attendance.binding_event(db, db.scalar(select(Connector)), alias).event_uid == identity

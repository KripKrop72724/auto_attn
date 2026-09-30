"""Unchanged approved aliases remain valid through routine terminal snapshots."""

from copy import deepcopy
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from test_add_backend import CNIC, connector_fixture, db as db, event, make_writable
from test_attendance_force_postgres import (
    force_pg as force_pg,
    postgres_store as postgres_store,
    repair_store as repair_store,
    store as store,
)
from zk_add import attendance_direct_ords as direct
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.crypto import decrypt_json, encrypt_cnic
from zk_add.identity_conflicts import (
    build_identity_conflict_report,
    create_same_employee_resolution,
    valid_resolution_for_user,
)
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision,
    AttendanceRecoveryItem,
    AttendanceRecoveryJob,
    AuditChainHead,
    AuditEvent,
    DeviceUser,
    DeviceUserSnapshot,
    OrdsOutbox,
)
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import (
    block_undelivered_attendance,
    ingest_attendance,
    replace_user_snapshot,
    restore_resolved_alias_direct_approvals,
)
from zk_add.time_utils import utc_now


def snapshot(db, connector, *, extra_member=False):
    users = [
        UserSnapshotRow(uid="1", user_id="1001", name=f"Same Person-{CNIC}"),
        UserSnapshotRow(uid="2", user_id="1002", name=f"Same Person Alias-{CNIC}"),
    ]
    if extra_member:
        users.append(UserSnapshotRow(uid="3", user_id="1003", name=f"New Alias-{CNIC}"))
    replace_user_snapshot(
        db, connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=str(uuid4()), complete=True, stable=True,
            observed_at=utc_now(), users=users,
        ),
    )
    db.flush()


def approve_alias_group(db, connector):
    group = build_identity_conflict_report(db, zkt=connector.zkt_device)["groups"][0]
    return create_same_employee_resolution(
        db, zkt=connector.zkt_device, group_token=group["group_token"],
        members=[(row["user_key"], row["row_version"]) for row in group["members"]],
        reason="Both terminal records belong to the same employee.",
        idempotency_key=str(uuid4()), actor="operator",
    )


def saved_direct_approval(db, connector):
    punch = event(event_uid="c" * 64, user_id="1001", uid="1")
    ingest_attendance(db, connector=connector, events=[punch])
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid))
    # Model the linked identity hold for which an administrator approved the
    # current user. Snapshot validation must preserve this frozen source.
    row.ords_status = "BLOCKED_IDENTITY"
    row.cnic_encrypted = row.cnic_lookup_hash = row.cnic_last4 = None
    db.flush()
    direct.create(
        db, actor="operator",
        request=DirectOrdsStartRequest(
            event_ids=[row.id], reason="Reviewed current employee for this exact saved punch.",
            password="test-password", idempotency_key=str(uuid4()),
        ),
    )
    direct.advance_once(db)
    db.flush()
    decision = db.scalar(select(AttendanceForceReleaseDecision))
    assert decision is not None
    assert row.ords_status == "PENDING"
    assert direct.approved_payload(db, row, connector, decision) is not None
    return row, decision


def test_snapshot_preserves_saved_approval_for_current_resolved_alias_group(db):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot(db, connector)
    resolution = approve_alias_group(db, connector)
    row, decision = saved_direct_approval(db, connector)
    original_source_digest = direct._source_digest(row, connector)
    original_payload = decrypt_json(decision.payload_encrypted)
    original_snapshot_id = row.identity_snapshot_id

    snapshot(db, connector)

    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "1001"))
    assert user.identity_conflict_code == "DUPLICATE_CNIC"
    assert valid_resolution_for_user(db, zkt=connector.zkt_device, user=user) is resolution
    assert resolution.status == "ACTIVE"
    assert row.ords_status == "PENDING"
    assert row.identity_snapshot_id == original_snapshot_id
    assert direct._source_digest(row, connector) == original_source_digest
    assert direct.approved_payload(db, row, connector, decision) == original_payload
    assert db.scalar(select(OrdsOutbox)).status == "PENDING"
    assert row.oracle_confirmed_at is None


def previously_blocked_approval(db):
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot(db, connector)
    resolution = approve_alias_group(db, connector)
    row, decision = saved_direct_approval(db, connector)
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "1001"))
    original_payload = decrypt_json(decision.payload_encrypted)
    original_source_digest = direct._source_digest(row, connector)

    assert block_undelivered_attendance(db, zkt=connector.zkt_device, user=user) == 1
    direct.advance_once(db)
    db.flush()

    assert row.ords_status == "BLOCKED_IDENTITY"
    assert direct._source_digest(row, connector) == original_source_digest
    assert direct.approved_payload(db, row, connector, decision) == original_payload
    item = db.get(AttendanceRecoveryItem, decision.item_id)
    job = db.get(AttendanceRecoveryJob, decision.job_id)
    outbox = db.scalar(select(OrdsOutbox))
    assert item.status == "NEEDS_REVIEW" and item.error_code == "BLOCKED_IDENTITY"
    item.result = {**item.result, "direct_post_attempts": 1}
    outbox.attempt_count = 3
    db.flush()
    return connector, user, resolution, row, decision, item, job, outbox


def test_fresh_snapshot_recovers_only_the_unchanged_prior_alias_approval(db):
    db.autoflush = False
    connector, _user, _resolution, row, decision, item, job, outbox = previously_blocked_approval(db)
    original_source = direct._source_digest(row, connector)
    original_proof = deepcopy(decision.proof)
    original_payload_encrypted = decision.payload_encrypted
    original_raw = deepcopy(row.raw_event)
    original_snapshot_id = row.identity_snapshot_id

    snapshot(db, connector)

    assert row.ords_status == outbox.status == "PENDING"
    assert row.cnic_encrypted is row.cnic_lookup_hash is None
    assert row.raw_event == original_raw
    assert row.identity_snapshot_id == original_snapshot_id
    assert direct._source_digest(row, connector) == original_source
    assert decision.proof == original_proof
    assert decision.payload_encrypted == original_payload_encrypted
    assert item.status == "WAITING_ORACLE" and item.error_code is None
    assert item.result["direct_post_attempts"] == 1
    assert outbox.attempt_count == 3
    assert job.status == "WAITING_ORACLE" and job.completed_at is None
    assert row.oracle_confirmed_at is None
    audit = db.scalar(select(AuditEvent).where(
        AuditEvent.action == "ATTENDANCE_DIRECT_ORDS_APPROVAL_REVALIDATED"
    ))
    assert audit.actor == "system:snapshot-validation"
    assert audit.target_id == str(row.id)
    assert audit.after["decision_id"] == decision.id
    snapshot(db, connector)
    assert len(db.scalars(select(AuditEvent).where(
        AuditEvent.action == "ATTENDANCE_DIRECT_ORDS_APPROVAL_REVALIDATED"
    )).all()) == 1


def test_alias_approval_recovery_commits_with_postgres_guard_and_row_locks(force_pg):
    sessions, _connector_id, _event_uid = force_pg
    with sessions() as session:
        connector, _user, _resolution, row, decision, _item, _job, _outbox = (
            previously_blocked_approval(session)
        )
        event_id, decision_id = row.id, decision.id
        original_source = direct._source_digest(row, connector)
        session.commit()
        snapshot(session, connector)
        session.commit()
        session.expire_all()
        row = session.get(AttendanceEvent, event_id)
        decision = session.get(AttendanceForceReleaseDecision, decision_id)
        outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event_id))
        assert row.ords_status == outbox.status == "PENDING"
        assert row.manual_release_required
        assert row.cnic_encrypted is None
        assert direct._source_digest(row, connector) == original_source
        assert direct.approved_payload(session, row, connector, decision) is not None
        assert session.get(AttendanceRecoveryItem, decision.item_id).result["direct_post_attempts"] == 1
        assert outbox.attempt_count == 3


def test_alias_recovery_skips_locked_job_then_next_snapshot_recovers_postgres(force_pg):
    sessions, _connector_id, _event_uid = force_pg
    with sessions() as session:
        connector, user, _resolution, row, decision, item, job, _outbox = (
            previously_blocked_approval(session)
        )
        connector_id, user_id, event_id, item_id, job_id = (
            connector.id, user.id, row.id, item.id, job.id
        )
        original_source = direct._source_digest(row, connector)
        session.commit()
    with sessions() as job_owner, sessions() as snapshot_session:
        job_owner.scalar(select(AttendanceRecoveryJob).where(
            AttendanceRecoveryJob.id == job_id
        ).with_for_update())
        # A different stale-group audit may already own the chain head in the
        # snapshot transaction. Waiting for the job here would invert the
        # scheduler's job -> audit-head order.
        snapshot_session.scalar(select(AuditChainHead).where(
            AuditChainHead.id == 1
        ).with_for_update())
        snapshot_session.execute(text("SET LOCAL lock_timeout = '500ms'"))
        connector = snapshot_session.get(type(connector), connector_id)
        user = snapshot_session.get(DeviceUser, user_id)
        latest = snapshot_session.get(DeviceUserSnapshot, connector.zkt_device.identity_snapshot_id)
        started = monotonic()
        assert restore_resolved_alias_direct_approvals(
            snapshot_session, zkt=connector.zkt_device, user=user, snapshot=latest,
        ) == 0
        assert monotonic() - started < 2
        row = snapshot_session.get(AttendanceEvent, event_id)
        item = snapshot_session.get(AttendanceRecoveryItem, item_id)
        assert row.ords_status == "BLOCKED_IDENTITY"
        assert item.status == "NEEDS_REVIEW"
        assert direct._source_digest(row, connector) == original_source
        assert snapshot_session.scalar(select(OrdsOutbox).where(
            OrdsOutbox.attendance_event_id == event_id
        )).status == "BLOCKED_IDENTITY"
        assert snapshot_session.scalar(select(AuditEvent).where(
            AuditEvent.action == "ATTENDANCE_DIRECT_ORDS_APPROVAL_REVALIDATED"
        )) is None
        snapshot_session.rollback()
        job_owner.rollback()
    with sessions() as session:
        connector = session.get(type(connector), connector_id)
        snapshot(session, connector)
        session.commit()
        row = session.get(AttendanceEvent, event_id)
        assert row.ords_status == "PENDING"
        assert direct._source_digest(row, connector) == original_source
        assert session.get(AttendanceRecoveryItem, item_id).status == "WAITING_ORACLE"
        assert session.get(AttendanceRecoveryJob, job_id).status == "WAITING_ORACLE"


@pytest.mark.parametrize("invalid", [
    "source", "current_cnic", "current_name", "payload_cipher", "payload_digest",
    "legacy_proof", "wrong_current_user", "wrong_item", "wrong_actor", "in_flight",
    "oracle_confirmed", "outbox_acknowledged", "different_hold", "revoked",
    "partial_snapshot", "wrong_policy", "cancelled_job",
    "immutable_facts_changed", "immutable_facts_missing", "terminal_changed",
    "terminal_missing", "hardware_changed", "hardware_missing", "wrong_resolution_type",
])
def test_alias_approval_recovery_fails_closed_without_every_original_guard(db, invalid):
    connector, user, resolution, row, decision, item, job, outbox = previously_blocked_approval(db)
    current_snapshot = db.get(DeviceUserSnapshot, connector.zkt_device.identity_snapshot_id)
    if invalid == "source":
        row.raw_event = {**row.raw_event, "changed": True}
    elif invalid == "current_cnic":
        user.cnic_encrypted = encrypt_cnic("3520212345672")
    elif invalid == "current_name":
        user.display_name = "Changed Employee"
    elif invalid == "payload_cipher":
        decision.payload_encrypted = "unreadable"
    elif invalid == "payload_digest":
        decision.payload_digest = "0" * 64
    elif invalid == "legacy_proof":
        decision.proof = {key: value for key, value in decision.proof.items() if key != "cnic_source"}
    elif invalid == "wrong_current_user":
        decision.proof = {**decision.proof, "current_user_key": "another-user"}
    elif invalid == "wrong_item":
        item.attendance_event_id = None
    elif invalid == "wrong_actor":
        decision.actor = "another-operator"
    elif invalid == "in_flight":
        outbox.status = "IN_FLIGHT"
    elif invalid == "oracle_confirmed":
        row.oracle_confirmed_at = utc_now()
    elif invalid == "outbox_acknowledged":
        outbox.acknowledged_at = utc_now()
    elif invalid == "different_hold":
        item.error_code = "EVIDENCE_CHANGED"
    elif invalid == "revoked":
        resolution.status = "REVOKED"
    elif invalid == "partial_snapshot":
        current_snapshot.complete = False
    elif invalid == "wrong_policy":
        decision.proof = {**decision.proof, "policy": "another-policy"}
    elif invalid == "cancelled_job":
        job.status = "CANCELLED"
    elif invalid == "immutable_facts_changed":
        decision.proof = {
            **decision.proof,
            "immutable_facts": {**decision.proof["immutable_facts"], "device_event_time": "changed"},
        }
    elif invalid == "immutable_facts_missing":
        decision.proof = {key: value for key, value in decision.proof.items() if key != "immutable_facts"}
    elif invalid == "terminal_changed":
        decision.proof = {**decision.proof, "terminal": "ANOTHER-TERMINAL"}
    elif invalid == "terminal_missing":
        decision.proof = {key: value for key, value in decision.proof.items() if key != "terminal"}
    elif invalid == "hardware_changed":
        decision.proof = {**decision.proof, "hardware_id": "another-connector"}
    elif invalid == "hardware_missing":
        decision.proof = {key: value for key, value in decision.proof.items() if key != "hardware_id"}
    elif invalid == "wrong_resolution_type":
        resolution.resolution_type = "ANOTHER_RESOLUTION"
    db.flush()
    if invalid.startswith(("immutable_facts_", "terminal_", "hardware_")):
        # The preexisting payload validator does not bind these proof fields;
        # checkpoint recovery must independently check their exact contents.
        assert direct.approved_payload(db, row, connector, decision) is not None

    assert restore_resolved_alias_direct_approvals(
        db, zkt=connector.zkt_device, user=user, snapshot=current_snapshot,
    ) == 0
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert item.status == "NEEDS_REVIEW"
    assert item.result["direct_post_attempts"] == 1
    assert outbox.attempt_count == 3
    assert db.scalar(select(AuditEvent).where(
        AuditEvent.action == "ATTENDANCE_DIRECT_ORDS_APPROVAL_REVALIDATED"
    )) is None


@pytest.mark.parametrize("change", ["revoked", "new_member", "unresolved"])
def test_snapshot_still_holds_duplicate_aliases_without_current_resolution(db, change):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot(db, connector)
    resolution = approve_alias_group(db, connector)
    row, _decision = saved_direct_approval(db, connector)
    if change == "revoked":
        resolution.status = "REVOKED"
    elif change == "unresolved":
        db.delete(resolution)
    db.flush()

    snapshot(db, connector, extra_member=change == "new_member")

    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "1001"))
    assert valid_resolution_for_user(db, zkt=connector.zkt_device, user=user) is None
    if change == "new_member":
        assert resolution.status == "STALE"
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert row.identity_resolution_status == "BLOCKED_CONFLICT"
    assert row.cnic_encrypted is None
    assert db.scalar(select(OrdsOutbox)).status == "BLOCKED_IDENTITY"
    assert row.oracle_confirmed_at is None

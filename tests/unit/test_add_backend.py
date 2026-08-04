from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event as sqlalchemy_event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import zk_add.worker as worker
from zk_add.crypto import (
    cnic_lookup,
    decrypt_cnic,
    decrypt_json,
    decrypt_text,
    encrypt_cnic,
)
from zk_add.db import Base, SessionLocal
from zk_add.identity import build_machine_name
from zk_add.identity_conflicts import (
    build_identity_conflict_report,
    create_same_employee_resolution,
)
from zk_add.models import (
    AttendanceEvent,
    AuditEvent,
    Connector,
    ConnectorCredential,
    DeviceAlert,
    DeviceCommand,
    DeviceConnectionEvent,
    DeviceUser,
    HistoricalCurrentIdentityResolution,
    IdentityConflictResolution,
    IdentityTombstone,
    OnboardingNonce,
    OracleReceipt,
    OrdsOutbox,
    TemporaryAdminLease,
    UserDeletionItem,
)
from zk_add.onboarding import derive_bootstrap_secret, verify_onboarding_signature
from zk_add.protocol import body_sha256, sign_request, signature_material
from zk_add.schemas import (
    AttendanceBatchRequest,
    AttendanceEventIn,
    HeartbeatPayload,
    OracleReceiptBatchRequest,
    UserCreateRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
    UserUpdateRequest,
)
from zk_add.security import (
    ADMIN_COOKIE,
    AdminContext,
    create_admin_session,
    hash_admin_password,
    require_step_up,
)
from zk_add.service import (
    advance_user_deletion_jobs,
    apply_command_update,
    build_historical_identity_report,
    cancel_user_deletion_job,
    create_admin_lease,
    create_command,
    create_device_user_command,
    create_historical_directory_identity,
    create_historical_event_group_identity,
    create_historical_identity_alias,
    create_user_deletion_job,
    delete_device_user_command,
    ingest_attendance,
    onboard_connector,
    oracle_payload,
    repair_verified_active_identity_backlog,
    repair_verified_tombstone_backlog,
    replace_user_snapshot,
    record_oracle_receipts,
    reconcile_admin_lease_states,
    serialize_command,
    serialize_user_deletion_job,
    resolve_historical_event_group_to_current_identity,
    update_device_user_command,
    update_heartbeat,
    upsert_alert,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.web import app, get_db
from zk_add.worker import (
    ORDS_DELIVERY_BATCH_SIZE,
    ORDS_DELIVERY_CONCURRENCY,
    ORDS_FIRMWARE_AUDIT_BATCH_SIZE,
    apply_confirmed_membership_audit_failure,
    apply_confirmed_membership_missing,
    apply_firmware_receipt_audit_failure,
    apply_firmware_receipt_missing,
    apply_ords_confirmation,
    apply_ords_delivery_result,
    claim_confirmed_membership_audit_batch,
    claim_firmware_receipt_audit_batch,
    event_uid_is_valid,
    ords_delivery_metrics,
    ords_delivery_succeeded,
    ords_circuit_is_open,
    ords_circuit_retry_after_seconds,
    ords_failure_category,
    ords_failure_is_permanent,
    ords_membership_missing,
    reconcile_ords_delivery_alerts,
    record_ords_route_result,
)


MAC = "e0:72:a1:d6:f3:28"
SERIAL = "ADZV211860253"
CNIC = "3520212345671"


def test_ords_delivery_defaults_drain_retry_backlog_in_bounded_batches():
    assert ORDS_DELIVERY_BATCH_SIZE == 100
    assert 1 <= ORDS_DELIVERY_CONCURRENCY <= ORDS_DELIVERY_BATCH_SIZE
    assert ORDS_DELIVERY_BATCH_SIZE <= 500
    assert 1 <= ORDS_FIRMWARE_AUDIT_BATCH_SIZE <= 500


def test_ords_transport_circuit_uses_bounded_exponential_backoff(monkeypatch):
    monkeypatch.setattr(worker, "_ords_circuit_failures", 0)
    monkeypatch.setattr(worker, "_ords_circuit_open_until", 0.0)

    record_ords_route_result(
        status=None,
        transport_error="ConnectTimeout",
        now=100.0,
    )
    assert ords_circuit_is_open(now=100.0)
    assert ords_circuit_retry_after_seconds(now=100.0) == 15
    assert not ords_circuit_is_open(now=115.0)

    record_ords_route_result(
        status=None,
        transport_error="ConnectTimeout",
        now=115.0,
    )
    assert ords_circuit_retry_after_seconds(now=115.0) == 30

    for failure_index in range(2, 10):
        record_ords_route_result(
            status=None,
            transport_error="ConnectTimeout",
            now=115.0 + failure_index,
        )
    assert ords_circuit_retry_after_seconds(now=125.0) <= 300
    assert ords_circuit_retry_after_seconds(now=125.0) > 0


def test_any_http_response_closes_ords_transport_circuit(monkeypatch):
    monkeypatch.setattr(worker, "_ords_circuit_failures", 4)
    monkeypatch.setattr(worker, "_ords_circuit_open_until", 500.0)

    record_ords_route_result(
        status=503,
        transport_error=None,
        now=100.0,
    )

    assert not ords_circuit_is_open(now=100.0)
    assert ords_circuit_retry_after_seconds(now=100.0) == 0
    assert worker._ords_circuit_failures == 0


def test_ords_transport_failure_pauses_parallel_worker_paths(monkeypatch):
    monkeypatch.setattr(worker, "_ords_circuit_failures", 0)
    monkeypatch.setattr(worker, "_ords_circuit_open_until", 0.0)
    monkeypatch.setattr(worker, "_ords_request_lock", asyncio.Lock())
    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "ords_username", "test-user")
    monkeypatch.setattr(settings, "ords_password", "test-password")

    claim_counts = {"firmware": 0, "confirmed": 0}
    request_count = 0

    def claim_firmware(_limit):
        claim_counts["firmware"] += 1
        return [(1, "a" * 64, 1)]

    def claim_confirmed(_limit):
        claim_counts["confirmed"] += 1
        return [(2, "b" * 64, 2)]

    async def fail_membership_check(_client, _url, _claims):
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0)
        return None, None, "ConnectTimeout", False

    monkeypatch.setattr(worker, "claim_firmware_receipt_audit_batch", claim_firmware)
    monkeypatch.setattr(worker, "claim_confirmed_membership_audit_batch", claim_confirmed)
    monkeypatch.setattr(worker, "post_ords_membership_check", fail_membership_check)
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(
        worker,
        "apply_firmware_receipt_audit_failure",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker,
        "apply_confirmed_membership_audit_failure",
        lambda *_args, **_kwargs: None,
    )

    async def run_parallel_paths():
        await asyncio.gather(
            worker.audit_firmware_receipts_batch(limit=1),
            worker.audit_confirmed_membership_batch(limit=1),
        )

    asyncio.run(run_parallel_paths())

    assert request_count == 1
    assert sum(claim_counts.values()) == 1
    assert ords_circuit_is_open()


def test_ords_delivery_preflight_prevents_transport_failure_fanout(monkeypatch):
    monkeypatch.setattr(worker, "_ords_circuit_failures", 0)
    monkeypatch.setattr(worker, "_ords_circuit_open_until", 0.0)
    monkeypatch.setattr(worker, "_ords_request_lock", asyncio.Lock())
    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "ords_username", "test-user")
    monkeypatch.setattr(settings, "ords_password", "test-password")

    claims = [
        (1, {"event_uid": "a" * 64}, 1, False),
        (2, {"event_uid": "b" * 64}, 2, True),
    ]
    membership_requests = 0
    delivery_requests = 0
    failed_claims: list[int] = []

    async def fail_membership_check(_client, _url, requested_claims):
        nonlocal membership_requests
        membership_requests += 1
        assert requested_claims == claims
        return None, None, "ConnectTimeout", False

    async def unexpected_delivery(*_args, **_kwargs):
        nonlocal delivery_requests
        delivery_requests += 1
        raise AssertionError("delivery must not fan out after the route probe fails")

    monkeypatch.setattr(worker, "claim_ords_batch", lambda _limit: claims)
    monkeypatch.setattr(worker, "post_ords_membership_check", fail_membership_check)
    monkeypatch.setattr(worker, "post_ords_claim", unexpected_delivery)
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(
        worker,
        "apply_ords_membership_failure",
        lambda _session, *, claimed_id, **_kwargs: failed_claims.append(claimed_id),
    )

    asyncio.run(worker.deliver_ords_batch(limit=2, concurrency=2))

    assert membership_requests == 1
    assert delivery_requests == 0
    assert failed_claims == [1, 2]
    assert ords_circuit_is_open()


def test_ords_delivery_preflight_sends_only_proven_missing_events(monkeypatch):
    monkeypatch.setattr(worker, "_ords_circuit_failures", 0)
    monkeypatch.setattr(worker, "_ords_circuit_open_until", 0.0)
    monkeypatch.setattr(worker, "_ords_request_lock", asyncio.Lock())
    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "ords_username", "test-user")
    monkeypatch.setattr(settings, "ords_password", "test-password")

    existing_uid = "a" * 64
    missing_uid = "b" * 64
    claims = [
        (1, {"event_uid": existing_uid}, 1, False),
        (2, {"event_uid": missing_uid}, 2, False),
    ]
    delivered_claims: list[int] = []
    confirmed_claims: list[int] = []
    applied_results: list[int] = []

    async def check_membership(_client, _url, requested_claims):
        assert requested_claims == claims
        return (
            200,
            {
                "success": True,
                "received_count": 2,
                "existing_count": 1,
                "missing_count": 1,
                "missing_event_uids": [missing_uid],
            },
            None,
            True,
        )

    async def deliver_missing(_client, _semaphore, _url, claim):
        delivered_claims.append(claim[0])
        return claim[0], claim[2], 201, {"success": True}, None, True

    monkeypatch.setattr(worker, "claim_ords_batch", lambda _limit: claims)
    monkeypatch.setattr(worker, "post_ords_membership_check", check_membership)
    monkeypatch.setattr(worker, "post_ords_claim", deliver_missing)
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(
        worker,
        "apply_ords_confirmation",
        lambda _session, *, claimed_id, **_kwargs: confirmed_claims.append(claimed_id),
    )
    monkeypatch.setattr(
        worker,
        "apply_ords_delivery_result",
        lambda _session, *, claimed_id, **_kwargs: applied_results.append(claimed_id),
    )

    asyncio.run(worker.deliver_ords_batch(limit=2, concurrency=2))

    assert confirmed_claims == [1]
    assert delivered_claims == [2]
    assert applied_results == [2]
    assert not ords_circuit_is_open()


@pytest.fixture()
def db() -> Session:
    settings.pii_fernet_key = Fernet.generate_key().decode()
    settings.pii_lookup_key = "test-lookup-key-with-enough-entropy"
    settings.fleet_root_secret = "test-fleet-root-secret-with-enough-entropy"
    settings.admin_username = "StateHealthAdmin"
    settings.admin_password_hash = hash_admin_password("correct-password")
    settings.admin_cookie_secure = False
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    app.dependency_overrides.clear()


def connector_fixture(
    db: Session,
    *,
    hardware_id: str = MAC,
    expected_serial: str | None = SERIAL,
) -> Connector:
    connector, _token, _created = onboard_connector(
        db,
        hardware_id=hardware_id,
        zone_id="ZONE-SLICTOWER-3FL",
        zone_name="ZONE-SLICTOWER-3FL",
        device_id="1",
        firmware_version="2.1.0",
        expected_serial=expected_serial,
        actor="test",
        ip_address="127.0.0.1",
    )
    db.commit()
    return connector


def make_writable(connector: Connector) -> None:
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.serial = SERIAL
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = True
    zkt.user_count = zkt.user_count or 0
    zkt.attendance_count = zkt.attendance_count or 0
    zkt.capability_profile = {
        "observed_user_record_bytes": 72,
        "read_users": True,
        "read_attendance": True,
        "user_write": True,
        "create_user": True,
        "delete_user": True,
        "admin_lease": True,
        "protocol_restart": True,
        "name_bytes": 24,
    }


def snapshot_user(
    db: Session,
    connector: Connector,
    *,
    uid: str = "7",
    user_id: str = "1007",
    name: str = f"Ayesha-{CNIC}",
    privilege: int = 0,
    terminal_identity_fingerprint: str | None = None,
    terminal_state_fingerprint: str | None = None,
) -> DeviceUser:
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=f"snapshot-{uid}-{user_id}",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=uid,
                    user_id=user_id,
                    name=name,
                    privilege=privilege,
                    terminal_identity_fingerprint=terminal_identity_fingerprint,
                    terminal_state_fingerprint=terminal_state_fingerprint,
                )
            ],
        ),
    )
    db.flush()
    return db.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == connector.zkt_device.id,
            DeviceUser.uid == uid,
            DeviceUser.lifecycle_state == "ACTIVE",
        )
    )


def event(
    *,
    event_uid: str,
    user_id: str = "1007",
    raw_name: str | None = None,
    uid: str | None = "7",
):
    return AttendanceEventIn(
        event_uid=event_uid,
        uid=uid,
        user_id=user_id,
        raw_name=raw_name,
        device_event_time=utc_now(),
        captured_at=utc_now(),
        source="LIVE",
        punch=0,
        status=0,
        clock_quality="OK",
        raw_event={"raw_name": raw_name, "safe": "retained"},
    )


def test_historical_identity_alias_repairs_blocked_events_and_is_idempotent(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    target = snapshot_user(
        db,
        connector,
        uid="61",
        user_id="13",
        name=f"NoumanI-{CNIC}",
    )
    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="a" * 64,
                user_id="CL04209",
                raw_name=None,
            )
        ],
    )
    db.flush()
    assert accepted == ["a" * 64]
    assert duplicates == []
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "a" * 64)
    )
    assert blocked is not None
    assert blocked.ords_status == "BLOCKED_IDENTITY"
    assert blocked.cnic_encrypted is None

    with pytest.raises(ValueError, match="does not match"):
        create_historical_identity_alias(
            db,
            connector=connector,
            source_user_id="CL04209",
            source_cnic="1111111111111",
            target_user=target,
            reason="Untrusted evidence must fail closed.",
            idempotency_key="alias-wrong-evidence",
            actor="StateHealthAdmin",
        )
    assert db.scalar(select(IdentityTombstone)) is None

    alias, repaired = create_historical_identity_alias(
        db,
        connector=connector,
        source_user_id="CL04209",
        source_cnic=CNIC,
        target_user=target,
        reason="Oracle retained one unique CNIC for this historical terminal identity.",
        idempotency_key="test-alias-key",
        actor="StateHealthAdmin",
    )
    db.flush()
    assert alias.user_id == "CL04209"
    assert alias.device_user_id == target.id
    assert repaired == 1
    assert blocked.device_user_id == target.id
    assert blocked.identity_resolution_status == "RESOLVED_HISTORICAL_ALIAS"
    assert blocked.identity_repair_reason == "VERIFIED_HISTORICAL_ALIAS"
    assert blocked.ords_status == "PENDING"
    assert decrypt_cnic(blocked.cnic_encrypted) == CNIC
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == blocked.id)
    )
    assert outbox is not None
    assert outbox.status == "PENDING"

    replay, replay_repaired = create_historical_identity_alias(
        db,
        connector=connector,
        source_user_id="CL04209",
        source_cnic=CNIC,
        target_user=target,
        reason="Idempotent operator retry.",
        idempotency_key="test-alias-key",
        actor="StateHealthAdmin",
    )
    assert replay.id == alias.id
    assert replay_repaired == 0


def test_historical_directory_identity_repairs_deleted_service_number_user(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    source = snapshot_user(
        db,
        connector,
        uid="7",
        user_id="03981",
        name="Hamza Nawab",
    )
    source.present = False
    source.lifecycle_state = "DELETED"
    source.deleted_at = utc_now()
    db.flush()
    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="d" * 64,
                user_id="03981",
                raw_name="Hamza Nawab",
            )
        ],
    )
    assert accepted == ["d" * 64]
    assert duplicates == []
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "d" * 64)
    )
    assert blocked is not None
    assert blocked.ords_status == "BLOCKED_IDENTITY"

    with pytest.raises(ValueError, match="service number"):
        create_historical_directory_identity(
            db,
            connector=connector,
            source_user=source,
            source_cnic=CNIC,
            directory_employee_id="5294",
            directory_service_number="3982",
            directory_employee_name="Hamza Nawab",
            directory_zone_code="75",
            expected_version=source.row_version,
            reason="Reject mismatched directory evidence.",
            idempotency_key="directory-mismatch",
            actor="StateHealthAdmin",
        )

    tombstone, repaired = create_historical_directory_identity(
        db,
        connector=connector,
        source_user=source,
        source_cnic=CNIC,
        directory_employee_id="5294",
        directory_service_number="3981",
        directory_employee_name="Hamza Nawab",
        directory_zone_code="75",
        expected_version=source.row_version,
        reason="Exact HR service number and employee name verified.",
        idempotency_key="directory-verified",
        actor="StateHealthAdmin",
    )
    db.flush()
    assert repaired == 1
    assert tombstone.device_user_id == source.id
    assert decrypt_cnic(tombstone.cnic_encrypted) == CNIC
    assert decrypt_cnic(source.cnic_encrypted) == CNIC
    assert blocked.identity_resolution_status == "RESOLVED_DIRECTORY_EVIDENCE"
    assert blocked.identity_repair_reason == "VERIFIED_HR_DIRECTORY_EVIDENCE"
    assert blocked.ords_status == "PENDING"
    assert decrypt_cnic(blocked.cnic_encrypted) == CNIC
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == blocked.id)
    )
    assert outbox is not None
    assert outbox.status == "PENDING"

    replay, replay_repaired = create_historical_directory_identity(
        db,
        connector=connector,
        source_user=source,
        source_cnic=CNIC,
        directory_employee_id="5294",
        directory_service_number="03981",
        directory_employee_name="Hamza Nawab",
        directory_zone_code="75",
        expected_version=source.row_version,
        reason="Idempotent directory evidence retry.",
        idempotency_key="directory-verified",
        actor="StateHealthAdmin",
    )
    assert replay.id == tombstone.id
    assert replay_repaired == 0


def test_historical_identity_report_attributes_only_unambiguous_deleted_users(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    source = snapshot_user(
        db,
        connector,
        uid="34",
        user_id="CL04197",
        name="Dr M Sohail Khan",
    )
    source.present = False
    source.lifecycle_state = "DELETED"
    source.deleted_at = utc_now()
    db.flush()
    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="e" * 64,
                user_id="CL04197",
                raw_name="Dr M Sohail Khan",
                uid="34",
            ),
            event(
                event_uid="f" * 64,
                user_id="not-in-snapshot",
                raw_name=None,
            ),
        ],
    )
    assert accepted == ["e" * 64, "f" * 64]
    assert duplicates == []
    before = db.scalar(
        select(func.count()).select_from(AttendanceEvent)
    )

    report = build_historical_identity_report(
        db,
        zkt=connector.zkt_device,
    )

    assert report["totals"] == {
        "unresolved_events": 2,
        "blocked_identity": 2,
        "quarantined_identity_reuse": 0,
        "attributed_to_deleted_users": 1,
        "unassigned_events": 1,
        "actionable_event_groups": 1,
        "candidate_users": 1,
    }
    assert report["rows"][0]["source_user_key"] == source.user_key
    assert report["rows"][0]["user_id"] == "CL04197"
    assert report["rows"][0]["event_count"] == 1
    assert report["rows"][0]["resolution_path"] == "HR_DIRECTORY_EVIDENCE"
    assert report["rows"][0]["operator_actionable"] is True
    assert len(report["unassigned_groups"]) == 1
    assert report["unassigned_groups"][0]["user_id"] == "not-in-snapshot"
    assert report["unassigned_groups"][0]["uid"] == "7"
    assert report["unassigned_groups"][0]["resolution_path"] == (
        "HR_DIRECTORY_EVENT_GROUP"
    )
    assert report["unassigned_groups"][0]["operator_actionable"] is True
    assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == before


def test_historical_event_group_identity_repairs_exact_orphan_without_touching_live_user(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="2" * 64,
                user_id="CL04999",
                raw_name="Former Employee",
                uid="77",
            ),
            event(
                event_uid="3" * 64,
                user_id="CL04999",
                raw_name="Former Employee",
                uid="77",
            ),
        ],
    )
    report = build_historical_identity_report(db, zkt=connector.zkt_device)
    candidate = report["unassigned_groups"][0]
    assert candidate["event_count"] == 2
    assert candidate["operator_actionable"] is True

    with pytest.raises(ValueError, match="changed since it was selected"):
        create_historical_event_group_identity(
            db,
            connector=connector,
            group_token="0" * 64,
            source_user_id="CL04999",
            source_uid="77",
            source_cnic=CNIC,
            directory_employee_id="5294",
            directory_service_number="CL04999",
            directory_employee_name="Former Employee",
            directory_zone_code="75",
            reason="Reject a stale historical event group.",
            idempotency_key="event-group-stale",
            actor="StateHealthAdmin",
        )

    tombstone, repaired = create_historical_event_group_identity(
        db,
        connector=connector,
        group_token=candidate["group_token"],
        source_user_id="CL04999",
        source_uid="77",
        source_cnic=CNIC,
        directory_employee_id="5294",
        directory_service_number="cl04999",
        directory_employee_name="Former Employee",
        directory_zone_code="75",
        reason="Exact orphan cohort verified against the HR directory.",
        idempotency_key="event-group-verified",
        actor="StateHealthAdmin",
    )
    db.flush()

    assert repaired == 2
    assert tombstone.user_id == "CL04999"
    assert tombstone.uid == "77"
    source = db.get(DeviceUser, tombstone.device_user_id)
    assert source is not None
    assert source.lifecycle_state == "DELETED"
    assert source.present is False
    assert source.source == "HR_DIRECTORY_EVIDENCE"
    assert decrypt_cnic(source.cnic_encrypted) == CNIC
    rows = db.scalars(
        select(AttendanceEvent).where(
            AttendanceEvent.event_uid.in_(["2" * 64, "3" * 64])
        )
    ).all()
    assert len(rows) == 2
    assert all(row.device_user_id == source.id for row in rows)
    assert all(row.ords_status == "PENDING" for row in rows)
    assert all(
        row.identity_resolution_status == "RESOLVED_DIRECTORY_EVENT_GROUP"
        for row in rows
    )
    assert all(decrypt_cnic(row.cnic_encrypted) == CNIC for row in rows)
    assert db.scalar(
        select(func.count())
        .select_from(OrdsOutbox)
        .where(OrdsOutbox.attendance_event_id.in_([row.id for row in rows]))
    ) == 2


def test_historical_event_group_identity_accepts_named_legacy_cohort_without_uid(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="6" * 64,
                user_id="CL04888",
                raw_name="Legacy Employee",
                uid="",
            ),
            event(
                event_uid="a" * 64,
                user_id="CL04888",
                raw_name="LegacyEmployee",
                uid="",
            ),
        ],
    )
    report = build_historical_identity_report(db, zkt=connector.zkt_device)
    candidate = report["unassigned_groups"][0]
    assert candidate["uid"] == ""
    assert candidate["display_name"] == "Legacy Employee"
    assert candidate["operator_actionable"] is True

    tombstone, repaired = create_historical_event_group_identity(
        db,
        connector=connector,
        group_token=candidate["group_token"],
        source_user_id="CL04888",
        source_uid="",
        source_cnic=CNIC,
        directory_employee_id="5294",
        directory_service_number="cl04888",
        directory_employee_name="Legacy Employee",
        directory_zone_code="75",
        reason="Named legacy cohort verified against the HR directory.",
        idempotency_key="legacy-event-group-verified",
        actor="StateHealthAdmin",
    )

    assert repaired == 2
    assert tombstone.uid == ""
    assert all(
        row.ords_status == "PENDING"
        for row in db.scalars(
            select(AttendanceEvent).where(
                AttendanceEvent.event_uid.in_(["6" * 64, "a" * 64])
            )
        ).all()
    )


def test_historical_event_group_resolves_to_exact_current_verified_identity(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="9" * 64,
                user_id="03752",
                raw_name="Ubaidullah",
                uid="",
            )
        ],
    )
    active = snapshot_user(
        db,
        connector,
        uid="11",
        user_id="03752",
        name="Ubaidullah",
    )
    active.cnic_encrypted = encrypt_cnic(CNIC)
    active.cnic_lookup_hash = cnic_lookup(CNIC)
    active.cnic_last4 = CNIC[-4:]
    db.flush()

    report = build_historical_identity_report(db, zkt=connector.zkt_device)
    candidate = report["unassigned_groups"][0]
    assert candidate["resolution_path"] == "CURRENT_IDENTITY_EVIDENCE"
    assert candidate["active_user_key"] == active.user_key
    assert candidate["active_user_row_version"] == active.row_version
    assert candidate["operator_actionable"] is True
    before = db.scalar(select(func.count()).select_from(AttendanceEvent))

    resolution, target, repaired = (
        resolve_historical_event_group_to_current_identity(
            db,
            connector=connector,
            group_token=candidate["group_token"],
            source_user_id="03752",
            source_uid="",
            target_user_key=active.user_key,
            expected_version=active.row_version,
            source_cnic=CNIC,
            verified_employee_name="Ubaidullah",
            reason="Oracle raw capture identity exactly matches the current user.",
            idempotency_key="current-identity-03752",
            actor="StateHealthAdmin",
        )
    )
    db.flush()

    assert target.id == active.id
    assert repaired == 1
    assert resolution.event_count == 1
    assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == before
    row = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "9" * 64)
    )
    assert row is not None
    assert row.device_user_id == active.id
    assert row.ords_status == "PENDING"
    assert row.identity_resolution_status == "RESOLVED_CURRENT_IDENTITY_EVIDENCE"
    assert row.identity_repair_reason == "VERIFIED_CURRENT_IDENTITY_EVENT_GROUP"
    assert decrypt_cnic(row.cnic_encrypted) == CNIC
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
    )
    assert outbox is not None
    assert outbox.status == "PENDING"
    audit = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "HISTORICAL_CURRENT_IDENTITY_VERIFIED"
        )
    )
    assert audit is not None
    assert audit.request_id == "current-identity-03752"
    assert "".join(
        character
        for character in audit.after["verified_cnic"]
        if character.isdigit()
    ).endswith(CNIC[-4:])

    replay, replay_target, replayed = (
        resolve_historical_event_group_to_current_identity(
            db,
            connector=connector,
            group_token=candidate["group_token"],
            source_user_id="03752",
            source_uid="",
            target_user_key=active.user_key,
            expected_version=active.row_version,
            source_cnic=CNIC,
            verified_employee_name="Ubaidullah",
            reason="Idempotent retry of the exact Oracle evidence.",
            idempotency_key="current-identity-03752",
            actor="StateHealthAdmin",
        )
    )
    assert replay.id == resolution.id
    assert replay_target.id == active.id
    assert replayed == 0
    assert db.scalar(
        select(func.count()).select_from(HistoricalCurrentIdentityResolution)
    ) == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"group_token": "0" * 64}, "changed since it was selected"),
        ({"expected_version": 999}, "changed since it was selected"),
        ({"source_cnic": "1111111111111"}, "CNIC evidence does not match"),
        ({"verified_employee_name": "Different Employee"}, "name does not match"),
    ],
)
def test_historical_current_identity_resolution_rejects_stale_or_mismatched_evidence(
    db: Session,
    override: dict,
    message: str,
):
    connector = connector_fixture(db)
    make_writable(connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="8" * 64,
                user_id="03752",
                raw_name="Ubaidullah",
                uid="",
            )
        ],
    )
    active = snapshot_user(
        db,
        connector,
        uid="11",
        user_id="03752",
        name="Ubaidullah",
    )
    active.cnic_encrypted = encrypt_cnic(CNIC)
    active.cnic_lookup_hash = cnic_lookup(CNIC)
    active.cnic_last4 = CNIC[-4:]
    db.flush()
    candidate = build_historical_identity_report(
        db,
        zkt=connector.zkt_device,
    )["unassigned_groups"][0]
    arguments = {
        "connector": connector,
        "group_token": candidate["group_token"],
        "source_user_id": "03752",
        "source_uid": "",
        "target_user_key": active.user_key,
        "expected_version": active.row_version,
        "source_cnic": CNIC,
        "verified_employee_name": "Ubaidullah",
        "reason": "Reject stale or mismatched current identity evidence.",
        "idempotency_key": f"reject-current-{message}",
        "actor": "StateHealthAdmin",
    }
    arguments.update(override)

    with pytest.raises(ValueError, match=message):
        resolve_historical_event_group_to_current_identity(db, **arguments)

    assert db.scalar(
        select(func.count()).select_from(HistoricalCurrentIdentityResolution)
    ) == 0
    row = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "8" * 64)
    )
    assert row is not None
    assert row.device_user_id is None
    assert row.ords_status == "BLOCKED_IDENTITY"


def test_historical_report_routes_linked_active_identity_to_certified_enrichment(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    active = snapshot_user(
        db,
        connector,
        uid="88",
        user_id="CL04666",
        name="Current Employee",
    )
    assert active.cnic_lookup_hash is None
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="b" * 64,
                user_id=active.user_id,
                raw_name="Current Employee",
                uid=active.uid,
            )
        ],
    )
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "b" * 64)
    )
    assert blocked is not None
    assert blocked.device_user_id == active.id
    assert blocked.ords_status == "BLOCKED_IDENTITY"

    report = build_historical_identity_report(db, zkt=connector.zkt_device)
    candidate = report["unassigned_groups"][0]
    assert candidate["active_user_key"] == active.user_key
    assert candidate["resolution_path"] == "ACTIVE_USER_ENRICHMENT"
    assert candidate["operator_actionable"] is True
    assert report["totals"]["actionable_event_groups"] == 1


def test_historical_event_group_identity_rejects_unnamed_legacy_cohort_without_uid(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="0" * 64,
                user_id="CL04777",
                raw_name=None,
                uid="",
            )
        ],
    )
    report = build_historical_identity_report(db, zkt=connector.zkt_device)
    candidate = report["unassigned_groups"][0]
    assert candidate["operator_actionable"] is False

    with pytest.raises(ValueError, match="stable terminal name"):
        create_historical_event_group_identity(
            db,
            connector=connector,
            group_token=candidate["group_token"],
            source_user_id="CL04777",
            source_uid="",
            source_cnic=CNIC,
            directory_employee_id="5294",
            directory_service_number="CL04777",
            directory_employee_name="Legacy Employee",
            directory_zone_code="75",
            reason="Unnamed legacy cohort must remain blocked.",
            idempotency_key="legacy-event-group-unnamed",
            actor="StateHealthAdmin",
        )


def test_historical_directory_identity_accepts_exact_alphanumeric_service_number(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    source = snapshot_user(
        db,
        connector,
        uid="33",
        user_id="CL04196",
        name="Dr Mehtab",
    )
    source.present = False
    source.lifecycle_state = "DELETED"
    source.deleted_at = utc_now()
    db.flush()
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="1" * 64,
                user_id="CL04196",
                raw_name="Dr Mehtab",
                uid="33",
            )
        ],
    )
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "1" * 64)
    )
    assert blocked is not None

    tombstone, repaired = create_historical_directory_identity(
        db,
        connector=connector,
        source_user=source,
        source_cnic=CNIC,
        directory_employee_id="5294",
        directory_service_number="cl04196",
        directory_employee_name="Dr Mehtab",
        directory_zone_code="75",
        expected_version=source.row_version,
        reason="Exact alphanumeric HR service number and employee name verified.",
        idempotency_key="directory-alphanumeric-service",
        actor="StateHealthAdmin",
    )

    assert tombstone.device_user_id == source.id
    assert repaired == 1
    assert blocked.ords_status == "PENDING"
    assert blocked.identity_resolution_status == "RESOLVED_DIRECTORY_EVIDENCE"


def test_historical_directory_identity_rejects_name_only_match(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    source = snapshot_user(
        db,
        connector,
        uid="7",
        user_id="03981",
        name="Hamza Nawab",
    )
    source.present = False
    source.lifecycle_state = "DELETED"
    db.flush()

    with pytest.raises(ValueError, match="employee name"):
        create_historical_directory_identity(
            db,
            connector=connector,
            source_user=source,
            source_cnic=CNIC,
            directory_employee_id="5294",
            directory_service_number="3981",
            directory_employee_name="Another Employee",
            directory_zone_code="75",
            expected_version=source.row_version,
            reason="A service number alone must not override a name mismatch.",
            idempotency_key="directory-name-mismatch",
            actor="StateHealthAdmin",
        )


def test_verified_tombstone_requeues_existing_blocked_punches(
    db: Session, monkeypatch
):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="7" * 64,
                user_id="unrecoverable",
                raw_name=None,
            ),
            event(event_uid="9" * 64, raw_name=None),
        ],
    )
    assert accepted == ["7" * 64, "9" * 64]
    assert duplicates == []
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "9" * 64)
    )
    assert blocked is not None

    # Model a row that arrived while identity proof was unavailable, followed
    # by a verified user deletion that preserved the identity tombstone.
    blocked.cnic_encrypted = None
    blocked.cnic_lookup_hash = None
    blocked.cnic_last4 = None
    blocked.ords_status = "BLOCKED_IDENTITY"
    blocked.identity_resolution_status = "BLOCKED_IDENTITY"
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == blocked.id)
    )
    assert outbox is not None
    outbox.status = "BLOCKED_IDENTITY"
    from zk_add.service import persist_identity_tombstone

    tombstone = persist_identity_tombstone(
        db, zkt=connector.zkt_device, user=user
    )
    db.flush()

    # The bounded scan must skip the earlier unrecoverable row instead of
    # allowing it to starve later rows that have verified tombstones.
    assert repair_verified_tombstone_backlog(db, limit=1) == 1
    assert blocked.device_user_id == tombstone.device_user_id
    assert blocked.identity_resolution_status == "RESOLVED_TOMBSTONE"
    assert blocked.identity_repair_reason == "VERIFIED_IDENTITY_TOMBSTONE"
    assert decrypt_cnic(blocked.cnic_encrypted) == CNIC
    assert blocked.ords_status == "PENDING"
    assert outbox.status == "PENDING"
    assert outbox.next_attempt_at is None

    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(db))
    claims = worker.claim_ords_batch(1)
    assert len(claims) == 1
    assert claims[0][1]["event_uid"] == "9" * 64
    assert claims[0][1]["cnic"] == CNIC


def test_verified_active_snapshot_repairs_only_safe_missing_uid_punches(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    zkt = connector.zkt_device
    assert zkt is not None
    assert user is not None

    base = utc_now()
    zkt.last_identity_change_at = base - timedelta(minutes=30)
    zkt.identity_snapshot_observed_at = base
    punches = [
        event(event_uid="1" * 64, uid=None, raw_name="Ayesha").model_copy(
            update={
                "device_event_time": base - timedelta(minutes=5),
                "captured_at": base - timedelta(minutes=4),
            }
        ),
        event(event_uid="2" * 64, uid=None, raw_name="Ayesha").model_copy(
            update={
                "device_event_time": base - timedelta(minutes=40),
                "captured_at": base - timedelta(minutes=39),
            }
        ),
        event(event_uid="3" * 64, uid=None, raw_name="Different Person").model_copy(
            update={
                "device_event_time": base - timedelta(minutes=3),
                "captured_at": base - timedelta(minutes=2),
            }
        ),
        event(event_uid="4" * 64, uid="999", raw_name="Ayesha").model_copy(
            update={
                "device_event_time": base - timedelta(minutes=3),
                "captured_at": base - timedelta(minutes=2),
            }
        ),
        event(event_uid="5" * 64, uid=None, raw_name="Ayesha").model_copy(
            update={
                "device_event_time": base + timedelta(minutes=1),
                "captured_at": base + timedelta(minutes=1),
            }
        ),
    ]
    ingest_attendance(db, connector=connector, events=punches)
    rows = {
        row.event_uid: row
        for row in db.scalars(
            select(AttendanceEvent).where(
                AttendanceEvent.event_uid.in_([p.event_uid for p in punches])
            )
        ).all()
    }
    assert all(row.ords_status == "BLOCKED_IDENTITY" for row in rows.values())

    assert repair_verified_active_identity_backlog(db) == 1
    repaired = rows["1" * 64]
    assert repaired.device_user_id == user.id
    assert repaired.identity_snapshot_id == zkt.identity_snapshot_id
    assert repaired.identity_resolution_status == "RESOLVED_CURRENT_SNAPSHOT"
    assert repaired.identity_repair_reason == "VERIFIED_CURRENT_TERMINAL_SNAPSHOT"
    assert decrypt_cnic(repaired.cnic_encrypted) == CNIC
    assert repaired.ords_status == "PENDING"
    assert db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == repaired.id)
    )
    for event_uid in ("2" * 64, "3" * 64, "4" * 64, "5" * 64):
        assert rows[event_uid].ords_status == "BLOCKED_IDENTITY"
        assert rows[event_uid].cnic_lookup_hash is None


def test_snapshot_history_recovers_backdated_punches_across_unchanged_catalog(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    continuity_start = utc_now() - timedelta(days=2)
    latest_observation = utc_now()
    user = UserSnapshotRow(uid="7", user_id="1007", name=f"Ayesha-{CNIC}")

    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="continuity-start",
            complete=True,
            stable=True,
            observed_at=continuity_start,
            users=[user],
        ),
    )
    # Simulate the production rows created before continuity initialization
    # was repaired. The next identical snapshot must recover the durable
    # history, not treat the latest observation as the beginning of identity.
    connector.zkt_device.last_identity_change_at = None
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="continuity-current",
            complete=True,
            stable=True,
            observed_at=latest_observation,
            users=[user],
        ),
    )

    assert ensure_utc(connector.zkt_device.last_identity_change_at) == ensure_utc(
        continuity_start
    )
    punch = event(
        event_uid="b" * 64,
        uid=None,
        raw_name="Ayesha",
    ).model_copy(
        update={
            "device_event_time": continuity_start + timedelta(days=1),
            "captured_at": latest_observation,
        }
    )
    ingest_attendance(db, connector=connector, events=[punch])

    assert repair_verified_active_identity_backlog(db) == 1
    repaired = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid)
    )
    assert repaired is not None
    assert repaired.ords_status == "PENDING"
    assert decrypt_cnic(repaired.cnic_encrypted) == CNIC


def test_partial_snapshot_breaks_identity_continuity_for_backdated_repair(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    first_observation = utc_now() - timedelta(days=2)
    boundary_observation = utc_now() - timedelta(days=1)
    latest_observation = utc_now()
    user = UserSnapshotRow(uid="7", user_id="1007", name=f"Ayesha-{CNIC}")

    for snapshot_id, complete, stable, observed_at in (
        ("before-boundary", True, True, first_observation),
        ("evidence-boundary", False, False, boundary_observation),
        ("after-boundary", True, True, latest_observation),
    ):
        replace_user_snapshot(
            db,
            connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=snapshot_id,
                complete=complete,
                stable=stable,
                observed_at=observed_at,
                users=[user],
            ),
        )

    assert ensure_utc(connector.zkt_device.last_identity_change_at) == ensure_utc(
        latest_observation
    )


def test_verified_active_snapshot_repair_is_not_starved_by_newer_bad_names(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_user(db, connector)
    zkt = connector.zkt_device
    assert zkt is not None

    base = utc_now()
    zkt.last_identity_change_at = base - timedelta(hours=2)
    zkt.identity_snapshot_observed_at = base
    valid = event(event_uid=f"{1:064x}", uid=None, raw_name="Ayesha").model_copy(
        update={
            "device_event_time": base - timedelta(hours=1),
            "captured_at": base - timedelta(minutes=59),
        }
    )
    ingest_attendance(db, connector=connector, events=[valid])
    for index in range(2, 27):
        invalid = event(
            event_uid=f"{index:064x}",
            uid=None,
            raw_name=f"Wrong Employee {index}",
        ).model_copy(
            update={
                "device_event_time": base - timedelta(minutes=30),
                "captured_at": base - timedelta(minutes=29),
            }
        )
        ingest_attendance(db, connector=connector, events=[invalid])

    assert repair_verified_active_identity_backlog(db, limit=1) == 1
    repaired = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == valid.event_uid)
    )
    assert repaired is not None
    assert repaired.ords_status == "PENDING"
    assert repaired.identity_repair_reason == "VERIFIED_CURRENT_TERMINAL_SNAPSHOT"


def test_verified_active_snapshot_requires_duplicate_cnic_resolution(db: Session):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="duplicate-current-users",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"Same Name-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Same Name-{CNIC}"),
            ],
        ),
    )
    zkt = connector.zkt_device
    assert zkt is not None
    base = utc_now()
    zkt.last_identity_change_at = base - timedelta(minutes=30)
    zkt.identity_snapshot_observed_at = base
    punch = event(
        event_uid="6" * 64,
        user_id="1001",
        uid=None,
        raw_name="Same Name",
    ).model_copy(
        update={
            "device_event_time": base - timedelta(minutes=2),
            "captured_at": base - timedelta(minutes=1),
        }
    )
    ingest_attendance(db, connector=connector, events=[punch])
    blocked = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid)
    )
    assert blocked is not None
    assert blocked.ords_status == "BLOCKED_IDENTITY"
    assert repair_verified_active_identity_backlog(db) == 0

    group = build_identity_conflict_report(db, zkt=zkt)["groups"][0]
    resolution = create_same_employee_resolution(
        db,
        zkt=zkt,
        group_token=group["group_token"],
        members=[
            (member["user_key"], member["row_version"])
            for member in group["members"]
        ],
        reason="Exact current membership was independently reviewed.",
        idempotency_key="repair-current-snapshot-duplicate",
        actor="StateHealthAdmin",
    )
    assert repair_verified_active_identity_backlog(db) == 1
    assert blocked.identity_resolution_id == resolution.id
    assert blocked.identity_resolution_status == "RESOLVED_CURRENT_SNAPSHOT"
    assert blocked.ords_status == "PENDING"


def test_maintenance_resolves_delivery_alert_only_after_queue_drains(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="0" * 64)],
    )
    upsert_alert(
        db,
        connector,
        code="ORDS_DELIVERY_FAILED",
        severity="WARNING",
        message="A transient Oracle delivery attempt is retrying.",
        details={"failure_category": "TRANSPORT_READTIMEOUT"},
    )
    db.flush()
    alert = db.scalar(
        select(DeviceAlert).where(
            DeviceAlert.connector_id == connector.id,
            DeviceAlert.code == "ORDS_DELIVERY_FAILED",
        )
    )
    outbox = db.scalar(select(OrdsOutbox))
    assert alert is not None
    assert outbox is not None
    assert alert.state == "OPEN"
    assert reconcile_ords_delivery_alerts(db) == 0
    assert alert.state == "OPEN"

    outbox.status = "ACKED"
    assert reconcile_ords_delivery_alerts(db) == 1
    assert alert.state == "RESOLVED"
    assert alert.resolved_at is not None


def test_historical_alias_is_eligible_for_ords_delivery(
    db: Session, monkeypatch
):
    connector = connector_fixture(db)
    make_writable(connector)
    target = snapshot_user(
        db,
        connector,
        uid="61",
        user_id="13",
        name=f"NoumanI-{CNIC}",
    )
    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="8" * 64, user_id="CL04209", raw_name=None)],
    )
    create_historical_identity_alias(
        db,
        connector=connector,
        source_user_id="CL04209",
        source_cnic=CNIC,
        target_user=target,
        reason="Approved Oracle history proves this terminal identity.",
        idempotency_key="claim-historical-alias",
        actor="StateHealthAdmin",
    )
    db.flush()

    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(db))
    claims = worker.claim_ords_batch(1)
    assert len(claims) == 1
    assert claims[0][1]["event_uid"] == "8" * 64
    assert claims[0][1]["cnic"] == CNIC


def test_request_signing_fixed_compatibility_vector():
    body = b'{"hello":"world"}'
    digest = "93a23971a914e5eacbf0a8d25154cda309c3c1c72fbb9914d47c60f3cb681588"
    assert body_sha256(body) == digest
    assert signature_material(
        method="post",
        path="/device/v2/onboard",
        timestamp="2026-07-13T12:34:56Z",
        nonce="fixed-nonce-0001",
        body_hash=digest,
    ) == "\n".join(
        ["POST", "/device/v2/onboard", "2026-07-13T12:34:56Z", "fixed-nonce-0001", digest]
    )
    assert sign_request(
        token="fixed-test-token",
        method="post",
        path="/device/v2/onboard",
        timestamp="2026-07-13T12:34:56Z",
        nonce="fixed-nonce-0001",
        body_hash=digest,
    ) == "42ae3518fe8b10ca97ef881e407a16c0610d6d469d21d11dcaf7d168c3b36552"


def test_signed_onboarding_is_automatic_replay_safe_and_idempotent(db: Session):
    body_dict = {
        "hardware_id": MAC,
        "zone_id": "ZONE-SLICTOWER-3FL",
        "zone_name": "ZONE-SLICTOWER-3FL",
        "device_id": "1",
        "firmware_version": "2.1.0",
        "expected_serial": SERIAL,
    }
    body = json.dumps(body_dict, separators=(",", ":")).encode()
    timestamp = utc_now().isoformat().replace("+00:00", "Z")
    secret = derive_bootstrap_secret(MAC)

    def headers(nonce: str) -> dict[str, str]:
        digest = body_sha256(body)
        return {
            "Content-Type": "application/json",
            "X-Zone-MAC": MAC,
            "X-ADD-Timestamp": timestamp,
            "X-ADD-Nonce": nonce,
            "X-ADD-Body-SHA256": digest,
            "X-ADD-Signature": sign_request(
                token=secret,
                method="POST",
                path="/device/v2/onboard",
                timestamp=timestamp,
                nonce=nonce,
                body_hash=digest,
            ),
        }

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    first = client.post("/device/v2/onboard", content=body, headers=headers("nonce-one"))
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert first_payload["created"] is True
    assert first_payload["schema_version"] == "2"
    assert first_payload["device_token"]

    replay = client.post("/device/v2/onboard", content=body, headers=headers("nonce-one"))
    assert replay.status_code == 409

    second = client.post("/device/v2/onboard", content=body, headers=headers("nonce-two"))
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["created"] is False
    assert second_payload["connector_id"] == first_payload["connector_id"]
    assert second_payload["device_token"] != first_payload["device_token"]
    assert db.scalar(select(func.count(Connector.id))) == 1
    assert db.scalar(select(func.count(OnboardingNonce.id))) == 2
    credentials = db.scalars(select(ConnectorCredential).order_by(ConnectorCredential.id)).all()
    assert len(credentials) == 2
    assert credentials[0].valid_until is not None


def test_onboarding_rejects_expired_or_modified_requests(db: Session):
    body = b"{}"
    digest = body_sha256(body)
    stale = (utc_now() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    signature = sign_request(
        token=derive_bootstrap_secret(MAC),
        method="POST",
        path="/device/v2/onboard",
        timestamp=stale,
        nonce="stale-nonce",
        body_hash=digest,
    )
    assert not verify_onboarding_signature(
        mac=MAC,
        method="POST",
        path="/device/v2/onboard",
        timestamp=stale,
        nonce="stale-nonce",
        supplied_body_hash=digest,
        signature=signature,
        body=body,
    )
    assert not verify_onboarding_signature(
        mac=MAC,
        method="POST",
        path="/device/v2/onboard",
        timestamp=utc_now().isoformat(),
        nonce="fresh-nonce",
        supplied_body_hash=digest,
        signature=signature,
        body=b'{"changed":true}',
    )


def test_existing_connector_rebind_preserves_history_and_rotates_token(db: Session):
    connector = connector_fixture(db)
    original_id = connector.id
    db.add(
        DeviceAlert(
            connector_id=connector.id,
            code="HISTORY_MARKER",
            severity="HIGH",
            state="OPEN",
            message="keep me",
        )
    )
    db.commit()
    rebound, _token, created = onboard_connector(
        db,
        hardware_id=MAC,
        zone_id="ZONE-SLICTOWER-3FL",
        zone_name="Updated display name",
        device_id="1",
        firmware_version="2.1.0",
        expected_serial=SERIAL,
        actor="test-rebind",
        ip_address="127.0.0.1",
    )
    db.commit()
    assert not created
    assert rebound.id == original_id
    assert db.scalar(select(DeviceAlert).where(DeviceAlert.code == "HISTORY_MARKER"))


def test_duplicate_serial_claim_is_quarantined(db: Session):
    first = connector_fixture(db)
    first.zkt_device.serial = SERIAL
    first.zkt_device.online = True
    second = connector_fixture(
        db,
        hardware_id="e0:72:a1:d6:f3:29",
        expected_serial=SERIAL,
    )
    update_heartbeat(
        db,
        connector=second,
        boot_id="second-boot",
        sequence=1,
        payload=HeartbeatPayload(
            firmware_version="2.1.0",
            zkt={
                "online": True,
                "connection_state": "ONLINE",
                "serial": SERIAL,
                "user_record_size": 72,
                "stability_since": (utc_now() - timedelta(minutes=3)).isoformat(),
            },
        ),
    )
    assert second.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    assert second.zkt_device.certification_state == "QUARANTINED"
    assert first.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    assert first.zkt_device.certification_state == "QUARANTINED"
    assert first.zkt_device.capability_profile["user_write"] is False
    assert db.scalar(
        select(DeviceAlert).where(DeviceAlert.code == "QUARANTINED_DUPLICATE_SERIAL")
    )


def test_28_byte_terminal_auto_certifies_read_only(db: Session):
    connector = connector_fixture(db)
    connector.zkt_device.snapshot_complete = True
    stable = (utc_now() - timedelta(minutes=3)).isoformat()
    payload = HeartbeatPayload(
        firmware_version="2.1.0",
        zkt={
            "online": True,
            "connection_state": "ONLINE",
            "serial": SERIAL,
            "user_record_size": 28,
            "stability_since": stable,
        },
    )
    update_heartbeat(db, connector=connector, boot_id="boot", sequence=1, payload=payload)
    update_heartbeat(db, connector=connector, boot_id="boot", sequence=2, payload=payload)
    assert connector.zkt_device.certification_state == "READ_ONLY"
    assert connector.zkt_device.writes_disabled_reason == "LEGACY_28_BYTE_RECORD"
    assert connector.zkt_device.capability_profile["user_write"] is False


def test_partial_snapshot_never_deletes_unseen_users_and_disables_writes(db: Session):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="complete",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name="One"),
                UserSnapshotRow(uid="2", user_id="1002", name="Two"),
            ],
        ),
    )
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="partial",
            complete=False,
            observed_at=utc_now(),
            users=[UserSnapshotRow(uid="1", user_id="1001", name="One updated")],
        ),
    )
    unseen = db.scalar(select(DeviceUser).where(DeviceUser.uid == "2"))
    assert unseen.present and unseen.lifecycle_state == "ACTIVE"
    assert connector.zkt_device.snapshot_complete is False
    assert connector.zkt_device.writes_disabled_reason == "USER_SNAPSHOT_TRUNCATED"
    assert db.scalar(select(DeviceAlert).where(DeviceAlert.code == "USER_SNAPSHOT_TRUNCATED"))


def test_duplicate_cnic_snapshot_is_quarantined_then_recovers_without_data_loss(
    db: Session,
):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="duplicate-cnic",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"One-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Two-{CNIC}"),
            ],
        ),
    )
    db.flush()
    users = db.scalars(
        select(DeviceUser).where(DeviceUser.lifecycle_state == "ACTIVE").order_by(DeviceUser.uid)
    ).all()
    assert len(users) == 2
    assert {row.identity_conflict_code for row in users} == {"DUPLICATE_CNIC"}
    assert {decrypt_cnic(row.cnic_encrypted) for row in users} == {CNIC}
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "DUPLICATE_USER_CNIC"))
    assert alert and alert.state == "OPEN" and alert.details == {
        "affected_users": 2,
        "duplicate_groups": 1,
    }

    raw_session, _admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    complete = client.get(f"/api/v2/devices/{connector.connector_id}/users?identity=COMPLETE")
    conflicted = client.get(f"/api/v2/devices/{connector.connector_id}/users?identity=CONFLICT")
    missing = client.get(f"/api/v2/devices/{connector.connector_id}/users?identity=MISSING")
    assert complete.status_code == conflicted.status_code == missing.status_code == 200
    assert complete.json()["rows"] == []
    assert len(conflicted.json()["rows"]) == len(missing.json()["rows"]) == 2
    assert {row["identity_conflict_code"] for row in conflicted.json()["rows"]} == {
        "DUPLICATE_CNIC"
    }
    assert conflicted.json()["identity_integrity"] == {
        "source": "CURRENT_COMPLETE_ZKT_SNAPSHOT",
        "total_users": 2,
        "with_cnic": 2,
        "missing_cnic": 0,
        "duplicate_groups": 1,
        "duplicate_users": 2,
        "resolved_duplicate_groups": 0,
        "unresolved_duplicate_groups": 1,
        "unresolved_duplicate_users": 2,
    }
    assert {
        tuple(member["user_id"] for member in row["identity_conflict_members"])
        for row in conflicted.json()["rows"]
    } == {("1001",), ("1002",)}
    assert {row["cnic_masked"] for row in conflicted.json()["rows"]} == {
        "*****-****567-1"
    }
    assert all(not row["identity_complete"] for row in conflicted.json()["rows"])
    assert CNIC not in json.dumps(conflicted.json())

    punch = event(
        event_uid="d" * 64,
        user_id="1001",
        uid="1",
        raw_name=f"One-{CNIC}",
    )
    ingest_attendance(db, connector=connector, events=[punch])
    attendance = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid))
    assert attendance and attendance.ords_status == "BLOCKED_IDENTITY"
    assert attendance.cnic_encrypted is None
    assert db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    ) is None

    make_writable(connector)
    with pytest.raises(
        ValueError,
        match=r"exact CNIC on active user records 1001, 1002",
    ):
        create_device_user_command(
            db,
            connector=connector,
            display_name="A third identity",
            cnic=CNIC,
            shift_worker=False,
            user_id_override="1003",
            idempotency_key="conflict-create-rejected",
            actor="StateHealthAdmin",
        )
    with pytest.raises(ValueError, match="replacement CNIC"):
        update_device_user_command(
            db,
            connector=connector,
            user=users[1],
            display_name=None,
            cnic=None,
            shift_worker=None,
            privilege=None,
            expected_version=users[1].row_version,
            idempotency_key="conflict-missing-replacement",
            actor="StateHealthAdmin",
        )
    replacement_cnic = "6110112345671"
    update = update_device_user_command(
        db,
        connector=connector,
        user=users[1],
        display_name=None,
        cnic=replacement_cnic,
        shift_worker=None,
        privilege=None,
        expected_version=users[1].row_version,
        idempotency_key="conflict-replacement",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=update.command_id,
        status="SUCCEEDED",
        result={"verified_uid": users[1].uid, "verified_user_id": users[1].user_id},
        error_code=None,
        error_message=None,
    )
    db.flush()
    users = db.scalars(
        select(DeviceUser).where(DeviceUser.lifecycle_state == "ACTIVE").order_by(DeviceUser.uid)
    ).all()
    assert [row.identity_conflict_code for row in users] == [None, None]
    assert {decrypt_cnic(row.cnic_encrypted) for row in users} == {CNIC, replacement_cnic}
    assert alert.state == "RESOLVED"
    assert attendance.ords_status == "PENDING"
    assert decrypt_cnic(attendance.cnic_encrypted) == CNIC
    assert db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    )


def test_same_employee_resolution_is_audited_reversible_and_never_mutates_punches(
    db: Session,
):
    connector = connector_fixture(db)
    assert connector.zkt_device is not None
    connector.zkt_device.attendance_count = 49_537
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="same-employee-duplicates",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"Same Name-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Same Name-{CNIC}"),
            ],
        ),
    )
    old_punches = [
        event(
            event_uid="7" * 64,
            user_id="1001",
            uid="1",
            raw_name=f"Same Name-{CNIC}",
        ),
        event(
            event_uid="8" * 64,
            user_id="1002",
            uid="2",
            raw_name=f"Same Name-{CNIC}",
        ),
    ]
    ingest_attendance(db, connector=connector, events=old_punches)
    db.flush()
    immutable_before = [
        (
            row.event_uid,
            row.device_user_id,
            row.user_id,
            row.device_event_time,
            row.cnic_encrypted,
            row.ords_status,
            row.identity_resolution_id,
        )
        for row in db.scalars(
            select(AttendanceEvent)
            .where(AttendanceEvent.event_uid.in_([p.event_uid for p in old_punches]))
            .order_by(AttendanceEvent.event_uid)
        ).all()
    ]
    assert {row[5] for row in immutable_before} == {"BLOCKED_IDENTITY"}

    raw_session, admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    report_response = client.get(
        f"/api/v2/devices/{connector.connector_id}/identity-conflicts"
    )
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["raw_duplicate_groups"] == report["unresolved_groups"] == 1
    assert report["resolved_groups"] == 0
    assert report["evidence_scope"] == {
        "snapshot_source": "CURRENT_COMPLETE_ZKT_SNAPSHOT",
        "terminal_attendance_count": 49_537,
        "add_attendance_count": 2,
        "attendance_coverage_percent": 0.0,
        "attendance_is_immutable": True,
        "terminal_users_are_unchanged": True,
    }
    assert report["groups"][0]["classification"] == "EXACT_NAME_MATCH"
    assert CNIC not in report_response.text

    group = report["groups"][0]
    resolve_response = client.post(
        f"/api/v2/devices/{connector.connector_id}/identity-conflicts/resolve",
        headers={"X-CSRF-Token": admin.csrf_token},
        json={
            "group_token": group["group_token"],
            "members": [
                {
                    "user_key": member["user_key"],
                    "expected_version": member["row_version"],
                }
                for member in group["members"]
            ],
            "reason": f"Exact terminal names for CNIC {CNIC} match; retain both terminal IDs.",
            "typed_confirmation": "SAME EMPLOYEE",
            "password": "correct-password",
            "idempotency_key": "resolve-01",
        },
    )
    assert resolve_response.status_code == 201, resolve_response.text
    assert CNIC not in resolve_response.text
    assert "[CNIC-REDACTED]" in resolve_response.text
    resolution_id = resolve_response.json()["resolution"]["resolution_id"]
    resolution = db.scalar(
        select(IdentityConflictResolution).where(
            IdentityConflictResolution.resolution_id == resolution_id
        )
    )
    assert resolution and resolution.status == "ACTIVE"
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "DUPLICATE_USER_CNIC"))
    assert alert and alert.state == "RESOLVED"

    immutable_after = [
        (
            row.event_uid,
            row.device_user_id,
            row.user_id,
            row.device_event_time,
            row.cnic_encrypted,
            row.ords_status,
            row.identity_resolution_id,
        )
        for row in db.scalars(
            select(AttendanceEvent)
            .where(AttendanceEvent.event_uid.in_([p.event_uid for p in old_punches]))
            .order_by(AttendanceEvent.event_uid)
        ).all()
    ]
    assert immutable_after == immutable_before

    make_writable(connector)
    resolved_user = db.scalar(
        select(DeviceUser).where(DeviceUser.user_id == "1001")
    )
    assert resolved_user is not None
    edit = update_device_user_command(
        db,
        connector=connector,
        user=resolved_user,
        display_name="Same Name Updated",
        cnic=None,
        shift_worker=None,
        privilege=None,
        expected_version=resolved_user.row_version,
        idempotency_key="edit-approved-alias-without-cnic-replacement",
        actor="StateHealthAdmin",
    )
    assert edit.command_type == "UPDATE_USER"

    resolved_punch = event(
        event_uid="9" * 64,
        user_id="1002",
        uid="2",
        raw_name=f"Same Name-{CNIC}",
    )
    ingest_attendance(db, connector=connector, events=[resolved_punch])
    db.flush()
    resolved_event = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == resolved_punch.event_uid)
    )
    assert resolved_event and resolved_event.ords_status == "PENDING"
    assert resolved_event.identity_resolution_id == resolution.id
    assert decrypt_cnic(resolved_event.cnic_encrypted) == CNIC
    assert db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == resolved_event.id)
    )

    revoke_response = client.post(
        f"/api/v2/devices/{connector.connector_id}/identity-conflicts/{resolution_id}/revoke",
        headers={"X-CSRF-Token": admin.csrf_token},
        json={
            "reason": "Exercise the protected rollback path before production use.",
            "typed_confirmation": "REVOKE RESOLUTION",
            "password": "correct-password",
        },
    )
    assert revoke_response.status_code == 200, revoke_response.text
    assert resolution.status == "REVOKED"
    revoked_punch = event(
        event_uid="a" * 64,
        user_id="1001",
        raw_name=f"Same Name-{CNIC}",
    )
    ingest_attendance(db, connector=connector, events=[revoked_punch])
    revoked_event = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == revoked_punch.event_uid)
    )
    assert revoked_event and revoked_event.ords_status == "BLOCKED_IDENTITY"
    assert revoked_event.identity_resolution_id is None


def test_same_employee_resolution_becomes_stale_when_terminal_membership_changes(
    db: Session,
):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="two-members",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"Same Name-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Same Name-{CNIC}"),
            ],
        ),
    )
    report = build_identity_conflict_report(db, zkt=connector.zkt_device)
    group = report["groups"][0]
    resolution = create_same_employee_resolution(
        db,
        zkt=connector.zkt_device,
        group_token=group["group_token"],
        members=[
            (member["user_key"], member["row_version"]) for member in group["members"]
        ],
        reason="Exact current membership was independently reviewed.",
        idempotency_key="stale-membership-resolution",
        actor="StateHealthAdmin",
    )
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="three-members",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"Same Name-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Same Name-{CNIC}"),
                UserSnapshotRow(uid="3", user_id="1003", name=f"Same Name-{CNIC}"),
            ],
        ),
    )
    db.flush()
    assert resolution.status == "STALE"
    assert db.scalar(select(func.count(DeviceUser.id))) == 3
    alert = db.scalar(
        select(DeviceAlert)
        .where(DeviceAlert.code == "DUPLICATE_USER_CNIC", DeviceAlert.state == "OPEN")
        .order_by(DeviceAlert.id.desc())
    )
    assert alert and alert.details == {"affected_users": 3, "duplicate_groups": 1}
    punch = event(
        event_uid="b" * 64,
        user_id="1003",
        uid="3",
        raw_name=f"Same Name-{CNIC}",
    )
    ingest_attendance(db, connector=connector, events=[punch])
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid))
    assert row and row.ords_status == "BLOCKED_IDENTITY"


def test_partial_snapshot_rejects_ambiguous_identity_replacement(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector, uid="1", user_id="1001", name="Original")
    with pytest.raises(ValueError, match="partial user snapshot"):
        replace_user_snapshot(
            db,
            connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id="ambiguous-partial",
                complete=False,
                observed_at=utc_now(),
                users=[UserSnapshotRow(uid="9", user_id="1001", name="Replacement")],
            ),
        )
    original = db.scalar(select(DeviceUser).where(DeviceUser.uid == "1"))
    assert original.present and original.lifecycle_state == "ACTIVE"


def test_unique_full_cnic_is_accepted_even_when_mask_and_other_conflicts_match(
    db: Session,
):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="existing-conflicts",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="1", user_id="1001", name=f"One-{CNIC}"),
                UserSnapshotRow(uid="2", user_id="1002", name=f"Two-{CNIC}"),
            ],
        ),
    )
    make_writable(connector)
    # Both values render with the same four-digit mask (***567-1), but the
    # reservation check is an HMAC of all 13 normalized digits.
    distinct_cnic_with_same_visible_suffix = "6110112345671"
    user, command = create_device_user_command(
        db,
        connector=connector,
        display_name="Unique full CNIC",
        cnic=distinct_cnic_with_same_visible_suffix,
        shift_worker=False,
        user_id_override="1003",
        idempotency_key="unique-despite-same-mask",
        actor="StateHealthAdmin",
    )
    assert user.lifecycle_state == "PENDING"
    assert command.command_type == "CREATE_USER"
    assert decrypt_cnic(user.cnic_encrypted) == distinct_cnic_with_same_visible_suffix
    assert user.cnic_lookup_hash not in {
        row.cnic_lookup_hash
        for row in db.scalars(
            select(DeviceUser).where(DeviceUser.lifecycle_state == "ACTIVE")
        )
    }


def test_malformed_legacy_user_id_uses_exact_terminal_fingerprints_for_safe_edit(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    legacy = snapshot_user(
        db,
        connector,
        uid="287",
        user_id="??",
        name="Jjkjjk",
    )
    with pytest.raises(ValueError, match="malformed bytes"):
        update_device_user_command(
            db,
            connector=connector,
            user=legacy,
            display_name="JunaidK",
            cnic="3450210835219",
            shift_worker=False,
            privilege=0,
            expected_version=legacy.row_version,
            idempotency_key="legacy-edit-before-fingerprint",
            actor="StateHealthAdmin",
        )

    identity_before = "a" * 64
    state_before = "b" * 64
    legacy = snapshot_user(
        db,
        connector,
        uid="287",
        user_id="??",
        name="Jjkjjk",
        terminal_identity_fingerprint=identity_before,
        terminal_state_fingerprint=state_before,
    )
    assert legacy.terminal_identity_fingerprint == identity_before
    assert legacy.terminal_state_fingerprint == state_before

    update = update_device_user_command(
        db,
        connector=connector,
        user=legacy,
        display_name="JunaidK",
        cnic="3450210835219",
        shift_worker=False,
        privilege=0,
        expected_version=legacy.row_version,
        idempotency_key="legacy-edit-with-fingerprint",
        actor="StateHealthAdmin",
    )
    wire = serialize_command(update)
    assert wire["payload"]["uid"] == "287"
    assert wire["payload"]["user_id"] == "??"
    assert wire["expected_state"]["terminal_identity_fingerprint"] == identity_before
    assert wire["expected_state"]["terminal_state_fingerprint"] == state_before
    assert wire["expected_state"]["name"] == "Jjkjjk"

    identity_after = "c" * 64
    state_after = "d" * 64
    attendance_before = db.scalar(select(func.count(AttendanceEvent.id)))
    apply_command_update(
        db,
        connector=connector,
        command_id=update.command_id,
        status="SUCCEEDED",
        result={
            "verified_privilege": 0,
            "verified_terminal_identity_fingerprint": identity_after,
            "verified_terminal_state_fingerprint": state_after,
        },
        error_code=None,
        error_message=None,
    )
    assert legacy.display_name == "JunaidK"
    assert decrypt_cnic(legacy.cnic_encrypted) == "3450210835219"
    assert decrypt_text(legacy.machine_name_encrypted) == "JunaidK-3450210835219"
    assert legacy.terminal_identity_fingerprint == identity_after
    assert legacy.terminal_state_fingerprint == state_after
    assert db.scalar(select(func.count(AttendanceEvent.id))) == attendance_before


def test_user_create_update_delete_is_idempotent_encrypted_and_immutable(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user, create = create_device_user_command(
        db,
        connector=connector,
        display_name="Ayesha Fatima with a deliberately long full name",
        cnic=CNIC,
        shift_worker=True,
        user_id_override="5001",
        idempotency_key="create-user-0001",
        actor="StateHealthAdmin",
    )
    same_user, same_create = create_device_user_command(
        db,
        connector=connector,
        display_name="ignored on replay",
        cnic="6110112345671",
        shift_worker=False,
        user_id_override=None,
        idempotency_key="create-user-0001",
        actor="StateHealthAdmin",
    )
    assert same_user.id == user.id and same_create.id == create.id
    assert user.cnic_encrypted and CNIC not in user.cnic_encrypted
    assert CNIC not in create.payload_summary.values()
    payload = serialize_command(create)["payload"]
    assert len(payload["name"].encode("utf-8")) <= 24
    assert payload["name"].endswith(f"-S-{CNIC}")
    apply_command_update(
        db,
        connector=connector,
        command_id=create.command_id,
        status="SUCCEEDED",
        result={"verified_uid": user.uid, "verified_user_id": user.user_id},
        error_code=None,
        error_message=None,
    )
    assert user.present and user.lifecycle_state == "ACTIVE"

    attendance = event(event_uid="a" * 64, user_id=user.user_id)
    ingest_attendance(db, connector=connector, events=[attendance])
    attendance_count = db.scalar(select(func.count(AttendanceEvent.id)))
    update = update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name="Ayesha Fatima",
        cnic="6110112345671",
        shift_worker=False,
        privilege=14,
        expected_version=user.row_version,
        idempotency_key="update-user-0001",
        actor="StateHealthAdmin",
    )
    assert update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name="ignored",
        cnic=None,
        shift_worker=None,
        privilege=None,
        expected_version=user.row_version,
        idempotency_key="update-user-0001",
        actor="StateHealthAdmin",
    ).id == update.id
    apply_command_update(
        db,
        connector=connector,
        command_id=update.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 14},
        error_code=None,
        error_message=None,
    )
    assert user.display_name == "Ayesha Fatima"
    assert decrypt_cnic(user.cnic_encrypted) == "6110112345671"
    assert user.privilege == 14
    with pytest.raises(ValueError, match="Demote"):
        delete_device_user_command(
            db,
            connector=connector,
            user=user,
            expected_version=user.row_version,
            typed_confirmation=user.display_name,
            idempotency_key="delete-user-blocked",
            actor="StateHealthAdmin",
        )

    demote = update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name=None,
        cnic=None,
        shift_worker=None,
        privilege=0,
        expected_version=user.row_version,
        idempotency_key="demote-user-0001",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=demote.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 0},
        error_code=None,
        error_message=None,
    )
    connector.zkt_device.attendance_count = 42
    delete = delete_device_user_command(
        db,
        connector=connector,
        user=user,
        expected_version=user.row_version,
        typed_confirmation=user.display_name,
        idempotency_key="delete-user-0001",
        actor="StateHealthAdmin",
    )
    expected = decrypt_json(delete.expected_state_encrypted)
    payload = decrypt_json(delete.payload_encrypted)
    assert expected["attendance_count"] == 42
    assert expected["privilege"] == 0
    assert payload["tombstone"] == {
        "display_name": user.display_name,
        "cnic": "6110112345671",
        "shift_worker": False,
    }
    assert "tombstone" not in delete.payload_summary
    assert db.scalar(select(IdentityTombstone).where(IdentityTombstone.device_user_id == user.id))
    apply_command_update(
        db,
        connector=connector,
        command_id=delete.command_id,
        status="SUCCEEDED",
        result={
            "user_absent": True,
            "attendance_count_before": 42,
            "attendance_count_after": 42,
        },
        error_code=None,
        error_message=None,
    )
    assert not user.present and user.lifecycle_state == "DELETED"
    assert db.scalar(select(func.count(AttendanceEvent.id))) == attendance_count
    audit_rows = db.scalars(
        select(AuditEvent).where(AuditEvent.target_id == user.user_key)
    ).all()
    assert audit_rows
    assert all(CNIC not in json.dumps(row.before) + json.dumps(row.after) for row in audit_rows)


def test_delete_postcondition_failure_keeps_user_active(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    connector.zkt_device.attendance_count = 10
    command = delete_device_user_command(
        db,
        connector=connector,
        user=user,
        expected_version=user.row_version,
        typed_confirmation=user.user_id,
        idempotency_key="delete-failure-0001",
        actor="StateHealthAdmin",
    )
    result = apply_command_update(
        db,
        connector=connector,
        command_id=command.command_id,
        status="SUCCEEDED",
        result={
            "user_absent": True,
            "attendance_count_before": 10,
            "attendance_count_after": 9,
        },
        error_code=None,
        error_message=None,
    )
    assert result.status == "FAILED"
    assert result.error_code == "DELETE_POSTCONDITION_FAILED"
    assert user.present and user.lifecycle_state == "ACTIVE"


def test_bulk_user_deletion_job_is_idempotent_sequential_and_preserves_attendance(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    connector.zkt_device.attendance_count = 73
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="bulk-delete-source",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid="11", user_id="1011", name=f"First-{CNIC}"),
                UserSnapshotRow(uid="12", user_id="1012", name="Second-6110112345671"),
            ],
        ),
    )
    users = list(
        db.scalars(
            select(DeviceUser)
            .where(DeviceUser.zkt_device_id == connector.zkt_device.id)
            .order_by(DeviceUser.uid)
        ).all()
    )
    targets = [(user.user_key, user.row_version) for user in users]
    job = create_user_deletion_job(
        db,
        connector=connector,
        targets=targets,
        reason="Approved duplicate terminal cleanup",
        typed_confirmation="DELETE 2 USERS FROM 1",
        idempotency_key="bulk-delete-users-0001",
        actor="StateHealthAdmin",
    )
    replay = create_user_deletion_job(
        db,
        connector=connector,
        targets=targets,
        reason="Approved duplicate terminal cleanup",
        typed_confirmation="DELETE 2 USERS FROM 1",
        idempotency_key="bulk-delete-users-0001",
        actor="StateHealthAdmin",
    )
    assert replay.id == job.id

    advance_user_deletion_jobs(db)
    items = list(
        db.scalars(
            select(UserDeletionItem)
            .where(UserDeletionItem.job_id == job.id)
            .order_by(UserDeletionItem.id)
        ).all()
    )
    assert [item.status for item in items] == ["RUNNING", "PENDING"]
    first_command = items[0].current_command_id
    assert first_command is not None
    apply_command_update(
        db,
        connector=connector,
        command_id=db.get(DeviceCommand, first_command).command_id,
        status="SUCCEEDED",
        result={
            "user_absent": True,
            "attendance_count_before": 73,
            "attendance_count_after": 73,
        },
        error_code=None,
        error_message=None,
    )
    advance_user_deletion_jobs(db)
    assert [item.status for item in items] == ["SUCCEEDED", "RUNNING"]
    second_command = items[1].current_command_id
    assert second_command is not None and second_command != first_command
    apply_command_update(
        db,
        connector=connector,
        command_id=db.get(DeviceCommand, second_command).command_id,
        status="SUCCEEDED",
        result={
            "user_absent": True,
            "attendance_count_before": 73,
            "attendance_count_after": 73,
        },
        error_code=None,
        error_message=None,
    )
    advance_user_deletion_jobs(db)
    summary = serialize_user_deletion_job(db, job)
    assert job.status == "SUCCEEDED"
    assert summary["counts"] == {
        "requested": 2,
        "succeeded": 2,
        "failed": 0,
        "canceled": 0,
        "expired": 0,
        "pending": 0,
    }
    assert connector.zkt_device.attendance_count == 73
    assert all(not user.present and user.lifecycle_state == "DELETED" for user in users)


def test_bulk_user_deletion_cancels_only_untouched_items(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    job = create_user_deletion_job(
        db,
        connector=connector,
        targets=[(user.user_key, user.row_version)],
        reason="Operator withdrew the cleanup request",
        typed_confirmation="DELETE 1 USERS FROM 1",
        idempotency_key="bulk-delete-users-cancel",
        actor="StateHealthAdmin",
    )
    cancel_user_deletion_job(db, job=job, actor="StateHealthAdmin")
    advance_user_deletion_jobs(db)
    item = db.scalar(select(UserDeletionItem).where(UserDeletionItem.job_id == job.id))
    assert job.status == "CANCELED"
    assert item.status == "CANCELED"
    assert user.present and user.lifecycle_state == "ACTIVE"


def test_active_enrollment_lease_blocks_user_mutation(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    db.add(
        TemporaryAdminLease(
            lease_id="active-lease",
            zkt_device_id=connector.zkt_device.id,
            device_user_id=user.id,
            state="ACTIVE",
            original_privilege=0,
        )
    )
    db.flush()
    with pytest.raises(ValueError, match="lease is active"):
        update_device_user_command(
            db,
            connector=connector,
            user=user,
            display_name="Changed",
            cnic=None,
            shift_worker=None,
            privilege=None,
            expected_version=user.row_version,
            idempotency_key="blocked-by-lease",
            actor="StateHealthAdmin",
        )


def test_missing_identity_is_unblocked_but_acked_attendance_is_immutable(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name="Identity Missing")
    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="b" * 64, raw_name="Identity Missing")],
    )
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == "b" * 64))
    assert row.ords_status == "BLOCKED_IDENTITY"
    command = update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name=None,
        cnic=CNIC,
        shift_worker=None,
        privilege=None,
        expected_version=user.row_version,
        idempotency_key="enrich-identity-0001",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=command.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 0},
        error_code=None,
        error_message=None,
    )
    assert row.ords_status == "PENDING"
    assert decrypt_cnic(row.cnic_encrypted) == CNIC
    outbox = db.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id))
    assert outbox
    row.ords_status = "FAILED_RETRYABLE"
    outbox.status = "FAILED_RETRYABLE"
    outbox.next_attempt_at = utc_now() + timedelta(minutes=10)
    outbox.last_http_status = 503
    outbox.last_error = "HTTP_503"
    second = update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name=None,
        cnic="6110112345671",
        shift_worker=None,
        privilege=None,
        expected_version=user.row_version,
        idempotency_key="enrich-identity-0002",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=second.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 0},
        error_code=None,
        error_message=None,
    )
    assert row.ords_status == "PENDING"
    assert decrypt_cnic(row.cnic_encrypted) == "6110112345671"
    assert outbox.status == "PENDING"
    assert outbox.next_attempt_at is None
    assert outbox.last_http_status is None
    assert outbox.last_error is None

    row.ords_status = "ACKED"
    outbox.status = "ACKED"
    original_encrypted = row.cnic_encrypted
    third = update_device_user_command(
        db,
        connector=connector,
        user=user,
        display_name=None,
        cnic="3520212345671",
        shift_worker=None,
        privilege=None,
        expected_version=user.row_version,
        idempotency_key="enrich-identity-0003",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=third.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 0},
        error_code=None,
        error_message=None,
    )
    assert row.cnic_encrypted == original_encrypted
    assert decrypt_cnic(row.cnic_encrypted) == "6110112345671"


def test_deleted_identity_tombstone_attributes_later_punches(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector)
    from zk_add.service import persist_identity_tombstone

    persist_identity_tombstone(db, zkt=connector.zkt_device, user=user)
    user.present = False
    user.lifecycle_state = "DELETED"
    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="c" * 64, raw_name="")],
    )
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == "c" * 64))
    assert row.device_user_id == user.id
    assert row.display_name == user.display_name
    assert decrypt_cnic(row.cnic_encrypted) == CNIC


def test_reused_terminal_user_id_never_inherits_active_or_tombstoned_identity(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    active = snapshot_user(db, connector)
    from zk_add.service import persist_identity_tombstone

    persist_identity_tombstone(db, zkt=connector.zkt_device, user=active)
    ingest_attendance(
        db,
        connector=connector,
        events=[
            event(
                event_uid="4" * 64,
                user_id=active.user_id,
                uid="999",
                raw_name="Different Employee",
            )
        ],
    )
    row = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "4" * 64)
    )
    assert row is not None
    assert row.device_user_id is None
    assert row.cnic_encrypted is None
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert row.identity_resolution_status == "BLOCKED_IDENTITY"


def test_tombstone_lookup_fails_closed_when_exact_user_id_uid_is_ambiguous(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    first = snapshot_user(db, connector)
    from zk_add.service import persist_identity_tombstone

    persist_identity_tombstone(db, zkt=connector.zkt_device, user=first)
    first.present = False
    first.lifecycle_state = "DELETED"
    second = DeviceUser(
        zkt_device_id=connector.zkt_device.id,
        uid=first.uid,
        user_id=first.user_id,
        display_name=first.display_name,
        cnic_encrypted=first.cnic_encrypted,
        cnic_lookup_hash=first.cnic_lookup_hash,
        cnic_last4=first.cnic_last4,
        present=False,
        lifecycle_state="DELETED",
        source="HR_DIRECTORY_EVIDENCE",
        observed_at=utc_now(),
    )
    db.add(second)
    db.flush()
    persist_identity_tombstone(db, zkt=connector.zkt_device, user=second)

    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="5" * 64, raw_name="")],
    )
    row = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "5" * 64)
    )
    assert row is not None
    assert row.device_user_id is None
    assert row.cnic_encrypted is None
    assert row.ords_status == "BLOCKED_IDENTITY"


def test_heartbeat_tracks_flapping_and_waits_without_mutating(db: Session):
    connector = connector_fixture(db)
    payload = HeartbeatPayload(
        firmware_version="2.1.0",
        zkt={
            "online": False,
            "connection_state": "FLAPPING",
            "serial": SERIAL,
            "ip_address": "192.168.110.137",
            "model": "MB20/ID",
            "platform": "ZLM60_TFT",
            "consecutive_failures": 3,
            "flap_count_15m": 4,
            "user_record_size": 72,
            "backoff_until": (utc_now() + timedelta(minutes=2)).isoformat(),
        },
    )
    result = update_heartbeat(
        db, connector=connector, boot_id="boot-1", sequence=1, payload=payload
    )
    assert result["state"] == "FLAPPING"
    transition = db.scalar(select(DeviceConnectionEvent))
    assert transition and transition.to_state == "FLAPPING"
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ZKT_CONNECTION_FLAPPING"))
    assert alert and alert.severity == "WARNING"


def test_planned_truth_session_refresh_does_not_degrade_or_count_as_flapping(db: Session):
    connector = connector_fixture(db)
    connector.lifecycle_state = "ONLINE"
    connector.zkt_device.connection_state = "ONLINE"
    connector.zkt_device.online = True
    connector.zkt_device.flap_count_15m = 2
    db.flush()

    payload = HeartbeatPayload(
        firmware_version="2.2.40",
        current_activity="SESSION_REFRESH",
        zkt={
            "online": False,
            "connection_state": "SESSION_REFRESH",
            "serial": SERIAL,
            "ip_address": "192.168.110.137",
            "model": "MB20/ID",
            "platform": "ZLM60_TFT",
            "consecutive_failures": 0,
            "consecutive_successes": 3,
            "flap_count_15m": 2,
            "user_record_size": 72,
        },
    )

    result = update_heartbeat(
        db, connector=connector, boot_id="boot-refresh", sequence=1, payload=payload
    )

    assert result["state"] == "ONLINE"
    assert connector.lifecycle_state == "ONLINE"
    assert connector.zkt_device.connection_state == "SESSION_REFRESH"
    assert connector.zkt_device.online is False
    assert connector.zkt_device.flap_count_15m == 2
    assert connector.zkt_device.offline_since is None
    assert (
        db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ZKT_CONNECTION_FLAPPING"))
        is None
    )


def test_heartbeat_surfaces_fail_closed_historical_backfill_state(db: Session):
    connector = connector_fixture(db)
    payload = HeartbeatPayload(
        firmware_version="2.2.12",
        zkt={
            "online": True,
            "connection_state": "ONLINE",
            "serial": SERIAL,
            "history_backfill": {
                "state": "BLOCKED",
                "coverage_start_month": "2023-04",
                "cursor_month": "2025-11",
                "failed_windows": 2,
            },
        },
    )

    result = update_heartbeat(
        db, connector=connector, boot_id="history-boot", sequence=1, payload=payload
    )

    capabilities = result["zkt"]["capabilities"]
    assert capabilities["history_backfill_state"] == "BLOCKED"
    assert capabilities["history_coverage_start_month"] == "2023-04"
    assert capabilities["history_cursor_month"] == "2025-11"
    assert capabilities["history_failed_windows"] == 2
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "HISTORY_BACKFILL_BLOCKED"))
    assert alert and alert.severity == "HIGH"

    payload.zkt["history_backfill"] = {
        "state": "COMPLETE",
        "coverage_start_month": "2023-04",
        "cursor_month": "2026-07",
        "failed_windows": 0,
    }
    update_heartbeat(
        db, connector=connector, boot_id="history-boot", sequence=2, payload=payload
    )
    assert alert.state == "RESOLVED"


def test_heartbeat_surfaces_and_resolves_real_local_led_failure(db: Session):
    connector = connector_fixture(db)
    payload = HeartbeatPayload(
        firmware_version="2.2.40",
        led_state="LOCAL_FAILURE",
        zkt={
            "online": True,
            "connection_state": "ONLINE",
            "serial": SERIAL,
        },
    )

    result = update_heartbeat(
        db, connector=connector, boot_id="local-fault", sequence=1, payload=payload
    )
    assert result["state"] == "DEGRADED"
    assert connector.last_error_code == "ESP_LOCAL_FAILURE"
    alert = db.scalar(
        select(DeviceAlert).where(DeviceAlert.code == "ESP_LOCAL_FAILURE")
    )
    assert alert and alert.state == "OPEN" and alert.severity == "HIGH"

    payload.led_state = "HEALTHY"
    result = update_heartbeat(
        db, connector=connector, boot_id="local-fault", sequence=2, payload=payload
    )
    assert result["state"] == "ONLINE"
    assert connector.last_error_code is None
    assert alert.state == "RESOLVED"


def test_admin_lease_result_is_durable(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name=f"Ayesha-S-{CNIC}")
    lease, command = create_admin_lease(
        db,
        connector=connector,
        user=user,
        idempotency_key="lease-test-0001",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=command.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 14, "expires_epoch": 1_900_000_000},
        error_code=None,
        error_message=None,
    )
    assert lease.state == "ACTIVE"
    expires_at = lease.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert int(expires_at.timestamp()) == 1_900_000_000


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "CANCELED", "EXPIRED"])
def test_admin_lease_terminal_grant_does_not_remain_active(
    db: Session, status: str
):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name=f"Ayesha-S-{CNIC}")
    lease, command = create_admin_lease(
        db,
        connector=connector,
        user=user,
        idempotency_key=f"lease-terminal-{status.lower()}",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=command.command_id,
        status=status,
        result={},
        error_code="GRANT_NOT_COMPLETED",
        error_message=None,
    )
    assert lease.state == "FAILED"
    assert lease.last_error == "GRANT_NOT_COMPLETED"


def test_stale_granting_lease_is_repaired_from_terminal_command(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name=f"Ayesha-S-{CNIC}")
    lease, command = create_admin_lease(
        db,
        connector=connector,
        user=user,
        idempotency_key="lease-stale-granting",
        actor="StateHealthAdmin",
    )
    command.status = "EXPIRED"
    command.completed_at = utc_now()
    db.flush()

    assert lease.state == "GRANTING"
    assert reconcile_admin_lease_states(db) == 1
    assert lease.state == "FAILED"
    assert "expired" in (lease.last_error or "")


def test_cancelled_revoke_is_retried_instead_of_sticking(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name=f"Ayesha-S-{CNIC}")
    lease, grant = create_admin_lease(
        db,
        connector=connector,
        user=user,
        idempotency_key="lease-revoke-retry",
        actor="StateHealthAdmin",
    )
    apply_command_update(
        db,
        connector=connector,
        command_id=grant.command_id,
        status="SUCCEEDED",
        result={"verified_privilege": 14, "expires_epoch": 1},
        error_code=None,
        error_message=None,
    )
    first_revoke = worker.queue_due_revokes(db)[0]
    first_revoke.status = "CANCELLED"
    lease.state = "OVERDUE"
    db.flush()

    retries = worker.queue_due_revokes(db)
    assert len(retries) == 1
    assert retries[0].id != first_revoke.id
    assert lease.state == "REVOKING"


def test_step_up_authentication_rejects_wrong_password(db: Session):
    _raw, row = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    context = AdminContext(row.id, row.username, row.csrf_token, row.last_step_up_at)
    require_step_up("correct-password", db, context)
    with pytest.raises(HTTPException) as exc:
        require_step_up("wrong-password", db, context)
    assert exc.value.status_code == 403


def test_cnic_validation_machine_projection_and_browser_secrecy():
    with pytest.raises(ValidationError):
        UserCreateRequest.model_validate(
            {
                "display_name": "User",
                "cnic": "123",
                "password": "x",
                "idempotency_key": "create-0001",
            }
        )
    with pytest.raises(ValidationError):
        UserUpdateRequest.model_validate(
            {
                "expected_version": 1,
                "password": "x",
                "idempotency_key": "update-0001",
            }
        )
    projected = build_machine_name(
        display_name="زارا State Life Employee",
        cnic=CNIC,
        shift_worker=True,
        byte_limit=24,
    )
    assert len(projected.encode("utf-8")) <= 24
    assert projected.endswith(f"-S-{CNIC}")


def test_attendance_ingestion_is_idempotent_sanitized_and_ords_is_ephemeral(db: Session):
    connector = connector_fixture(db)
    incoming = event(event_uid="e" * 64, raw_name=f"Ayesha-{CNIC}")
    incoming.raw_event["nested"] = {
        "cnic": CNIC,
        "wifi_password": "must-not-persist",
        "rows": [{"raw_name": f"Ayesha-{CNIC}"}],
    }
    accepted, duplicates = ingest_attendance(
        db, connector=connector, events=[incoming, incoming]
    )
    assert accepted == [incoming.event_uid]
    assert duplicates == [incoming.event_uid]
    row = db.scalar(select(AttendanceEvent))
    assert row.raw_event["raw_name"] == "[REDACTED]"
    assert row.raw_event["nested"] == {
        "cnic": "[REDACTED]",
        "wifi_password": "[REDACTED]",
        "rows": [{"raw_name": "[REDACTED]"}],
    }
    outbox = db.scalar(select(OrdsOutbox))
    assert outbox and not hasattr(outbox, "payload")
    payload = oracle_payload(connector, connector.zkt_device, row, CNIC)
    assert payload["cnic"] == CNIC
    assert payload["capturetype"] == "LIVE"


def test_impossible_device_time_is_preserved_but_never_queued_as_attendance(
    db: Session,
):
    connector = connector_fixture(db)
    incoming = event(event_uid="0" * 64, raw_name=f"Ayesha-{CNIC}")
    incoming.device_event_time = datetime(2001, 1, 1, tzinfo=timezone.utc)

    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[incoming],
    )
    db.flush()

    assert accepted == [incoming.event_uid]
    assert duplicates == []
    row = db.scalar(
        select(AttendanceEvent).where(
            AttendanceEvent.event_uid == incoming.event_uid
        )
    )
    assert row is not None
    assert row.ords_status == "QUARANTINED_INVALID_DEVICE_TIME"
    assert row.clock_quality == "INVALID"
    assert db.scalar(select(OrdsOutbox)) is None
    alert = db.scalar(
        select(DeviceAlert).where(
            DeviceAlert.connector_id == connector.id,
            DeviceAlert.code == "ATTENDANCE_TIMESTAMP_QUARANTINED",
        )
    )
    assert alert is not None


def test_attendance_batch_uses_bounded_flushes_and_duplicate_retry_uses_none(
    db: Session,
):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    incoming = [
        event(event_uid=f"{index:064x}")
        for index in range(1, 101)
    ]
    flush_count = 0

    def count_flush(*_args):
        nonlocal flush_count
        flush_count += 1

    sqlalchemy_event.listen(db, "before_flush", count_flush)
    try:
        accepted, duplicates = ingest_attendance(
            db,
            connector=connector,
            events=incoming,
        )
        first_flush_count = flush_count
        retry_accepted, retry_duplicates = ingest_attendance(
            db,
            connector=connector,
            events=incoming,
        )
    finally:
        sqlalchemy_event.remove(db, "before_flush", count_flush)

    assert len(accepted) == 100
    assert duplicates == []
    assert first_flush_count == 2
    assert retry_accepted == []
    assert retry_duplicates == [row.event_uid for row in incoming]
    assert flush_count == first_flush_count
    assert db.scalar(
        select(func.count()).select_from(AttendanceEvent)
    ) == 100
    assert db.scalar(
        select(func.count()).select_from(OrdsOutbox)
    ) == 100


def test_attendance_rejects_corrupt_event_uids_and_ords_conflicts_are_idempotent():
    assert event_uid_is_valid("a" * 64)
    assert not event_uid_is_valid("a" * 31 + "?" + "b" * 32)
    with pytest.raises(ValidationError):
        AttendanceBatchRequest.model_validate(
            {
                "batch_id": "corrupt-batch",
                "events": [
                    {
                        "event_uid": "a" * 31 + "?" + "b" * 32,
                        "user_id": "1",
                        "device_event_time": "2026-07-13T13:00:00Z",
                        "captured_at": "2026-07-13T13:00:01Z",
                        "source": "LIVE",
                    }
                ],
            }
        )
    assert ords_delivery_succeeded(409, {"message": "resource already exists"})
    assert ords_delivery_succeeded(201, {"success": True})
    assert not ords_delivery_succeeded(400, {"success": False})


def test_oracle_receipts_are_durable_idempotent_and_order_independent(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    observed_at = utc_now()
    before_event = OracleReceiptBatchRequest(
        confirmation_path="FIRMWARE_RECONCILE",
        oracle_observed_at=observed_at,
        event_uids=["7" * 64],
    )
    applied, awaiting, rejected = record_oracle_receipts(
        db,
        connector=connector,
        batch=before_event,
    )
    assert (applied, awaiting, rejected) == (0, 1, 0)
    receipt = db.scalar(
        select(OracleReceipt).where(OracleReceipt.event_uid == "7" * 64)
    )
    assert receipt and receipt.attendance_event_id is None

    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="7" * 64)],
    )
    first = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "7" * 64)
    )
    first_outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == first.id)
    )
    assert first.ords_status == "FIRMWARE_RECEIPT_UNVERIFIED"
    assert first.oracle_confirmation_path is None
    assert first.oracle_confirmed_at is None
    assert first_outbox.status == "FIRMWARE_RECEIPT_UNVERIFIED"
    assert first_outbox.acknowledged_at is None
    assert receipt.attendance_event_id == first.id

    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid="8" * 64)],
    )
    second = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == "8" * 64)
    )
    assert second.ords_status == "PENDING"
    applied, awaiting, rejected = record_oracle_receipts(
        db,
        connector=connector,
        batch=OracleReceiptBatchRequest(
            confirmation_path="FIRMWARE_LIVE",
            oracle_observed_at=observed_at,
            event_uids=["8" * 64],
        ),
    )
    assert (applied, awaiting, rejected) == (1, 0, 0)
    assert second.ords_status == "FIRMWARE_RECEIPT_UNVERIFIED"
    assert second.oracle_confirmation_path is None
    assert second.oracle_confirmed_at is None

    record_oracle_receipts(
        db,
        connector=connector,
        batch=before_event,
    )
    assert receipt.observation_count == 2
    metrics = ords_delivery_metrics(db)
    assert metrics["backlog"] == 2
    assert metrics["acknowledged"] == 0
    assert metrics["acknowledged_firmware"] == 0
    assert metrics["firmware_unverified"] == 2


def test_firmware_receipts_require_oracle_proof_and_missing_events_are_requeued(
    db: Session,
):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    observed_at = utc_now()
    event_uids = ["a" * 64, "b" * 64]
    ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid=event_uid) for event_uid in event_uids],
    )
    record_oracle_receipts(
        db,
        connector=connector,
        batch=OracleReceiptBatchRequest(
            confirmation_path="FIRMWARE_RECONCILE",
            oracle_observed_at=observed_at,
            event_uids=event_uids,
        ),
    )
    rows = db.scalars(
        select(AttendanceEvent).order_by(AttendanceEvent.event_uid.asc())
    ).all()
    outboxes = db.scalars(
        select(OrdsOutbox).order_by(OrdsOutbox.id.asc())
    ).all()
    for row, outbox in zip(rows, outboxes, strict=True):
        row.ords_status = "FIRMWARE_RECEIPT_VERIFYING"
        outbox.status = "FIRMWARE_RECEIPT_VERIFYING"
        outbox.attempt_count = 1

    apply_firmware_receipt_missing(db, claimed_id=outboxes[0].id)
    apply_ords_confirmation(
        db,
        claimed_id=outboxes[1].id,
        path="FIRMWARE_RECEIPT_MEMBERSHIP_CHECK",
    )

    assert outboxes[0].status == "PENDING"
    assert outboxes[0].next_attempt_at is not None
    assert outboxes[0].last_error == "FIRMWARE_RECEIPT_NOT_IN_ORACLE"
    assert rows[0].ords_status == "PENDING"
    assert rows[0].oracle_confirmed_at is None
    assert rows[0].oracle_confirmation_path is None
    assert outboxes[1].status == "ACKED_CHECK"
    assert rows[1].ords_status == "ACKED_CHECK"
    assert rows[1].oracle_confirmation_path == (
        "FIRMWARE_RECEIPT_MEMBERSHIP_CHECK"
    )
    assert rows[1].oracle_confirmed_at is not None
    metrics = ords_delivery_metrics(db)
    assert metrics["backlog"] == 1
    assert metrics["acknowledged"] == 1
    assert metrics["firmware_unverified"] == 0


def test_legacy_firmware_ack_is_claimed_for_bounded_membership_audit(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    event_uid = "d" * 64
    ingest_attendance(db, connector=connector, events=[event(event_uid=event_uid)])
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
    )
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    )
    attendance.ords_status = "ACKED_FIRMWARE"
    outbox.status = "ACKED_FIRMWARE"
    outbox.acknowledged_at = utc_now()
    db.commit()

    previous_bind = SessionLocal.kw.get("bind")
    SessionLocal.configure(bind=db.get_bind())
    try:
        claims = claim_firmware_receipt_audit_batch(limit=1)
        assert claims == [(outbox.id, event_uid, connector.id)]
        with SessionLocal() as verification:
            claimed_outbox = verification.get(OrdsOutbox, outbox.id)
            claimed_event = verification.get(AttendanceEvent, attendance.id)
            assert claimed_outbox.status == "FIRMWARE_RECEIPT_VERIFYING"
            assert claimed_outbox.attempt_count == 1
            assert claimed_outbox.last_attempt_at is not None
            assert claimed_event.ords_status == "FIRMWARE_RECEIPT_VERIFYING"
    finally:
        SessionLocal.configure(bind=previous_bind)


def test_firmware_receipt_audit_failure_stays_unverified_and_late_receipt_cannot_downgrade_direct_proof(
    db: Session,
):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    event_uid = "c" * 64
    ingest_attendance(db, connector=connector, events=[event(event_uid=event_uid)])
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
    )
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    )
    outbox.status = "FIRMWARE_RECEIPT_VERIFYING"
    outbox.attempt_count = 1
    attendance.ords_status = "FIRMWARE_RECEIPT_VERIFYING"
    apply_firmware_receipt_audit_failure(
        db,
        claimed_id=outbox.id,
        status=None,
        transport_error="ConnectTimeout",
        response_parsed=False,
    )
    assert outbox.status == "FIRMWARE_RECEIPT_UNVERIFIED"
    assert outbox.last_error == "FIRMWARE_CHECK_TRANSPORT_CONNECTTIMEOUT"
    assert outbox.next_attempt_at is not None
    assert attendance.ords_status == "FIRMWARE_RECEIPT_UNVERIFIED"

    apply_ords_delivery_result(
        db,
        claimed_id=outbox.id,
        status=201,
        body={"success": True},
        transport_error=None,
        response_parsed=True,
    )
    direct_confirmed_at = attendance.oracle_confirmed_at
    record_oracle_receipts(
        db,
        connector=connector,
        batch=OracleReceiptBatchRequest(
            confirmation_path="FIRMWARE_LIVE",
            oracle_observed_at=utc_now(),
            event_uids=[event_uid],
        ),
    )
    assert outbox.status == "ACKED"
    assert attendance.ords_status == "ACKED"
    assert attendance.oracle_confirmation_path == "ADD_DELIVERY"
    assert attendance.oracle_confirmed_at == direct_confirmed_at


def test_confirmed_events_are_periodically_reverified_and_requeued_if_missing(
    db: Session,
):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    event_uid = "e" * 64
    ingest_attendance(db, connector=connector, events=[event(event_uid=event_uid)])
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
    )
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    )
    old_confirmation = utc_now() - timedelta(days=2)
    attendance.ords_status = "ACKED"
    attendance.oracle_confirmed_at = old_confirmation
    attendance.oracle_confirmation_path = "ADD_DELIVERY"
    outbox.status = "ACKED"
    outbox.acknowledged_at = old_confirmation
    outbox.last_attempt_at = old_confirmation
    db.commit()

    previous_bind = SessionLocal.kw.get("bind")
    previous_interval = settings.ords_membership_reverify_seconds
    SessionLocal.configure(bind=db.get_bind())
    settings.ords_membership_reverify_seconds = 60
    try:
        claims = claim_confirmed_membership_audit_batch(limit=1)
        assert claims == [(outbox.id, event_uid, connector.id)]
        with SessionLocal() as verification:
            claimed_outbox = verification.get(OrdsOutbox, outbox.id)
            claimed_event = verification.get(AttendanceEvent, attendance.id)
            assert claimed_outbox.status == "MEMBERSHIP_REVERIFYING"
            assert claimed_event.ords_status == "MEMBERSHIP_REVERIFYING"
            assert ensure_utc(claimed_event.oracle_confirmed_at) == ensure_utc(
                old_confirmation
            )
    finally:
        settings.ords_membership_reverify_seconds = previous_interval
        SessionLocal.configure(bind=previous_bind)

    db.expire_all()
    apply_confirmed_membership_missing(db, claimed_id=outbox.id)
    missing_outbox = db.get(OrdsOutbox, outbox.id)
    missing_event = db.get(AttendanceEvent, attendance.id)
    assert missing_outbox.status == "PENDING"
    assert missing_outbox.last_error == "CONFIRMED_EVENT_MISSING_FROM_ORACLE"
    assert missing_event.ords_status == "PENDING"
    assert missing_event.oracle_confirmed_at is None
    assert missing_event.oracle_confirmation_path is None


def test_periodic_membership_transport_failure_preserves_last_known_proof(
    db: Session,
):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    event_uid = "f" * 64
    ingest_attendance(db, connector=connector, events=[event(event_uid=event_uid)])
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
    )
    outbox = db.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == attendance.id)
    )
    confirmed_at = utc_now() - timedelta(days=1)
    attendance.ords_status = "MEMBERSHIP_REVERIFYING"
    attendance.oracle_confirmed_at = confirmed_at
    attendance.oracle_confirmation_path = "ADD_DELIVERY"
    outbox.status = "MEMBERSHIP_REVERIFYING"
    outbox.attempt_count = 2

    apply_confirmed_membership_audit_failure(
        db,
        claimed_id=outbox.id,
        status=None,
        transport_error="ConnectTimeout",
        response_parsed=False,
    )

    assert outbox.status == "MEMBERSHIP_REVERIFY_RETRY"
    assert outbox.last_error == "REVERIFY_TRANSPORT_CONNECTTIMEOUT"
    assert outbox.next_attempt_at is not None
    assert attendance.ords_status == "MEMBERSHIP_REVERIFY_RETRY"
    assert ensure_utc(attendance.oracle_confirmed_at) == ensure_utc(confirmed_at)
    assert attendance.oracle_confirmation_path == "ADD_DELIVERY"
    metrics = ords_delivery_metrics(db)
    assert metrics["backlog"] == 1
    assert metrics["membership_reverify"] == 1
    assert metrics["acknowledged"] == 0


def test_oracle_receipt_batch_uses_one_flush_for_one_hundred_events(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    event_uids = [f"{index:064x}" for index in range(1, 101)]
    accepted, duplicates = ingest_attendance(
        db,
        connector=connector,
        events=[event(event_uid=event_uid) for event_uid in event_uids],
    )
    assert len(accepted) == 100
    assert duplicates == []
    db.flush()

    flush_count = 0

    def count_flush(*_args):
        nonlocal flush_count
        flush_count += 1

    sqlalchemy_event.listen(db, "before_flush", count_flush)
    try:
        result = record_oracle_receipts(
            db,
            connector=connector,
            batch=OracleReceiptBatchRequest(
                confirmation_path="FIRMWARE_RECONCILE",
                oracle_observed_at=utc_now(),
                event_uids=event_uids,
            ),
        )
    finally:
        sqlalchemy_event.remove(db, "before_flush", count_flush)

    assert result == (100, 0, 0)
    assert flush_count == 1
    assert db.scalar(
        select(func.count()).select_from(OracleReceipt)
    ) == 100
    assert db.scalar(
        select(func.count())
        .select_from(AttendanceEvent)
        .where(AttendanceEvent.ords_status == "FIRMWARE_RECEIPT_UNVERIFIED")
    ) == 100


def test_oracle_receipt_batch_cannot_poison_another_connectors_event(db: Session):
    owner = connector_fixture(db)
    snapshot_user(db, owner)
    event_uid = "9" * 64
    ingest_attendance(db, connector=owner, events=[event(event_uid=event_uid)])
    reporter = connector_fixture(
        db,
        hardware_id="e0:72:a1:d6:f3:29",
        expected_serial="PESHAWAR-OTHER-TERMINAL",
    )
    db.flush()

    result = record_oracle_receipts(
        db,
        connector=reporter,
        batch=OracleReceiptBatchRequest(
            confirmation_path="FIRMWARE_RECONCILE",
            oracle_observed_at=utc_now(),
            event_uids=[event_uid],
        ),
    )

    assert result == (0, 0, 1)
    assert db.scalar(
        select(OracleReceipt).where(OracleReceipt.event_uid == event_uid)
    ) is None
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
    )
    assert attendance.ords_status == "PENDING"
    assert db.scalar(
        select(DeviceAlert).where(
            DeviceAlert.connector_id == reporter.id,
            DeviceAlert.code == "ORACLE_RECEIPT_CONNECTOR_MISMATCH",
            DeviceAlert.state == "OPEN",
        )
    )


def test_ords_membership_response_is_fail_closed():
    requested = {"a" * 64, "b" * 64}
    assert ords_membership_missing(
        200,
        {
            "success": True,
            "received_count": 2,
            "existing_count": 1,
            "missing_count": 1,
            "missing_event_uids": ["b" * 64],
        },
        requested,
    ) == {"b" * 64}
    assert (
        ords_membership_missing(
            200,
            {
                "success": True,
                "received_count": 2,
                "missing_count": 1,
                "missing_event_uids": ["c" * 64],
            },
            requested,
        )
        is None
    )
    assert (
        ords_membership_missing(
            200,
            {
                "success": True,
                "received_count": 2,
                "existing_count": 2,
                "missing_count": 1,
                "missing_event_uids": ["b" * 64],
            },
            requested,
        )
        is None
    )
    assert ords_membership_missing(503, {"success": False}, requested) is None


def test_ords_delivery_quarantines_poison_rows_and_redacts_failures(db: Session):
    connector = connector_fixture(db)
    snapshot_user(db, connector)
    pending_alert = upsert_alert(
        db,
        connector,
        code="SAME_TRANSACTION_DEDUP_TEST",
        severity="WARNING",
        message="First observation",
    )
    repeated_alert = upsert_alert(
        db,
        connector,
        code="SAME_TRANSACTION_DEDUP_TEST",
        severity="WARNING",
        message="Second observation",
    )
    assert repeated_alert is pending_alert
    first = event(event_uid="1" * 64)
    second = event(event_uid="2" * 64)
    third = event(event_uid="3" * 64)
    fourth = event(event_uid="4" * 64)
    ingest_attendance(db, connector=connector, events=[first, second, third, fourth])
    db.flush()
    outboxes = db.scalars(select(OrdsOutbox).order_by(OrdsOutbox.id.asc())).all()
    assert len(outboxes) == 4
    for outbox in outboxes:
        outbox.status = "IN_FLIGHT"
        outbox.attempt_count = 1

    apply_ords_delivery_result(
        db,
        claimed_id=outboxes[0].id,
        status=400,
        body={"success": False, "message": f"Rejected {CNIC}"},
        transport_error=None,
        response_parsed=True,
    )
    assert outboxes[0].status == "QUARANTINED_ORDS_REJECTED"
    assert outboxes[0].last_error == "HTTP_400"
    assert CNIC not in outboxes[0].last_error
    assert db.scalar(
        select(DeviceAlert).where(DeviceAlert.code == "ORDS_EVENT_REJECTED")
    )

    apply_ords_delivery_result(
        db,
        claimed_id=outboxes[1].id,
        status=503,
        body={"success": False, "message": f"Retry {CNIC}"},
        transport_error=None,
        response_parsed=True,
    )
    assert outboxes[1].status == "FAILED_RETRYABLE"
    assert outboxes[1].last_error == "HTTP_503"
    assert CNIC not in outboxes[1].last_error
    apply_ords_delivery_result(
        db,
        claimed_id=outboxes[3].id,
        status=503,
        body={"success": False, "message": f"Retry another {CNIC}"},
        transport_error=None,
        response_parsed=True,
    )
    retry_alerts = db.scalars(
        select(DeviceAlert).where(DeviceAlert.code == "ORDS_DELIVERY_FAILED")
    ).all()
    assert len(retry_alerts) == 1

    blocked_event = db.get(AttendanceEvent, outboxes[2].attendance_event_id)
    assert blocked_event
    blocked_event.ords_status = "BLOCKED_IDENTITY"
    db.delete(outboxes[2])
    db.flush()

    metrics = ords_delivery_metrics(db)
    assert metrics["backlog"] == 3
    assert metrics["retrying"] == 2
    assert metrics["blocked_identity"] == 1
    assert metrics["quarantined"] == 1
    assert ords_failure_is_permanent(400)
    assert ords_failure_is_permanent(422)
    assert not ords_failure_is_permanent(401)
    assert ords_failure_category(
        None, transport_error="Read Timeout: secret", response_parsed=False
    ) == "TRANSPORT_ERROR"


def test_raw_machine_name_is_encrypted_at_rest(db: Session):
    connector = connector_fixture(db)
    user = snapshot_user(db, connector, name=f"Ayesha-{CNIC}")
    assert user.machine_name_encrypted
    assert CNIC not in user.machine_name_encrypted
    assert decrypt_text(user.machine_name_encrypted) == f"Ayesha-{CNIC}"


def test_stable_snapshot_clears_removed_cnic_and_repairs_only_verified_pending_rows(
    db: Session,
):
    connector = connector_fixture(db)
    user = snapshot_user(db, connector, name=f"Asadisb-{CNIC}")
    punch = event(event_uid="d" * 64, user_id=user.user_id)
    ingest_attendance(db, connector=connector, events=[punch])
    db.flush()
    attendance = db.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == punch.event_uid)
    )
    assert attendance and decrypt_cnic(attendance.cnic_encrypted) == CNIC

    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="asad-cnic-removed",
            observed_at=utc_now(),
            users=[UserSnapshotRow(uid=user.uid, user_id=user.user_id, name="Asadisb")],
        ),
    )
    db.flush()
    assert user.cnic_encrypted is None
    assert attendance.cnic_encrypted is None
    assert attendance.ords_status == "BLOCKED_IDENTITY"
    assert attendance.identity_resolution_status == "BLOCKED_MALFORMED_IDENTITY"

    replacement = "6110112009989"
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="asad-cnic-restored",
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=user.uid,
                    user_id=user.user_id,
                    name=f"Asadisb-{replacement}",
                )
            ],
        ),
    )
    db.flush()
    assert decrypt_cnic(attendance.cnic_encrypted) == replacement
    assert attendance.ords_status == "PENDING"
    assert attendance.identity_resolution_status == "RESOLVED"
    assert attendance.identity_repair_reason == "VERIFIED_TERMINAL_SNAPSHOT"


def test_snapshot_revision_detects_same_count_valid_to_valid_identity_change(db: Session):
    connector = connector_fixture(db)
    user = snapshot_user(db, connector, name=f"Drbilalmeh-{CNIC}")
    first_revision = connector.zkt_device.identity_snapshot_revision
    replacement = "1560703548395"
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="same-count-valid-change",
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=user.uid,
                    user_id=user.user_id,
                    name=f"Drbilalmeh-{replacement}",
                )
            ],
        ),
    )
    assert connector.zkt_device.user_count == 1
    assert connector.zkt_device.identity_snapshot_revision == first_revision + 1
    assert connector.zkt_device.identity_snapshot_stable is True
    assert decrypt_cnic(user.cnic_encrypted) == replacement


def test_identical_terminal_reread_does_not_invalidate_selected_user_version(
    db: Session,
):
    connector = connector_fixture(db)
    make_writable(connector)
    identity_fingerprint = "a" * 64
    state_fingerprint = "b" * 64
    user = snapshot_user(
        db,
        connector,
        name=f"Stable User-{CNIC}",
        terminal_identity_fingerprint=identity_fingerprint,
        terminal_state_fingerprint=state_fingerprint,
    )
    selected_version = user.row_version

    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="harmless-reread",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=user.uid,
                    user_id=user.user_id,
                    name=f"Stable User-{CNIC}",
                    privilege=0,
                    terminal_identity_fingerprint=identity_fingerprint,
                    terminal_state_fingerprint=state_fingerprint,
                )
            ],
        ),
    )
    assert user.row_version == selected_version

    job = create_user_deletion_job(
        db,
        connector=connector,
        targets=[(user.user_key, selected_version)],
        reason="Remove a confirmed obsolete terminal account",
        typed_confirmation="DELETE 1 USERS FROM 1",
        idempotency_key="stable-selection-after-reread",
        actor="StateHealthAdmin",
    )
    assert job.status == "QUEUED"


def test_real_terminal_user_change_invalidates_selected_version(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    user = snapshot_user(db, connector, name=f"Original User-{CNIC}")
    selected_version = user.row_version

    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="actual-terminal-change",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=user.uid,
                    user_id=user.user_id,
                    name=f"Renamed User-{CNIC}",
                )
            ],
        ),
    )
    assert user.row_version == selected_version + 1
    with pytest.raises(ValueError, match="changed since it was selected"):
        create_user_deletion_job(
            db,
            connector=connector,
            targets=[(user.user_key, selected_version)],
            reason="Remove a confirmed obsolete terminal account",
            typed_confirmation="DELETE 1 USERS FROM 1",
            idempotency_key="stale-selection-after-change",
            actor="StateHealthAdmin",
        )


def test_no_registration_routes_remain():
    paths = {route.path for route in app.routes}
    assert "/api/v1/connectors" not in paths
    assert "/device/v1/activate" not in paths
    assert "/device/v2/onboard" in paths
    assert "/api/v2/devices/{connector_id}/users" in paths


def test_command_cancellation_is_local_before_dispatch_and_a_handshake_after(db: Session):
    connector = connector_fixture(db)
    make_writable(connector)
    queued = create_command(
        db,
        connector=connector,
        command_type="REFRESH_USERS",
        payload={},
        expected_state={},
        desired_state={},
        idempotency_key="cancel-local-0001",
        actor="StateHealthAdmin",
    )
    dispatched = create_command(
        db,
        connector=connector,
        command_type="REFRESH_USERS",
        payload={},
        expected_state={},
        desired_state={},
        idempotency_key="cancel-handshake-0001",
        actor="StateHealthAdmin",
    )
    dispatched.status = "DISPATCHED"
    dispatched.attempt_count = 1
    raw_session, admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    headers = {"X-CSRF-Token": admin.csrf_token}

    local_response = client.post(
        f"/api/v2/commands/{queued.command_id}/cancel", json={}, headers=headers
    )
    assert local_response.status_code == 200
    assert local_response.json()["status"] == "CANCELLED"

    handshake_response = client.post(
        f"/api/v2/commands/{dispatched.command_id}/cancel", json={}, headers=headers
    )
    assert handshake_response.status_code == 200
    assert handshake_response.json()["status"] == "CANCEL_REQUESTED"
    cancel_envelope = serialize_command(dispatched)
    assert cancel_envelope["type"] == "command_cancel"
    assert cancel_envelope["command_id"] == dispatched.command_id


def test_invalid_attendance_cnic_filter_is_rejected(db: Session):
    raw_session, _admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    response = client.get("/api/v1/attendance?cnic=not-a-cnic")
    assert response.status_code == 422
    assert "13 digits" in response.json()["detail"]


def test_firmware_control_reason_is_rejected_before_database_overflow(db: Session):
    raw_session, admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    response = client.post(
        "/api/v1/firmware/campaigns/not-reached/cancel",
        json={"reason": "x" * 201, "password": "correct-password"},
        headers={"X-CSRF-Token": admin.csrf_token},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"


def test_device_detail_exposes_admin_command_status_not_device_wire_envelope(
    db: Session,
):
    connector = connector_fixture(db)
    command = create_command(
        db,
        connector=connector,
        command_type="RESTART_ZKT",
        payload={"reason": "Regression test", "mode": "protocol"},
        expected_state={"serial": SERIAL},
        desired_state={"restart": True},
        idempotency_key="restart-device-detail-contract",
        actor="StateHealthAdmin",
        expires_in_seconds=120,
    )
    raw_session, _admin = create_admin_session(
        db,
        username="StateHealthAdmin",
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    response = client.get(f"/api/v1/devices/{connector.connector_id}")

    assert response.status_code == 200
    active = response.json()["active_command"]
    assert active["command_id"] == command.command_id
    assert active["type"] == "RESTART_ZKT"
    assert active["status"] == command.status
    assert active["status"]
    assert "payload" not in active
    assert "expected_state" not in active
    assert "desired_state" not in active

from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.crypto import decrypt_cnic, decrypt_json, decrypt_text
from zk_add.db import Base
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
    DeviceConnectionEvent,
    DeviceUser,
    IdentityConflictResolution,
    IdentityTombstone,
    OnboardingNonce,
    OrdsOutbox,
    TemporaryAdminLease,
)
from zk_add.onboarding import derive_bootstrap_secret, verify_onboarding_signature
from zk_add.protocol import body_sha256, sign_request, signature_material
from zk_add.schemas import (
    AttendanceBatchRequest,
    AttendanceEventIn,
    HeartbeatPayload,
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
    apply_command_update,
    create_admin_lease,
    create_command,
    create_device_user_command,
    delete_device_user_command,
    ingest_attendance,
    onboard_connector,
    oracle_payload,
    replace_user_snapshot,
    serialize_command,
    update_device_user_command,
    update_heartbeat,
    upsert_alert,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.web import app, get_db
from zk_add.worker import (
    apply_ords_delivery_result,
    event_uid_is_valid,
    ords_delivery_metrics,
    ords_delivery_succeeded,
    ords_failure_category,
    ords_failure_is_permanent,
)


MAC = "e0:72:a1:d6:f3:28"
SERIAL = "ADZV211860253"
CNIC = "3520212345671"


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


def event(*, event_uid: str, user_id: str = "1007", raw_name: str | None = None):
    return AttendanceEventIn(
        event_uid=event_uid,
        uid="7",
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

    punch = event(event_uid="d" * 64, user_id="1001", raw_name=f"One-{CNIC}")
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
        event(event_uid="7" * 64, user_id="1001", raw_name=f"Same Name-{CNIC}"),
        event(event_uid="8" * 64, user_id="1002", raw_name=f"Same Name-{CNIC}"),
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
    punch = event(event_uid="b" * 64, user_id="1003", raw_name=f"Same Name-{CNIC}")
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

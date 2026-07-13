from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.db import Base
from zk_add.models import AttendanceEvent, DeviceAlert, DeviceConnectionEvent, DeviceUser, OrdsOutbox
from zk_add.schemas import (
    AttendanceBatchRequest,
    AttendanceEventIn,
    HeartbeatPayload,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import (
    apply_command_update,
    create_admin_lease,
    create_connector,
    ingest_attendance,
    replace_user_snapshot,
    update_heartbeat,
)
from zk_add.settings import settings
from zk_add.worker import event_uid_is_valid, ords_delivery_succeeded


@pytest.fixture()
def db() -> Session:
    settings.pii_fernet_key = Fernet.generate_key().decode()
    settings.pii_lookup_key = "test-lookup-key-with-enough-entropy"
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def connector_fixture(db: Session):
    connector, _activation = create_connector(
        db,
        hardware_id="e0:72:a1:d6:f3:28",
        zone_id="ZONE-SLICTOWER-3FL",
        zone_name="ZONE-SLICTOWER-3FL",
        device_id="1",
        display_name="SLICTOWER · 3rd Floor",
        expected_serial="ADZV211860253",
        actor="test",
        ip_address="127.0.0.1",
    )
    db.commit()
    return connector


def test_attendance_rejects_corrupt_event_uids():
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


def test_heartbeat_tracks_flapping_and_transition_history(db: Session):
    connector = connector_fixture(db)
    payload = HeartbeatPayload(
        firmware_version="zone-lite-2.0.0",
        zkt={
            "online": False,
            "connection_state": "FLAPPING",
            "serial": "ADZV211860253",
            "ip_address": "192.168.110.137",
            "model": "MB20/ID",
            "platform": "ZLM60_TFT",
            "consecutive_failures": 3,
            "flap_count_15m": 4,
            "user_record_size": 72,
        },
    )
    result = update_heartbeat(
        db, connector=connector, boot_id="boot-1", sequence=1, payload=payload
    )
    db.commit()

    assert result["state"] == "FLAPPING"
    assert result["zkt"]["capabilities"]["observed_user_record_bytes"] == 72
    transition = db.scalar(select(DeviceConnectionEvent))
    assert transition and transition.to_state == "FLAPPING"
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ZKT_CONNECTION_FLAPPING"))
    assert alert and alert.severity == "WARNING"


def test_heartbeat_resolves_stale_esp_offline_alert(db: Session):
    connector = connector_fixture(db)
    alert = DeviceAlert(
        connector_id=connector.id,
        code="ESP_OFFLINE",
        severity="HIGH",
        state="OPEN",
        message="ESP heartbeat is stale.",
    )
    db.add(alert)
    db.commit()

    update_heartbeat(
        db,
        connector=connector,
        boot_id="boot-recovered",
        sequence=1,
        payload=HeartbeatPayload(
            firmware_version="zone-lite-2.0.2",
            zkt={"online": True, "connection_state": "RECOVERING"},
        ),
    )
    db.commit()

    assert alert.state == "RESOLVED"
    assert alert.resolved_at is not None


def test_heartbeat_tracks_reconcile_and_restart_schedule(db: Session):
    connector = connector_fixture(db)
    payload = HeartbeatPayload(
        firmware_version="zone-lite-2.0.1",
        zkt={
            "online": True,
            "connection_state": "ONLINE",
            "backoff_until": "2026-07-10T15:01:00Z",
            "stability_since": "2026-07-10T15:02:00Z",
            "last_reconcile_at": "2026-07-10T15:03:00Z",
            "next_restart_at": "2026-07-10T17:00:00Z",
        },
    )
    update_heartbeat(db, connector=connector, boot_id="boot-1", sequence=1, payload=payload)
    db.commit()

    zkt = connector.zkt_device
    assert zkt.backoff_until.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 10, 15, 1, tzinfo=timezone.utc
    )
    assert zkt.stability_since.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 10, 15, 2, tzinfo=timezone.utc
    )
    assert zkt.last_reconcile_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 10, 15, 3, tzinfo=timezone.utc
    )
    assert zkt.next_restart_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 10, 17, 0, tzinfo=timezone.utc
    )

    # Current-state nulls clear, while an omitted last successful reconcile is
    # intentionally retained across connector reboots.
    update_heartbeat(
        db,
        connector=connector,
        boot_id="boot-2",
        sequence=2,
        payload=HeartbeatPayload(
            firmware_version="zone-lite-2.0.1",
            zkt={
                "online": True,
                "connection_state": "RECOVERING",
                "backoff_until": None,
                "stability_since": None,
                "next_restart_at": None,
            },
        ),
    )
    db.commit()

    assert zkt.backoff_until is None
    assert zkt.stability_since is None
    assert zkt.next_restart_at is None
    assert zkt.last_reconcile_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 10, 15, 3, tzinfo=timezone.utc
    )


def test_snapshot_admin_lease_and_durable_command_result(db: Session):
    connector = connector_fixture(db)
    connector.zkt_device.capability_profile = {
        "read_users": True,
        "read_attendance": True,
        "user_write": True,
        "admin_lease": True,
        "protocol_restart": True,
        "name_bytes": 24,
    }
    connector.zkt_device.certification_state = "CERTIFIED"
    snapshot = UserSnapshotRequest(
        snapshot_id="snapshot-1",
        complete=True,
        observed_at=datetime.now(timezone.utc),
        users=[
            UserSnapshotRow(
                uid="7",
                user_id="1007",
                name="Ayesha-S-3520212345671",
                privilege=0,
                card=1234,
            )
        ],
    )
    assert replace_user_snapshot(db, connector=connector, snapshot=snapshot) == 1
    user = db.scalar(select(DeviceUser))
    assert user and user.display_name == "Ayesha" and user.shift_worker

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
    db.commit()
    assert lease.state == "ACTIVE"
    expires_at = lease.expires_at
    if expires_at.tzinfo is None:  # SQLite drops timezone metadata; PostgreSQL does not.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert int(expires_at.timestamp()) == 1_900_000_000


def test_user_snapshot_handles_user_id_swaps_atomically(db: Session):
    connector = connector_fixture(db)
    observed_at = datetime.now(timezone.utc)
    first = UserSnapshotRequest(
        snapshot_id="snapshot-before-swap",
        complete=True,
        observed_at=observed_at,
        users=[
            UserSnapshotRow(uid="1", user_id="1001", name="First", card=1),
            UserSnapshotRow(uid="2", user_id="1002", name="Second", card=2),
        ],
    )
    replace_user_snapshot(db, connector=connector, snapshot=first)
    db.commit()

    swapped = UserSnapshotRequest(
        snapshot_id="snapshot-after-swap",
        complete=True,
        observed_at=observed_at,
        users=[
            UserSnapshotRow(uid="1", user_id="1002", name="First", card=1),
            UserSnapshotRow(uid="2", user_id="1001", name="Second", card=2),
        ],
    )
    assert replace_user_snapshot(db, connector=connector, snapshot=swapped) == 2
    db.commit()

    rows = {row.uid: row for row in db.scalars(select(DeviceUser)).all()}
    assert rows["1"].user_id == "1002"
    assert rows["2"].user_id == "1001"


def test_user_snapshot_handles_reused_user_id(db: Session):
    connector = connector_fixture(db)
    observed_at = datetime.now(timezone.utc)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="snapshot-old-uid",
            complete=True,
            observed_at=observed_at,
            users=[UserSnapshotRow(uid="1", user_id="1001", name="Old User")],
        ),
    )
    db.commit()

    assert replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="snapshot-reused-id",
            complete=True,
            observed_at=observed_at,
            users=[UserSnapshotRow(uid="9", user_id="1001", name="New User")],
        ),
    ) == 1
    db.commit()

    rows = {row.uid: row for row in db.scalars(select(DeviceUser)).all()}
    assert rows["9"].user_id == "1001" and rows["9"].present
    assert rows["1"].user_id.startswith("__retired__") and not rows["1"].present


def test_user_snapshot_rejects_duplicates_before_mutating(db: Session):
    connector = connector_fixture(db)
    snapshot = UserSnapshotRequest(
        snapshot_id="snapshot-duplicate",
        complete=True,
        observed_at=datetime.now(timezone.utc),
        users=[
            UserSnapshotRow(uid="1", user_id="1001", name="One"),
            UserSnapshotRow(uid="2", user_id="1001", name="Two"),
        ],
    )
    with pytest.raises(ValueError, match="Duplicate device user ID"):
        replace_user_snapshot(db, connector=connector, snapshot=snapshot)
    assert db.scalar(select(DeviceUser)) is None


def test_attendance_ingestion_is_idempotent(db: Session):
    connector = connector_fixture(db)
    event = AttendanceEventIn(
        event_uid="e" * 64,
        uid="7",
        user_id="1007",
        raw_name="Ayesha-3520212345671",
        device_event_time=datetime.now(timezone.utc),
        captured_at=datetime.now(timezone.utc),
        source="LIVE",
        punch=0,
        status=0,
        clock_quality="OK",
    )
    accepted, duplicates = ingest_attendance(db, connector=connector, events=[event, event])
    db.commit()
    assert accepted == [event.event_uid]
    assert duplicates == [event.event_uid]
    assert db.scalar(select(AttendanceEvent)).ords_status == "PENDING"


def test_reconcile_attendance_is_normalized_for_ords(db: Session):
    connector = connector_fixture(db)
    replace_user_snapshot(
        db,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id="snapshot-ords-normalization",
            complete=True,
            observed_at=datetime.now(timezone.utc),
            users=[
                UserSnapshotRow(
                    uid="7",
                    user_id="1007",
                    name="Ayesha-3520212345671",
                )
            ],
        ),
    )
    event = AttendanceEventIn(
        event_uid="f" * 64,
        uid="7",
        user_id="1007",
        raw_name="Ayesha-3520212345671",
        device_event_time=datetime.now(timezone.utc),
        captured_at=datetime.now(timezone.utc),
        source="RECONCILE_15M",
        clock_quality="OK",
    )
    ingest_attendance(db, connector=connector, events=[event])
    db.commit()

    payload = db.scalar(select(OrdsOutbox)).payload
    assert payload["capturetype"] == "DUMP_RECONNECT"
    assert payload["trust_status"] == "BACKFILL_ACCEPTED_CLOCK_OK"


def test_ords_live_conflict_is_an_idempotent_acknowledgement():
    assert ords_delivery_succeeded(409, {"message": "resource already exists"}) is True
    assert ords_delivery_succeeded(201, {"success": True}) is True
    assert ords_delivery_succeeded(400, {"success": False}) is False

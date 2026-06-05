from __future__ import annotations

from datetime import datetime, timezone
import threading

from sqlalchemy.orm import sessionmaker

from zk_common.enums import SourceType, SyncStatus, TrustStatus
from zk_common.hashing import attendance_event_uid
from zk_zone_agent.attendance import AttendanceContext, attendance_processor
from zk_zone_agent.bulk_user_update import split_machine_identity
from zk_zone_agent.crypto import protect_secret
from zk_zone_agent.db import (
    AttendanceEvent,
    Base,
    Device,
    DeviceUser,
    OracleAttendanceOutbox,
    ServiceEvent,
    SyncQueue,
    create_sqlite_engine,
)
from zk_zone_agent.oracle_sync import (
    DELIVERY_BULK,
    DELIVERY_LIVE,
    ORACLE_STATUS_ACKED,
    ORACLE_STATUS_BLOCKED_IDENTITY,
    ORACLE_STATUS_FAILED_RETRYABLE,
    ORACLE_STATUS_PENDING,
    OracleSyncWorker,
    build_oracle_event_payload,
)
from zk_zone_agent.zk_client import ZKAttendance


def _session_factory(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def _device_event(
    *,
    event_uid: str = "event-1",
    source_type: str = SourceType.LIVE.value,
    trust_status: str = TrustStatus.TRUSTED_LIVE.value,
    raw_punch: bool = False,
) -> AttendanceEvent:
    return AttendanceEvent(
        event_uid=event_uid,
        zone_id="ZONE-SLICTOWER-3FL",
        device_id="MAIN-GATE-MB20",
        device_serial="ADZV211860253",
        user_id="1001",
        employee_name="Ali Khan",
        cnic="3520212345671",
        device_event_time=datetime(2026, 4, 1, 3, 39, 48, tzinfo=timezone.utc),
        zone_received_wall_time=datetime(2026, 4, 1, 3, 39, 50, tzinfo=timezone.utc),
        zone_trusted_time=datetime(2026, 4, 1, 3, 39, 50, tzinfo=timezone.utc),
        status=trust_status,
        trust_status=trust_status,
        punch="0",
        raw_event="{}",
        device_drift_seconds=2.4,
        raw_punch=raw_punch,
        source_type=source_type,
        sync_status=SyncStatus.PENDING.value,
    )


def test_machine_identity_parser_handles_shift_marker_and_missing_cnic():
    normal = split_machine_identity("Qaisar-4220133929615")
    shift = split_machine_identity("Qaisar-S-4220133929615")
    missing = split_machine_identity("Qaisar")

    assert normal.employee_name == "Qaisar"
    assert normal.cnic == "4220133929615"
    assert normal.raw_punch is False
    assert shift.employee_name == "Qaisar"
    assert shift.cnic == "4220133929615"
    assert shift.raw_punch is True
    assert missing.employee_name == "Qaisar"
    assert missing.cnic == ""
    assert missing.raw_punch is False


def test_oracle_payload_mapper_uses_utc_allowed_statuses_and_raw_punch_flag():
    event = _device_event(
        source_type=SourceType.DUMP_RECONNECT.value,
        trust_status=TrustStatus.BACKFILL_UNVERIFIED_AGENT_DOWN.value,
        raw_punch=True,
    )

    payload = build_oracle_event_payload(event)

    assert payload == {
        "event_uid": "event-1",
        "zone_id": "ZONE-SLICTOWER-3FL",
        "device_id": "MAIN-GATE-MB20",
        "device_serial": "ADZV211860253",
        "user_id": "1001",
        "employee_name": "Ali Khan",
        "cnic": "3520212345671",
        "timestamp": "2026-04-01T03:39:48Z",
        "clockdiff": "2.4",
        "capturetype": "DUMP_RECONNECT",
        "trust_status": "BACKFILL_UNVERIFIED_BLIND_PERIOD",
        "raw_punch": "T",
    }


def test_oracle_payload_mapper_collapses_suspicious_internal_statuses():
    event = _device_event(trust_status=TrustStatus.SUSPECT_PC_TIME.value)

    payload = build_oracle_event_payload(event)

    assert payload["trust_status"] == "SUSPECT_DEVICE_TIME"


def test_attendance_event_uid_is_stable_across_live_and_dump_sources():
    timestamp = datetime(2026, 6, 3, 18, 10)

    live_uid = attendance_event_uid(
        zone_id="ZONE-SLICTOWER-3FL",
        device_serial="ADZV211860253",
        user_id="1001",
        device_event_time=timestamp,
        punch=0,
        source_uid=None,
    )
    dump_uid = attendance_event_uid(
        zone_id="ZONE-SLICTOWER-3FL",
        device_serial="ADZV211860253",
        user_id="1001",
        device_event_time=timestamp,
        punch=0,
        source_uid=77,
    )

    assert live_uid == dump_uid


def test_attendance_processor_enqueues_oracle_outbox_and_disables_legacy_attendance_sync(
    tmp_path,
):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            Device(
                device_id="MAIN-GATE-MB20",
                label="Main Gate MB20",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted=protect_secret("1979"),
                serial="ADZV211860253",
                enabled=True,
            )
        )
        session.add(
            DeviceUser(
                device_id="MAIN-GATE-MB20",
                user_id="1001",
                employee_name="Qaisar-S-4220133929615",
            )
        )
        session.commit()

    with session_factory() as session:
        row = attendance_processor.process(
            session,
            device=session.query(Device).one(),
            attendance=ZKAttendance(
                user_id="1001",
                timestamp=datetime(2026, 6, 3, 18, 10),
                punch=0,
                uid=77,
                raw={"source": "test"},
            ),
            context=AttendanceContext(
                zone_id="ZONE-SLICTOWER-3FL",
                timezone="Asia/Karachi",
                internet_online=True,
                oracle_attendance_configured=True,
                oracle_cutover_utc=datetime(2026, 6, 3, 13, 0, tzinfo=timezone.utc),
            ),
            source_type=SourceType.LIVE,
            zone_trusted_time=datetime(2026, 6, 3, 13, 10, tzinfo=timezone.utc),
        )

        outbox = session.query(OracleAttendanceOutbox).one()
        assert row.employee_name == "Qaisar"
        assert row.cnic == "4220133929615"
        assert row.raw_punch is True
        assert row.device_event_time == datetime(2026, 6, 3, 13, 10, tzinfo=timezone.utc)
        assert outbox.status == ORACLE_STATUS_PENDING
        assert outbox.delivery_mode == DELIVERY_LIVE
        assert session.query(SyncQueue).count() == 0


def test_attendance_processor_blocks_oracle_delivery_when_cnic_is_missing(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            Device(
                device_id="MAIN-GATE-MB20",
                label="Main Gate MB20",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted=protect_secret("1979"),
                serial="ADZV211860253",
                enabled=True,
            )
        )
        session.add(DeviceUser(device_id="MAIN-GATE-MB20", user_id="1002", employee_name="No Cnic"))
        session.commit()

    with session_factory() as session:
        attendance_processor.process(
            session,
            device=session.query(Device).one(),
            attendance=ZKAttendance(
                user_id="1002",
                timestamp=datetime(2026, 6, 3, 18, 20),
                punch=0,
                uid=78,
                raw={"source": "test"},
            ),
            context=AttendanceContext(
                zone_id="ZONE-SLICTOWER-3FL",
                timezone="Asia/Karachi",
                internet_online=True,
                oracle_attendance_configured=True,
                oracle_cutover_utc=datetime(2026, 6, 3, 13, 0, tzinfo=timezone.utc),
            ),
            source_type=SourceType.LIVE,
            zone_trusted_time=datetime(2026, 6, 3, 13, 20, tzinfo=timezone.utc),
        )

        outbox = session.query(OracleAttendanceOutbox).one()
        assert outbox.status == ORACLE_STATUS_BLOCKED_IDENTITY
        assert "CNIC" in outbox.last_error
        assert session.query(SyncQueue).count() == 0


def test_live_delivery_failure_keeps_event_retryable(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        event = _device_event()
        session.add(event)
        session.flush()
        session.add(
            OracleAttendanceOutbox(
                attendance_event_id=event.id,
                event_uid=event.event_uid,
                status=ORACLE_STATUS_PENDING,
                delivery_mode=DELIVERY_LIVE,
            )
        )
        session.commit()

    class DownClient:
        def post_live(self, _payload):
            raise RuntimeError("network down")

    worker = OracleSyncWorker(threading.Event(), session_factory=session_factory)
    worker._record_service_event = lambda *_args: None

    assert worker._sync_live_once(DownClient()) is True

    with session_factory() as session:
        row = session.query(OracleAttendanceOutbox).one()
        assert row.status == ORACLE_STATUS_FAILED_RETRYABLE
        assert row.attempt_count == 1
        assert row.last_error == "network down"
        assert row.next_attempt_at is not None


def test_live_delivery_commits_claim_before_http_call(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        event = _device_event()
        session.add(event)
        session.flush()
        session.add(
            OracleAttendanceOutbox(
                attendance_event_id=event.id,
                event_uid=event.event_uid,
                status=ORACLE_STATUS_PENDING,
                delivery_mode=DELIVERY_LIVE,
            )
        )
        session.commit()

    class InspectingClient:
        def post_live(self, _payload):
            with session_factory() as session:
                row = session.query(OracleAttendanceOutbox).one()
                assert row.status == "IN_FLIGHT"
                session.add(
                    ServiceEvent(
                        event_type="CONCURRENT_WRITE",
                        description="HTTP delivery did not hold the SQLite transaction.",
                    )
                )
                session.commit()
            return 201, {"success": True}, ""

    worker = OracleSyncWorker(threading.Event(), session_factory=session_factory)
    worker._record_service_event = lambda *_args: None

    assert worker._sync_live_once(InspectingClient()) is True

    with session_factory() as session:
        row = session.query(OracleAttendanceOutbox).one()
        assert row.status == ORACLE_STATUS_ACKED
        assert session.query(ServiceEvent).count() == 1


def test_bulk_duplicate_existing_is_acknowledged(tmp_path):
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        event = _device_event(source_type=SourceType.DUMP_STARTUP.value)
        session.add(event)
        session.flush()
        session.add(
            OracleAttendanceOutbox(
                attendance_event_id=event.id,
                event_uid=event.event_uid,
                status=ORACLE_STATUS_FAILED_RETRYABLE,
                delivery_mode=DELIVERY_BULK,
            )
        )
        session.commit()

    class DuplicateClient:
        def post_bulk(self, *, batch_uid, events):
            assert batch_uid.startswith("ZONE-ORDS-")
            assert len(events) == 1
            return (
                200,
                {
                    "success": True,
                    "inserted_count": 0,
                    "duplicate_existing_count": 1,
                    "invalid_count": 0,
                    "failed_count": 0,
                    "duplicate_in_request_count": 0,
                    "results": [],
                },
                "",
            )

    worker = OracleSyncWorker(threading.Event(), session_factory=session_factory)
    worker._record_service_event = lambda *_args: None

    assert worker._sync_bulk_once(DuplicateClient()) is True

    with session_factory() as session:
        row = session.query(OracleAttendanceOutbox).one()
        assert row.status == ORACLE_STATUS_ACKED
        assert row.attempt_count == 1

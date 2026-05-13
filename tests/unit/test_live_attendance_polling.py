from __future__ import annotations

from datetime import datetime, timezone
import threading

from sqlalchemy.orm import sessionmaker

from zk_common.enums import SourceType, TrustStatus
from zk_zone_agent.config import ActiveZoneConfig
from zk_zone_agent.crypto import protect_secret
from zk_zone_agent.db import Base, Device, DeviceUser, AttendanceEvent, create_sqlite_engine
from zk_zone_agent.device_worker import DeviceWorker
from zk_zone_agent.trusted_time import TrustedTimeService
from zk_zone_agent.zk_client import ZKAttendance


class _PollOnlyClient:
    def __init__(self, attendances: list[ZKAttendance]) -> None:
        self.attendances = attendances

    def get_attendance(self) -> list[ZKAttendance]:
        return self.attendances


def test_live_poll_reconcile_captures_new_attendance_once(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    trusted_now = datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)
    trusted_time = TrustedTimeService(wall_clock=lambda: trusted_now, monotonic_ns=lambda: 0)
    with session_factory() as session:
        trusted_time.update_from_head_office(trusted_now, session)
        session.add(
            Device(
                device_id="MAIN-GATE",
                label="Main Gate",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted=protect_secret("1979"),
                serial="ADZV211860253",
                enabled=True,
                last_clock_status="OK",
            )
        )
        session.add(DeviceUser(device_id="MAIN-GATE", user_id="5", employee_name="Ali"))
        session.commit()

    worker = DeviceWorker(
        device_id="MAIN-GATE",
        zone_config=ActiveZoneConfig(
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="http://127.0.0.1:8080",
            zone_token="token",
            setup_completed=True,
        ),
        stop_event=threading.Event(),
        trusted_time=trusted_time,
        session_factory=session_factory,
        live_poll_reconcile_interval_seconds=0,
    )
    client = _PollOnlyClient(
        [
            ZKAttendance(
                user_id="5",
                timestamp=trusted_now,
                punch=0,
                uid="5",
                raw={"source": "test"},
            )
        ]
    )

    assert worker._live_poll_reconcile_if_due(client) == 1
    assert worker._live_poll_reconcile_if_due(client) == 0

    with session_factory() as session:
        row = session.query(AttendanceEvent).one()
        assert row.employee_name == "Ali"
        assert row.source_type == SourceType.LIVE_POLL.value
        assert row.trust_status == TrustStatus.TRUSTED_LIVE.value
        assert session.query(AttendanceEvent).count() == 1

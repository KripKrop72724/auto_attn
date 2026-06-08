from __future__ import annotations

from datetime import datetime, timezone
import threading

from sqlalchemy.orm import sessionmaker

from zk_common.enums import OutageType, SourceType, SyncStatus, TrustStatus
from zk_zone_agent.config import ActiveZoneConfig
from zk_zone_agent.crypto import protect_secret
from zk_zone_agent.db import Base, Device, DeviceUser, AttendanceEvent, OutagePeriod, SyncQueue, create_sqlite_engine
from zk_zone_agent.device_worker import DeviceWorker, _DeviceCommand
from zk_zone_agent.trusted_time import TrustedTimeService
from zk_zone_agent.zk_client import ZKAttendance


class _PollOnlyClient:
    def __init__(self, attendances: list[ZKAttendance]) -> None:
        self.attendances = attendances

    def get_attendance(self) -> list[ZKAttendance]:
        return self.attendances


class _CommandClient:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.get_users_calls = 0

    def stop_live_capture(self) -> None:
        self.stop_calls += 1

    def get_users(self):
        self.get_users_calls += 1
        raise AssertionError("canceled command should not touch the device")


def _command_worker() -> DeviceWorker:
    return DeviceWorker(
        device_id="MAIN-GATE",
        zone_config=ActiveZoneConfig(
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="https://head-office-production.up.railway.app",
            zone_token="token",
            setup_completed=True,
        ),
        stop_event=threading.Event(),
        trusted_time=TrustedTimeService(),
        session_factory=lambda: (_ for _ in ()).throw(RuntimeError("no database in this test")),
    )


def test_device_command_timeout_wakes_live_capture_and_cancels_before_start():
    worker = _command_worker()
    client = _CommandClient()
    worker.is_alive = lambda: True
    worker.command_available.set()
    worker._set_active_client(client)

    try:
        worker._submit_command("refresh_users", None, 0.001)
    except TimeoutError as exc:
        assert "refresh users" in str(exc)
    else:
        raise AssertionError("expected command timeout")

    command = worker.command_queue.get_nowait()
    assert command.canceled is True
    assert client.stop_calls >= 1


def test_device_worker_skips_canceled_command_without_mutating_device():
    worker = _command_worker()
    client = _CommandClient()
    command = _DeviceCommand(kind="refresh_users", payload=None, done=threading.Event())
    command.canceled = True
    worker.command_queue.put(command)

    worker._drain_commands(client)

    assert command.done.is_set()
    assert client.get_users_calls == 0


def test_retryable_device_io_retries_zkt_read_size_failures(monkeypatch):
    worker = _command_worker()
    client = _CommandClient()
    monkeypatch.setattr("zk_zone_agent.device_worker.settings.device_user_io_retry_attempts", 2)
    monkeypatch.setattr("zk_zone_agent.device_worker.settings.device_user_io_retry_delay_seconds", 0)
    calls = {"count": 0}

    def flaky_operation():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("can't read sizes")
        return ["ok"]

    assert worker._device_io(client, "read users", flaky_operation) == ["ok"]
    assert calls["count"] == 2
    assert client.stop_calls == 2


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


def test_reconcile_command_captures_missing_attendance_through_device_queue(tmp_path):
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
    worker.pending_startup_reconcile = True
    command = _DeviceCommand(kind="reconcile_attendance", payload=None, done=threading.Event())
    worker.command_queue.put(command)

    worker._drain_commands(client)

    assert command.done.is_set()
    assert command.error is None
    assert command.result == 1
    with session_factory() as session:
        row = session.query(AttendanceEvent).one()
        assert row.source_type == SourceType.DUMP_STARTUP.value


def test_reconcile_watchdog_submits_reconcile_command(monkeypatch):
    worker = _command_worker()
    calls = []
    submitted = threading.Event()
    reconcile_stop = threading.Event()
    monkeypatch.setattr("zk_zone_agent.device_worker.settings.reconcile_watchdog_startup_delay_seconds", 0)
    monkeypatch.setattr("zk_zone_agent.device_worker.settings.reconcile_watchdog_interval_seconds", 60)
    monkeypatch.setattr(
        "zk_zone_agent.device_worker.settings.reconcile_watchdog_command_timeout_seconds",
        123,
    )

    def submit(kind, payload, timeout_seconds):
        calls.append((kind, payload, timeout_seconds))
        submitted.set()

    worker._submit_command = submit
    thread = threading.Thread(
        target=worker._reconcile_watchdog_loop,
        args=(reconcile_stop,),
        daemon=True,
    )
    thread.start()
    assert submitted.wait(1)
    reconcile_stop.set()
    thread.join(timeout=1)

    assert calls == [("reconcile_attendance", None, 123)]


def test_reconnect_closes_sqlite_outage_with_utc_aware_duration(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    outage_start = datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)
    reconnect_time = datetime(2026, 5, 13, 10, 0, 30, tzinfo=timezone.utc)
    trusted_time = TrustedTimeService(wall_clock=lambda: reconnect_time, monotonic_ns=lambda: 0)

    with session_factory() as session:
        session.add(
            Device(
                device_id="MAIN-GATE",
                label="Main Gate",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted=protect_secret("1979"),
                enabled=True,
            )
        )
        session.add(
            OutagePeriod(
                zone_id="RWP-ZONE-01",
                device_id="MAIN-GATE",
                outage_type=OutageType.DEVICE_LAN_OUTAGE.value,
                start_time=outage_start,
                start_reason="previous disconnect",
                classification="LAN_DEVICE_OFFLINE",
                sync_status=SyncStatus.PENDING.value,
            )
        )
        session.commit()

    worker = DeviceWorker(
        device_id="MAIN-GATE",
        zone_config=ActiveZoneConfig(
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="https://head-office-production.up.railway.app",
            zone_token="token",
            setup_completed=True,
        ),
        stop_event=threading.Event(),
        trusted_time=trusted_time,
        session_factory=session_factory,
    )

    worker._close_outage("Device reconnected.")

    with session_factory() as session:
        outage = session.query(OutagePeriod).one()
        assert outage.start_time.tzinfo is not None
        assert outage.end_time is not None
        assert outage.end_time.tzinfo is not None
        assert outage.duration_seconds == 30
        queued = session.query(SyncQueue).one()
        assert queued.payload_type == "OUTAGE"

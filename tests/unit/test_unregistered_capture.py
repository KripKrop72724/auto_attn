from __future__ import annotations

from datetime import datetime, timezone
import importlib
import threading

from zk_common.enums import ClockStatus, PayloadType, SyncStatus, TrustStatus
from zk_common.hashing import canonical_json


def _reload_zone_modules(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_DATABASE_URL", f"sqlite:///{tmp_path / 'zone.db'}")
    monkeypatch.setenv("ZK_ZONE_DISABLE_WORKERS", "false")
    monkeypatch.setenv("ZK_ZONE_AUTO_DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("ZK_ZONE_BRUTEFORCE_ENABLED", "false")

    import zk_zone_agent.settings as settings_module
    import zk_zone_agent.db as db_module
    import zk_zone_agent.device_registry as registry_module
    import zk_zone_agent.config as config_module
    import zk_zone_agent.sync as sync_module
    import zk_zone_agent.supervisor as supervisor_module

    importlib.reload(settings_module)
    db_module = importlib.reload(db_module)
    registry_module = importlib.reload(registry_module)
    config_module = importlib.reload(config_module)
    sync_module = importlib.reload(sync_module)
    supervisor_module = importlib.reload(supervisor_module)
    return db_module, registry_module, config_module, sync_module, supervisor_module


def test_device_workers_start_without_zone_registration(monkeypatch, tmp_path):
    db_module, registry_module, config_module, _sync_module, supervisor_module = _reload_zone_modules(
        monkeypatch, tmp_path
    )
    db_module.init_db()
    with db_module.session_scope() as session:
        registry_module.device_registry.save_device(
            session,
            device_id="MAIN-GATE",
            label="Main Gate",
            ip="192.168.110.137",
            port=4370,
            comm_key=1979,
            serial="ADZV211860253",
            enabled=True,
        )

    started = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.zone_config = kwargs["zone_config"]

        def is_alive(self):
            return True

        def start(self):
            started.append(self)

    monkeypatch.setattr(supervisor_module, "DeviceWorker", FakeWorker)
    supervisor = supervisor_module.ZoneSupervisor()
    supervisor._start_device_workers()

    assert len(started) == 1
    assert started[0].zone_config.zone_id == config_module.UNREGISTERED_ZONE_ID
    assert not started[0].zone_config.setup_completed


def test_registration_reassigns_pre_registration_records(monkeypatch, tmp_path):
    db_module, _registry_module, config_module, _sync_module, _supervisor_module = _reload_zone_modules(
        monkeypatch, tmp_path
    )
    db_module.init_db()
    now = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    with db_module.session_scope() as session:
        session.add(
            db_module.AttendanceEvent(
                event_uid="event-1",
                zone_id=config_module.UNREGISTERED_ZONE_ID,
                device_id="MAIN-GATE",
                device_serial="ADZV211860253",
                user_id="5",
                device_event_time=now,
                zone_received_wall_time=now,
                zone_trusted_time=now,
                status=TrustStatus.TRUSTED_LIVE.value,
                trust_status=TrustStatus.TRUSTED_LIVE.value,
                raw_event="{}",
                source_type="LIVE",
                sync_status=SyncStatus.PENDING.value,
            )
        )
        session.add(
            db_module.ClockCheck(
                zone_id=config_module.UNREGISTERED_ZONE_ID,
                device_id="MAIN-GATE",
                trusted_time=now,
                windows_wall_time=now,
                monotonic_ns=123,
                status=ClockStatus.OK.value,
                sync_status=SyncStatus.PENDING.value,
            )
        )
        session.add(
            db_module.OutagePeriod(
                zone_id=config_module.UNREGISTERED_ZONE_ID,
                device_id="MAIN-GATE",
                outage_type="DEVICE_LAN_OUTAGE",
                start_time=now,
                sync_status=SyncStatus.PENDING.value,
            )
        )
        session.add(
            db_module.FraudIncident(
                zone_id=config_module.UNREGISTERED_ZONE_ID,
                device_id="MAIN-GATE",
                incident_type="DEVICE_CLOCK_DRIFT",
                severity="HIGH",
                description="pre-registration drift",
                sync_status=SyncStatus.PENDING.value,
            )
        )

    with db_module.session_scope() as session:
        config_module.config_manager.save_setup(
            session,
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="http://127.0.0.1:8080",
            zone_token="issued-token",
        )

    with db_module.session_scope() as session:
        assert session.query(db_module.AttendanceEvent).one().zone_id == "RWP-ZONE-01"
        assert session.query(db_module.ClockCheck).one().zone_id == "RWP-ZONE-01"
        assert session.query(db_module.OutagePeriod).one().zone_id == "RWP-ZONE-01"
        assert session.query(db_module.FraudIncident).one().zone_id == "RWP-ZONE-01"


def test_sync_payload_uses_registered_zone_without_mutating_queue(monkeypatch, tmp_path):
    db_module, _registry_module, config_module, sync_module, _supervisor_module = _reload_zone_modules(
        monkeypatch, tmp_path
    )
    db_module.init_db()
    with db_module.session_scope() as session:
        row = db_module.SyncQueue(
            payload_type=PayloadType.CLOCK_CHECK.value,
            payload_json=canonical_json({"id": 1, "zone_id": config_module.UNREGISTERED_ZONE_ID}),
            record_id="1",
            status=SyncStatus.PENDING.value,
        )
        session.add(row)
        session.flush()
        config = config_module.ActiveZoneConfig(
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="http://127.0.0.1:8080",
            zone_token="issued-token",
            setup_completed=True,
        )
        payload = sync_module.SyncWorker(threading.Event())._payload_for_sync(row, config)

        assert payload["zone_id"] == "RWP-ZONE-01"
        assert config_module.UNREGISTERED_ZONE_ID in row.payload_json

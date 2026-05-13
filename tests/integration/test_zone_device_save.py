from datetime import datetime, timezone
import importlib

from fastapi.testclient import TestClient

from zk_common.enums import SourceType, TrustStatus
from zk_zone_agent.device_validation import DeviceValidation
from zk_zone_agent.zk_client import ZKDeviceInfo


def _load_zone_web(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_DATABASE_URL", f"sqlite:///{tmp_path / 'zone.db'}")
    monkeypatch.setenv("ZK_ZONE_DISABLE_WORKERS", "true")
    import zk_zone_agent.settings as settings_module
    import zk_zone_agent.db as db_module
    import zk_zone_agent.device_registry as registry_module
    import zk_zone_agent.supervisor as supervisor_module
    import zk_zone_agent.web as web_module

    importlib.reload(settings_module)
    db_module = importlib.reload(db_module)
    importlib.reload(registry_module)
    importlib.reload(supervisor_module)
    web_module = importlib.reload(web_module)
    return db_module, web_module


def test_device_save_rejects_invalid_comm_key_without_persisting(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    def fail_validation(**_kwargs):
        raise ValueError("bad comm key")

    monkeypatch.setattr(web_module, "validate_device_connection", fail_validation)
    with TestClient(web_module.app) as client:
        response = client.post(
            "/devices",
            data={
                "device_id": "MAIN-GATE",
                "label": "Main Gate",
                "ip": "192.168.110.137",
                "port": "4370",
                "comm_key": "wrong",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Device+was+not+saved" in response.headers["location"]
    with db_module.session_scope() as session:
        assert session.query(db_module.Device).count() == 0


def test_device_save_persists_only_after_validation(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    def pass_validation(**_kwargs):
        return DeviceValidation(
            info=ZKDeviceInfo("ADZV211860253", "ZLM60_TFT", "MB20/0"),
            device_time=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(web_module, "validate_device_connection", pass_validation)
    with TestClient(web_module.app) as client:
        response = client.post(
            "/devices",
            data={
                "device_id": "MAIN-GATE",
                "label": "Main Gate",
                "ip": "192.168.110.137",
                "port": "4370",
                "comm_key": "1979",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Device+validated+and+saved" in response.headers["location"]
    with db_module.session_scope() as session:
        device = session.query(db_module.Device).one()
        assert device.serial == "ADZV211860253"
        assert device.last_clock_status == "PENDING"
        assert "Worker is starting" in device.last_error


def test_recent_attendance_api_returns_live_rows(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    db_module.init_db()
    now = datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)
    with db_module.session_scope() as session:
        session.add(
            db_module.AttendanceEvent(
                event_uid="event-1",
                zone_id="RWP-ZONE-01",
                device_id="MAIN-GATE",
                device_serial="ADZV211860253",
                user_id="5",
                employee_name="Ali",
                device_event_time=now,
                zone_received_wall_time=now,
                zone_trusted_time=now,
                status=TrustStatus.TRUSTED_LIVE.value,
                trust_status=TrustStatus.TRUSTED_LIVE.value,
                raw_event="{}",
                source_type=SourceType.LIVE_POLL.value,
                sync_status="PENDING",
            )
        )

    with TestClient(web_module.app) as client:
        response = client.get("/api/attendance/recent")

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert rows[0]["user"] == "Ali"
    assert rows[0]["source_type"] == "LIVE_POLL"

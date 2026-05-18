from datetime import datetime, timezone
import importlib
import re

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


def _create_admin(db_module, web_module):
    db_module.init_db()
    with db_module.session_scope() as session:
        web_module.create_admin(session, "local-pass")


def _unlock(client: TestClient) -> str:
    login = client.post("/login", data={"password": "local-pass"}, follow_redirects=False)
    assert login.status_code == 303
    page = client.get("/devices")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)
    assert match
    return match.group(1)


def test_device_save_rejects_invalid_comm_key_without_persisting(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)

    def fail_validation(**_kwargs):
        raise ValueError("bad comm key")

    monkeypatch.setattr(web_module, "validate_device_connection", fail_validation)
    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/devices",
            data={
                "csrf_token": csrf_token,
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
    _create_admin(db_module, web_module)

    def pass_validation(**_kwargs):
        return DeviceValidation(
            info=ZKDeviceInfo("ADZV211860253", "ZLM60_TFT", "MB20/0"),
            device_time=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(web_module, "validate_device_connection", pass_validation)
    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/devices",
            data={
                "csrf_token": csrf_token,
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


def test_mutating_device_route_requires_admin_csrf(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)

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

    assert response.status_code == 403
    with db_module.session_scope() as session:
        assert session.query(db_module.Device).count() == 0


def test_setup_stores_token_once_and_encrypts_secret(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    class FakeHeadOfficeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_time(self):
            return datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(web_module, "HeadOfficeClient", FakeHeadOfficeClient)
    with TestClient(web_module.app) as client:
        first = client.post(
            "/setup",
            data={
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi Main Office",
                "head_office_url": "https://head-office-production.up.railway.app",
                "zone_token": "issued-token",
                "timezone": "Asia/Karachi",
                "admin_password": "local-pass",
                "admin_password_confirm": "local-pass",
            },
            follow_redirects=False,
        )
        assert first.status_code == 303
        setup_page = client.get("/setup")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', setup_page.text).group(1)
        second = client.post(
            "/setup",
            data={
                "csrf_token": csrf,
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi Main Office",
                "head_office_url": "https://head-office-production.up.railway.app",
                "zone_token": "replacement-token",
                "timezone": "Asia/Karachi",
            },
            follow_redirects=False,
        )

    assert second.status_code == 409
    with db_module.session_scope() as session:
        row = session.query(db_module.ZoneConfig).one()
        assert row.zone_token_encrypted != "issued-token"
        assert "issued-token" not in row.zone_token_encrypted
        assert web_module.config_manager.get(session).zone_token == "issued-token"


def test_setup_rejects_non_production_head_office_url(monkeypatch, tmp_path):
    _db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    with TestClient(web_module.app) as client:
        response = client.post(
            "/setup",
            data={
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi Main Office",
                "head_office_url": "http://127.0.0.1:8080",
                "zone_token": "issued-token",
                "timezone": "Asia/Karachi",
                "admin_password": "local-pass",
                "admin_password_confirm": "local-pass",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Head+office+URL+must+use+HTTPS" in response.headers["location"]


def test_setup_allows_localhost_when_dev_override_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_ALLOW_DEV_HEAD_OFFICE_URLS", "true")
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    class FakeHeadOfficeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_time(self):
            return datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(web_module, "HeadOfficeClient", FakeHeadOfficeClient)
    with TestClient(web_module.app) as client:
        response = client.post(
            "/setup",
            data={
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi Main Office",
                "head_office_url": "http://127.0.0.1:8080",
                "zone_token": "issued-token",
                "timezone": "Asia/Karachi",
                "admin_password": "local-pass",
                "admin_password_confirm": "local-pass",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    with db_module.session_scope() as session:
        assert web_module.config_manager.get(session).head_office_url == "http://127.0.0.1:8080"


def test_existing_setup_can_create_first_admin_after_upgrade(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_ALLOW_DEV_HEAD_OFFICE_URLS", "true")
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    db_module.init_db()
    with db_module.session_scope() as session:
        web_module.config_manager.save_setup(
            session,
            zone_id="RWP-ZONE-01",
            zone_name="Rawalpindi Main Office",
            timezone="Asia/Karachi",
            head_office_url="http://127.0.0.1:8080",
            zone_token="issued-token",
        )

    with TestClient(web_module.app) as client:
        page = client.get("/setup")
        assert "Create Admin Unlock" in page.text
        response = client.post(
            "/admin/create?next=/setup",
            data={"admin_password": "local-pass", "admin_password_confirm": "local-pass"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with db_module.session_scope() as session:
        assert web_module.admin_exists(session)


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

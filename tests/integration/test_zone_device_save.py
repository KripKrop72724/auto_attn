from datetime import datetime, timezone
import importlib
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from zk_common.enums import SourceType, TrustStatus
from zk_zone_agent.device_validation import DeviceValidation
from zk_zone_agent.zk_client import ZKDeviceInfo, ZKUser


def _load_zone_web(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_DATABASE_URL", f"sqlite:///{tmp_path / 'zone.db'}")
    monkeypatch.setenv("ZK_ZONE_DISABLE_WORKERS", "true")
    import zk_zone_agent.settings as settings_module
    import zk_zone_agent.db as db_module
    import zk_zone_agent.config as config_module
    import zk_zone_agent.device_registry as registry_module
    import zk_zone_agent.local_security as local_security_module
    import zk_zone_agent.supervisor as supervisor_module
    import zk_zone_agent.webauthn_security as webauthn_module
    import zk_zone_agent.web as web_module

    importlib.reload(settings_module)
    db_module = importlib.reload(db_module)
    importlib.reload(config_module)
    importlib.reload(local_security_module)
    importlib.reload(webauthn_module)
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


class FakeWebAuthnSecurity:
    def __init__(self) -> None:
        self.register_challenge = "register-challenge"
        self.login_challenge = "login-challenge"
        self.used_challenges: set[str] = set()

    def credential_count(self, session) -> int:
        return session.query(__import__("zk_zone_agent.db").db.AdminWebAuthnCredential).count()

    def registration_options(self, _session, *, label=None):
        return {
            "challenge_id": self.register_challenge,
            "label": label or "Windows Hello",
            "publicKey": {
                "challenge": "abc",
                "rp": {"name": "ZK Zone Agent", "id": "localhost"},
                "user": {"id": "abc", "name": "zone-agent-admin", "displayName": "Zone Agent Local Admin"},
                "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                "authenticatorSelection": {
                    "authenticatorAttachment": "platform",
                    "userVerification": "required",
                },
            },
        }

    def verify_registration(
        self,
        session,
        *,
        challenge_id,
        credential,
        expected_origin,
        label=None,
        recovery_password=None,
    ):
        db_module = __import__("zk_zone_agent.db").db
        if challenge_id != self.register_challenge:
            raise ValueError("Windows Hello challenge was not found.")
        if challenge_id in self.used_challenges:
            raise ValueError("Windows Hello challenge was already used.")
        self.used_challenges.add(challenge_id)
        admin = session.scalar(select(db_module.LocalAdmin).where(db_module.LocalAdmin.id == 1))
        if admin is None:
            from zk_zone_agent.local_security import create_admin

            admin = create_admin(session, recovery_password)
        row = db_module.AdminWebAuthnCredential(
            admin_id=admin.id,
            credential_id=credential.get("id", "fake-credential"),
            public_key="fake-public-key",
            sign_count=0,
            label=label or "Windows Hello",
            credential_device_type="single_device",
            credential_backed_up=False,
        )
        session.add(row)
        session.flush()
        return admin, row

    def authentication_options(self, session):
        db_module = __import__("zk_zone_agent.db").db
        if session.query(db_module.AdminWebAuthnCredential).count() == 0:
            raise ValueError("Windows Hello unlock is not enrolled.")
        return {
            "challenge_id": self.login_challenge,
            "publicKey": {
                "challenge": "abc",
                "rpId": "localhost",
                "allowCredentials": [{"id": "fake-credential", "type": "public-key"}],
                "userVerification": "required",
            },
        }

    def verify_authentication(self, session, *, challenge_id, credential, expected_origin):
        db_module = __import__("zk_zone_agent.db").db
        if challenge_id != self.login_challenge:
            raise ValueError("Windows Hello challenge was not found.")
        if challenge_id in self.used_challenges:
            raise ValueError("Windows Hello challenge was already used.")
        self.used_challenges.add(challenge_id)
        admin = session.scalar(select(db_module.LocalAdmin).where(db_module.LocalAdmin.id == 1))
        if admin is None:
            raise ValueError("Local admin is not configured.")
        return admin


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


def _seed_device_user(db_module, *, online=True):
    db_module.init_db()
    now = datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)
    with db_module.session_scope() as session:
        session.add(
            db_module.Device(
                device_id="MAIN-GATE",
                label="Main Gate",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted="encrypted",
                online=online,
            )
        )
        session.add(
            db_module.DeviceUser(
                device_id="MAIN-GATE",
                uid="7",
                user_id="1007",
                employee_name="Ali",
                privilege="0",
                card=12345,
                raw_json="{}",
            )
        )
        session.add(
            db_module.AttendanceEvent(
                event_uid="event-user-1007",
                zone_id="RWP-ZONE-01",
                device_id="MAIN-GATE",
                user_id="1007",
                employee_name="Ali",
                device_event_time=now,
                zone_received_wall_time=now,
                zone_trusted_time=now,
                status=TrustStatus.TRUSTED_LIVE.value,
                trust_status=TrustStatus.TRUSTED_LIVE.value,
                raw_event="{}",
                source_type=SourceType.LIVE.value,
                sync_status="PENDING",
            )
        )


def test_users_page_locked_admin_sees_read_only_cached_users(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module)

    with TestClient(web_module.app) as client:
        response = client.get("/users")

    assert response.status_code == 200
    assert "Ali" in response.text
    assert "users-layout" in response.text
    assert "editor-panel" in response.text
    assert "Admin unlock is required to edit users" in response.text
    assert "Save Changes" in response.text
    assert "disabled" in response.text


def test_user_update_requires_admin_csrf(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module)

    with TestClient(web_module.app) as client:
        response = client.post(
            "/users/MAIN-GATE/7/update",
            data={
                "user_id": "2007",
                "employee_name": "Ali Khan",
                "privilege": "14",
                "card": "987",
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    with db_module.session_scope() as session:
        user = session.query(db_module.DeviceUser).one()
        assert user.user_id == "1007"


def test_user_update_calls_device_worker_and_preserves_attendance_history(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module)
    calls = []

    def fake_update_device_user(device_id, update):
        calls.append((device_id, update))
        with db_module.session_scope() as session:
            row = session.scalar(
                select(db_module.DeviceUser).where(
                    db_module.DeviceUser.device_id == device_id,
                    db_module.DeviceUser.uid == update.uid,
                )
            )
            row.user_id = update.user_id
            row.employee_name = update.name
            row.privilege = str(update.privilege)
            row.card = update.card
        return ZKUser(
            uid=update.uid,
            user_id=update.user_id,
            name=update.name,
            privilege=str(update.privilege),
            card=update.card,
        )

    monkeypatch.setattr(web_module.zone_supervisor, "update_device_user", fake_update_device_user)

    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/users/MAIN-GATE/7/update",
            data={
                "csrf_token": csrf_token,
                "user_id": "2007",
                "employee_name": "Ali Khan",
                "privilege": "14",
                "card": "987",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert calls[0][0] == "MAIN-GATE"
    assert calls[0][1].user_id == "2007"
    with db_module.session_scope() as session:
        user = session.query(db_module.DeviceUser).one()
        assert user.user_id == "2007"
        assert user.employee_name == "Ali Khan"
        assert user.privilege == "14"
        assert user.card == 987
        attendance = session.query(db_module.AttendanceEvent).one()
        assert attendance.user_id == "1007"
        assert attendance.employee_name == "Ali"
        assert (
            session.query(db_module.AuditLedger)
            .filter(db_module.AuditLedger.record_type == "device_user_update")
            .count()
            == 1
        )


def test_user_update_duplicate_error_does_not_mutate_cache(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module)

    def duplicate_error(_device_id, _update):
        raise ValueError("User ID 2007 already exists on this device.")

    monkeypatch.setattr(web_module.zone_supervisor, "update_device_user", duplicate_error)

    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/users/MAIN-GATE/7/update",
            data={
                "csrf_token": csrf_token,
                "user_id": "2007",
                "employee_name": "Ali Khan",
                "privilege": "14",
                "card": "987",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "already+exists" in response.headers["location"]
    with db_module.session_scope() as session:
        user = session.query(db_module.DeviceUser).one()
        assert user.user_id == "1007"
        assert user.employee_name == "Ali"


def test_user_update_offline_device_fails_without_local_only_change(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module, online=False)

    def offline_error(_device_id, _update):
        raise RuntimeError("Device is not online; user changes can be retried after it reconnects.")

    monkeypatch.setattr(web_module.zone_supervisor, "update_device_user", offline_error)

    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/users/MAIN-GATE/7/update",
            data={
                "csrf_token": csrf_token,
                "user_id": "2007",
                "employee_name": "Ali Khan",
                "privilege": "14",
                "card": "987",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "not+online" in response.headers["location"]
    with db_module.session_scope() as session:
        user = session.query(db_module.DeviceUser).one()
        assert user.user_id == "1007"
        assert user.employee_name == "Ali"


def test_user_refresh_route_uses_worker_and_updates_cache(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    _create_admin(db_module, web_module)
    _seed_device_user(db_module)

    def fake_refresh(device_id):
        with db_module.session_scope() as session:
            row = session.scalar(
                select(db_module.DeviceUser).where(db_module.DeviceUser.device_id == device_id)
            )
            row.employee_name = "Ali Refreshed"
            row.card = 777
        return [ZKUser(uid="7", user_id="1007", name="Ali Refreshed", privilege="0", card=777)]

    monkeypatch.setattr(web_module.zone_supervisor, "refresh_device_users", fake_refresh)

    with TestClient(web_module.app) as client:
        csrf_token = _unlock(client)
        response = client.post(
            "/users/MAIN-GATE/refresh",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with db_module.session_scope() as session:
        user = session.query(db_module.DeviceUser).one()
        assert user.employee_name == "Ali Refreshed"
        assert user.card == 777


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
        assert "Create Recovery Password Unlock" in page.text
        response = client.post(
            "/admin/create?next=/setup",
            data={"admin_password": "local-pass", "admin_password_confirm": "local-pass"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    with db_module.session_scope() as session:
        assert web_module.admin_exists(session)


def test_password_unlock_creation_rejects_blank_password(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)

    with TestClient(web_module.app) as client:
        response = client.post(
            "/admin/create?next=/setup",
            data={"admin_password": "", "admin_password_confirm": ""},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Recovery+password+is+required" in response.headers["location"]
    with db_module.session_scope() as session:
        assert not web_module.admin_exists(session)


def test_webauthn_passwordless_admin_creation_and_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_ZONE_ALLOW_DEV_HEAD_OFFICE_URLS", "true")
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    fake_webauthn = FakeWebAuthnSecurity()
    monkeypatch.setattr(web_module, "webauthn_admin_security", fake_webauthn)

    class FakeHeadOfficeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_time(self):
            return datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(web_module, "HeadOfficeClient", FakeHeadOfficeClient)

    with TestClient(web_module.app, base_url="http://localhost:7860") as client:
        page = client.get("/setup")
        assert "Enroll Windows Hello Admin Unlock" in page.text
        options = client.post("/api/admin/webauthn/register/options", json={"label": "Front Desk PC"})
        assert options.status_code == 200
        verify = client.post(
            "/api/admin/webauthn/register/verify",
            json={
                "challenge_id": options.json()["challenge_id"],
                "credential": {"id": "fake-credential"},
                "label": "Front Desk PC",
                "next": "/setup",
            },
        )
        assert verify.status_code == 200
        assert "zk_zone_admin" in verify.headers["set-cookie"]

        setup_page = client.get("/setup")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', setup_page.text).group(1)
        setup = client.post(
            "/setup",
            data={
                "csrf_token": csrf,
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi Main Office",
                "head_office_url": "http://localhost:8080",
                "zone_token": "issued-token",
                "timezone": "Asia/Karachi",
                "admin_password": "",
                "admin_password_confirm": "",
            },
            follow_redirects=False,
        )

    assert setup.status_code == 303
    with db_module.session_scope() as session:
        assert web_module.admin_exists(session)
        assert not web_module.admin_has_recovery_password(session)
        assert session.query(db_module.AdminWebAuthnCredential).count() == 1
        assert web_module.config_manager.get(session).zone_token == "issued-token"


def test_webauthn_login_unlocks_admin_session(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    fake_webauthn = FakeWebAuthnSecurity()
    fake_webauthn.used_challenges.add(fake_webauthn.register_challenge)
    monkeypatch.setattr(web_module, "webauthn_admin_security", fake_webauthn)
    db_module.init_db()
    with db_module.session_scope() as session:
        admin = web_module.create_admin(session)
        session.add(
            db_module.AdminWebAuthnCredential(
                admin_id=admin.id,
                credential_id="fake-credential",
                public_key="fake-public-key",
                sign_count=0,
                label="Windows Hello",
            )
        )

    with TestClient(web_module.app, base_url="http://localhost:7860") as client:
        options = client.post("/api/admin/webauthn/login/options")
        assert options.status_code == 200
        verify = client.post(
            "/api/admin/webauthn/login/verify",
            json={
                "challenge_id": options.json()["challenge_id"],
                "credential": {"id": "fake-credential"},
                "next": "/devices",
            },
        )
        assert verify.status_code == 200
        assert verify.json()["redirect"] == "/devices"
        page = client.get("/devices")

    assert re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)


def test_existing_password_admin_can_enroll_webauthn(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    fake_webauthn = FakeWebAuthnSecurity()
    monkeypatch.setattr(web_module, "webauthn_admin_security", fake_webauthn)
    _create_admin(db_module, web_module)

    with TestClient(web_module.app, base_url="http://localhost:7860") as client:
        csrf = _unlock(client)
        options = client.post(
            "/api/admin/webauthn/register/options",
            json={"label": "Manager PC"},
            headers={"X-CSRF-Token": csrf},
        )
        assert options.status_code == 200
        verify = client.post(
            "/api/admin/webauthn/register/verify",
            json={
                "challenge_id": options.json()["challenge_id"],
                "credential": {"id": "fake-credential"},
                "label": "Manager PC",
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert verify.status_code == 200
    with db_module.session_scope() as session:
        assert session.query(db_module.AdminWebAuthnCredential).count() == 1
        assert web_module.admin_has_recovery_password(session)


def test_webauthn_replayed_challenge_is_rejected(monkeypatch, tmp_path):
    _db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    fake_webauthn = FakeWebAuthnSecurity()
    monkeypatch.setattr(web_module, "webauthn_admin_security", fake_webauthn)

    with TestClient(web_module.app, base_url="http://localhost:7860") as client:
        first = client.post(
            "/api/admin/webauthn/register/verify",
            json={
                "challenge_id": fake_webauthn.register_challenge,
                "credential": {"id": "fake-credential"},
            },
        )
        setup_page = client.get("/setup")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)">', setup_page.text).group(1)
        replay = client.post(
            "/api/admin/webauthn/register/verify",
            json={
                "challenge_id": fake_webauthn.register_challenge,
                "credential": {"id": "fake-credential-2"},
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert "already used" in replay.json()["detail"]


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
        response = client.get("/api/attendance/recent?date_preset=all")

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert rows[0]["user"] == "Ali"
    assert rows[0]["source_type"] == "LIVE_POLL"


def test_zone_timeline_filters_realtime_api_and_clean_timestamp_markup(monkeypatch, tmp_path):
    db_module, web_module = _load_zone_web(monkeypatch, tmp_path)
    db_module.init_db()
    inside = datetime(2026, 5, 18, 6, 30, 5, tzinfo=timezone.utc)
    outside = datetime(2026, 5, 18, 20, 0, tzinfo=timezone.utc)
    with db_module.session_scope() as session:
        session.add(
            db_module.Device(
                device_id="MAIN-GATE",
                label="Main Gate",
                ip="192.168.110.137",
                port=4370,
                comm_key_encrypted="encrypted",
            )
        )
        session.add_all(
            [
                db_module.AttendanceEvent(
                    event_uid="event-1",
                    zone_id="RWP-ZONE-01",
                    device_id="MAIN-GATE",
                    user_id="5",
                    employee_name="Ali",
                    device_event_time=inside,
                    zone_received_wall_time=inside,
                    zone_trusted_time=inside,
                    status=TrustStatus.TRUSTED_LIVE.value,
                    trust_status=TrustStatus.TRUSTED_LIVE.value,
                    raw_event="{}",
                    source_type=SourceType.LIVE_POLL.value,
                    sync_status="PENDING",
                ),
                db_module.AttendanceEvent(
                    event_uid="event-2",
                    zone_id="RWP-ZONE-01",
                    device_id="SIDE-GATE",
                    user_id="6",
                    employee_name="Sara",
                    device_event_time=outside,
                    zone_received_wall_time=outside,
                    zone_trusted_time=outside,
                    status="SUSPECT",
                    trust_status="SUSPECT",
                    raw_event="{}",
                    source_type="DUMP_RECONNECT",
                    sync_status="PENDING",
                ),
                db_module.ClockCheck(
                    zone_id="RWP-ZONE-01",
                    device_id="MAIN-GATE",
                    device_time=inside,
                    trusted_time=inside,
                    windows_wall_time=inside,
                    monotonic_ns=1,
                    status="OK",
                    reason="Clock ok.",
                    sync_status="PENDING",
                ),
                db_module.OutagePeriod(
                    zone_id="RWP-ZONE-01",
                    device_id="MAIN-GATE",
                    outage_type="DEVICE_LAN_OUTAGE",
                    start_time=inside,
                    classification="LAN_DEVICE_OFFLINE",
                    sync_status="PENDING",
                ),
                db_module.SyncQueue(
                    payload_type="ATTENDANCE",
                    payload_json="{}",
                    status="PENDING",
                    created_at=inside,
                ),
                db_module.ServiceEvent(
                    event_type="ZONE_SETUP_COMPLETED",
                    description="Setup completed.",
                    created_at=inside,
                ),
            ]
        )

    with TestClient(web_module.app) as client:
        query = (
            "date_preset=custom&from_date=2026-05-18&to_date=2026-05-18"
            "&device_id=MAIN-GATE"
        )
        attendance = client.get(
            f"/attendance?{query}&source_type=LIVE_POLL&trust_status=TRUSTED_LIVE"
        )
        assert attendance.status_code == 200
        assert "filter-head" in attendance.text
        assert "filter-grid" in attendance.text
        assert "Ali" in attendance.text
        assert "Sara" not in attendance.text
        assert 'data-timestamp="2026-05-18T06:30:05Z"' in attendance.text
        assert "11:30:05" in attendance.text
        assert 'data-time-format-option="12"' in attendance.text

        recent = client.get(
            f"/api/attendance/recent?{query}&source_type=LIVE_POLL&trust_status=TRUSTED_LIVE"
        )
        assert recent.status_code == 200
        body = recent.json()
        assert body["display_timezone"] == "Asia/Karachi"
        assert [row["user"] for row in body["rows"]] == ["Ali"]

        clock = client.get(f"/clock-guard?{query}&status=OK")
        assert clock.status_code == 200
        assert "Clock ok." in clock.text
        assert "11:30:05" in clock.text

        outages = client.get(f"/outages?{query}&outage_type=DEVICE_LAN_OUTAGE")
        assert outages.status_code == 200
        assert "LAN_DEVICE_OFFLINE" in outages.text
        assert "11:30:05" in outages.text

        sync_queue = client.get(
            "/sync-queue?date_preset=custom&from_date=2026-05-18&to_date=2026-05-18"
            "&payload_type=ATTENDANCE&status=PENDING"
        )
        assert sync_queue.status_code == 200
        assert "ATTENDANCE" in sync_queue.text
        assert "11:30:05" in sync_queue.text

        logs = client.get(
            "/logs?date_preset=custom&from_date=2026-05-18&to_date=2026-05-18"
            "&event_type=ZONE_SETUP_COMPLETED"
        )
        assert logs.status_code == 200
        assert "Setup completed." in logs.text
        assert "11:30:05" in logs.text

from datetime import datetime, timezone
import importlib
import re
import secrets

from fastapi.testclient import TestClient

from zk_common.hashing import canonical_json
from zk_common.security import body_sha256, password_hash, sign_request, signed_timestamp


def _signed_headers(token: str, zone_id: str, method: str, path: str, body: bytes, *, nonce: str | None = None):
    timestamp = signed_timestamp()
    nonce = nonce or secrets.token_urlsafe(18)
    digest = body_sha256(body)
    return {
        "Authorization": f"Bearer {token}",
        "X-ZK-Zone-Id": zone_id,
        "X-ZK-Timestamp": timestamp,
        "X-ZK-Nonce": nonce,
        "X-ZK-Body-SHA256": digest,
        "X-ZK-Signature": sign_request(
            token=token,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body_hash=digest,
        ),
    }


def _post_signed(client: TestClient, path: str, token: str, zone_id: str, payload: dict, *, nonce: str | None = None):
    body = canonical_json(payload).encode("utf-8")
    headers = _signed_headers(token, zone_id, "POST", path, body, nonce=nonce)
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def _issue_token(client: TestClient, zone_id: str = "RWP-ZONE-01", zone_name: str = "Rawalpindi") -> str:
    response = client.post("/zones/token", data={"zone_id": zone_id, "zone_name": zone_name})
    assert response.status_code == 200
    match = re.search(r"<code>([^<]+)</code>", response.text)
    assert match
    return match.group(1)


def _csrf_from_page(response) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', response.text)
    assert match
    return match.group(1)


def test_register_heartbeat_and_attendance_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_HEAD_DATABASE_URL", f"sqlite:///{tmp_path / 'head.db'}")
    import zk_head_office.settings as settings_module
    import zk_head_office.db as db_module
    import zk_head_office.web as web_module

    importlib.reload(settings_module)
    importlib.reload(db_module)
    web_module = importlib.reload(web_module)
    with TestClient(web_module.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["database_ok"] is True

        token = _issue_token(client)

        heartbeat_payload = {
            "zone_id": "RWP-ZONE-01",
            "zone_name": "Rawalpindi",
            "agent_version": "0.1.0",
            "server_time_estimate": "2026-05-13T11:00:00Z",
            "devices": [
                {
                    "device_id": "RWP-MAIN-GATE-01",
                    "serial": "ADZV211860253",
                    "online": True,
                    "last_clock_status": "OK",
                    "last_drift_seconds": 1,
                }
            ],
            "pending_queue_count": 0,
        }
        heartbeat = _post_signed(
            client,
            "/api/zones/heartbeat",
            token,
            "RWP-ZONE-01",
            heartbeat_payload,
        )
        assert heartbeat.status_code == 200

        event_time = datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc).isoformat()
        sync_payload = {
            "zone_id": "RWP-ZONE-01",
            "batch_id": "batch-1",
            "events": [
                {
                    "event_uid": "event-1",
                    "device_id": "RWP-MAIN-GATE-01",
                    "device_serial": "ADZV211860253",
                    "user_id": "5",
                    "employee_name": "Ali",
                    "device_event_time": event_time,
                    "zone_trusted_time": event_time,
                    "source_type": "DUMP_RECONNECT",
                    "trust_status": "BACKFILL_ACCEPTED_CLOCK_OK",
                    "raw_event": {},
                    "fraud_score": 40,
                    "fraud_reason": "Backfill accepted.",
                }
            ],
        }
        sync = _post_signed(
            client,
            "/api/sync/attendance",
            token,
            "RWP-ZONE-01",
            sync_payload,
        )
        assert sync.status_code == 200
        assert sync.json()["acked_event_uids"] == ["event-1"]


def test_clock_check_sync_accepts_large_monotonic_ns(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_HEAD_DATABASE_URL", f"sqlite:///{tmp_path / 'head.db'}")
    import zk_head_office.settings as settings_module
    import zk_head_office.db as db_module
    import zk_head_office.web as web_module

    importlib.reload(settings_module)
    db_module = importlib.reload(db_module)
    web_module = importlib.reload(web_module)
    with TestClient(web_module.app) as client:
        token = _issue_token(client)
        event_time = datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc).isoformat()
        payload = {
            "zone_id": "RWP-ZONE-01",
            "batch_id": "clock-batch-1",
            "clock_checks": [
                {
                    "id": 1,
                    "zone_id": "RWP-ZONE-01",
                    "device_id": "RWP-MAIN-GATE-01",
                    "device_serial": "ADZV211860253",
                    "device_time": event_time,
                    "trusted_time": event_time,
                    "windows_wall_time": event_time,
                    "monotonic_ns": 273092062000000,
                    "drift_seconds": 0,
                    "expected_device_time": None,
                    "jump_seconds": None,
                    "status": "OK",
                    "reason": "Device clock is within configured thresholds.",
                    "created_at": event_time,
                }
            ],
        }
        response = _post_signed(client, "/api/sync/clock-checks", token, "RWP-ZONE-01", payload)

        assert response.status_code == 200
        assert response.json()["acked_ids"] == ["1"]
        with db_module.session_scope() as session:
            row = session.query(db_module.ClockCheck).one()
            assert row.monotonic_ns == 273092062000000


def test_signed_sync_rejects_bad_signature_replay_wrong_zone_and_revoked_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_HEAD_DATABASE_URL", f"sqlite:///{tmp_path / 'head.db'}")
    import zk_head_office.settings as settings_module
    import zk_head_office.db as db_module
    import zk_head_office.web as web_module

    importlib.reload(settings_module)
    importlib.reload(db_module)
    web_module = importlib.reload(web_module)
    with TestClient(web_module.app) as client:
        token = _issue_token(client)
        payload = {
            "zone_id": "RWP-ZONE-01",
            "zone_name": "Rawalpindi",
            "agent_version": "0.1.0",
            "server_time_estimate": "2026-05-13T11:00:00Z",
            "devices": [],
            "pending_queue_count": 0,
        }
        body = canonical_json(payload).encode("utf-8")
        headers = _signed_headers(token, "RWP-ZONE-01", "POST", "/api/zones/heartbeat", body)
        headers["X-ZK-Signature"] = "bad"
        headers["Content-Type"] = "application/json"
        bad_signature = client.post("/api/zones/heartbeat", content=body, headers=headers)
        assert bad_signature.status_code == 401

        replay_nonce = "fixed-nonce"
        first = _post_signed(client, "/api/zones/heartbeat", token, "RWP-ZONE-01", payload, nonce=replay_nonce)
        second = _post_signed(client, "/api/zones/heartbeat", token, "RWP-ZONE-01", payload, nonce=replay_nonce)
        assert first.status_code == 200
        assert second.status_code == 409

        wrong_zone = _post_signed(client, "/api/zones/heartbeat", token, "OTHER-ZONE", payload)
        assert wrong_zone.status_code == 403

        revoke = client.post("/zones/RWP-ZONE-01/revoke", follow_redirects=False)
        assert revoke.status_code == 303
        revoked = _post_signed(client, "/api/zones/heartbeat", token, "RWP-ZONE-01", payload)
        assert revoked.status_code == 401


def test_head_office_admin_auth_protects_dashboard_and_token_controls(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_HEAD_DATABASE_URL", f"sqlite:///{tmp_path / 'head.db'}")
    monkeypatch.setenv("ZK_HEAD_REQUIRE_ADMIN_AUTH", "true")
    monkeypatch.setenv("ZK_HEAD_ADMIN_PASSWORD_HASH", password_hash("head-office-pass"))
    monkeypatch.setenv("ZK_HEAD_SESSION_SECRET", "test-head-office-session-secret")
    import zk_head_office.settings as settings_module
    import zk_head_office.db as db_module
    import zk_head_office.admin_auth as auth_module
    import zk_head_office.web as web_module

    importlib.reload(settings_module)
    importlib.reload(db_module)
    importlib.reload(auth_module)
    web_module = importlib.reload(web_module)
    with TestClient(web_module.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200

        zones_locked = client.get("/zones", follow_redirects=False)
        assert zones_locked.status_code == 303
        assert zones_locked.headers["location"].startswith("/login")

        unauth_token = client.post(
            "/zones/token",
            data={"zone_id": "RWP-ZONE-01", "zone_name": "Rawalpindi"},
        )
        assert unauth_token.status_code == 403

        login = client.post(
            "/login",
            data={"password": "head-office-pass", "next": "/zones"},
            follow_redirects=False,
        )
        assert login.status_code == 303

        zones_page = client.get("/zones")
        assert zones_page.status_code == 200
        assert 'href="/static/app.css"' in zones_page.text
        assert "http://testserver/static/app.css" not in zones_page.text
        csrf_token = _csrf_from_page(zones_page)

        token_response = client.post(
            "/zones/token",
            data={
                "csrf_token": csrf_token,
                "zone_id": "RWP-ZONE-01",
                "zone_name": "Rawalpindi",
            },
        )
        assert token_response.status_code == 200
        assert re.search(r"<code>([^<]+)</code>", token_response.text)

        revoke_without_csrf = client.post("/zones/RWP-ZONE-01/revoke")
        assert revoke_without_csrf.status_code == 403

        revoke = client.post(
            "/zones/RWP-ZONE-01/revoke",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert revoke.status_code == 303

from datetime import datetime, timezone
import importlib

from fastapi.testclient import TestClient


def test_register_heartbeat_and_attendance_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("ZK_HEAD_DATABASE_URL", f"sqlite:///{tmp_path / 'head.db'}")
    import zk_head_office.settings as settings_module
    import zk_head_office.db as db_module
    import zk_head_office.web as web_module

    importlib.reload(settings_module)
    importlib.reload(db_module)
    web_module = importlib.reload(web_module)
    with TestClient(web_module.app) as client:
        register = client.post(
            "/api/zones/register",
            json={"zone_id": "RWP-ZONE-01", "zone_name": "Rawalpindi", "enrollment_key": "ABC-123"},
        )
        assert register.status_code == 200
        token = register.json()["zone_token"]
        headers = {"Authorization": f"Bearer {token}"}

        heartbeat = client.post(
            "/api/zones/heartbeat",
            headers=headers,
            json={
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
            },
        )
        assert heartbeat.status_code == 200

        event_time = datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc).isoformat()
        sync = client.post(
            "/api/sync/attendance",
            headers=headers,
            json={
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
            },
        )
        assert sync.status_code == 200
        assert sync.json()["acked_event_uids"] == ["event-1"]

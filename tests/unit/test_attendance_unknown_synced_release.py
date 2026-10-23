"""One click lists and releases held unknown-identity punches with a synced CNIC."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_attendance_force_release import store as store
from test_attendance_repair import CORRECT_CNIC, repair_store as repair_store
from zk_add import attendance_direct_ords as direct
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem as Item,
    DeviceUser,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db


def unknown(sessions, *, status="BLOCKED_IDENTITY", name=None):
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = event.cnic_lookup_hash = None
        event.display_name = name
        event.ords_status = status
        db.commit()
        return event.id


def client_for(db):
    raw_session, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client, {"X-CSRF-Token": admin.csrf_token}


def test_filter_and_summary_count_only_held_unnamed_punches_with_one_synced_cnic(store):
    sessions, connector_id, _uid = store
    event_id = unknown(sessions)
    with sessions() as db:
        client, _headers = client_for(db)
        page = client.get("/api/v1/attendance", params={"unknown_synced_cnic": "true"}).json()
        assert [row["id"] for row in page["rows"]] == [event_id]
        assert page["rows"][0]["direct_ords_identity"]["cnic_source"] == "SYNCED_USER"
        summary = client.get("/api/v2/attendance-direct-ords/unknown-synced").json()
        assert summary["count"] == 1
        assert summary["event_ids"] == [event_id]
        assert summary["truncated"] is False
        assert summary["devices"][0]["connector_id"] == connector_id
        assert summary["devices"][0]["count"] == 1
        assert CORRECT_CNIC not in str(summary)
    app.dependency_overrides.clear()

    for status, name, user_cnic in (
        ("BLOCKED_IDENTITY", "Captured employee", True),  # a name was saved: not unknown
        ("QUARANTINED_IDENTITY_REUSE", None, True),  # a quarantine, not a hold
        ("BLOCKED_IDENTITY", None, False),  # no CNIC on the synced user
    ):
        unknown(sessions, status=status, name=name)
        with sessions() as db:
            user = db.scalar(select(DeviceUser))
            saved = user.cnic_encrypted
            if not user_cnic:
                user.cnic_encrypted = None
            db.commit()
            client, _headers = client_for(db)
            assert client.get("/api/v2/attendance-direct-ords/unknown-synced").json()["count"] == 0
            assert client.get("/api/v1/attendance", params={"unknown_synced_cnic": "true"}).json()["rows"] == []
            user.cnic_encrypted = saved
            db.commit()
        app.dependency_overrides.clear()


def test_quick_release_needs_no_reason_or_password_and_queues_the_synced_cnic(store):
    sessions, _connector_id, _uid = store
    event_id = unknown(sessions)
    with sessions() as db:
        client, headers = client_for(db)
        body = {"event_ids": [event_id], "idempotency_key": "quick-release-test-1"}
        assert client.post("/api/v2/attendance-direct-ords/quick-release", json=body).status_code == 403
        response = client.post("/api/v2/attendance-direct-ords/quick-release", json=body, headers=headers)
        assert response.status_code == 202, response.text
        run = response.json()
        assert run["selected"] == 1 and run["ready"] == 1 and run["skipped"] == 0
        assert run["not_matching"] == 0 and run["skipped_by_reason"] == {}
        replay = client.post("/api/v2/attendance-direct-ords/quick-release", json=body, headers=headers)
        assert replay.json()["job_id"] == run["job_id"]
        db.commit()
    app.dependency_overrides.clear()
    with sessions() as db:
        item = db.scalar(select(Item))
        # No record covers this punch: one click is the administrator's
        # confirmation that today's synced CNIC is sent.
        assert item.result["cnic_source"] == "SYNCED_USER"
        assert item.result["cnic_choice"] == "CURRENT"
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "PENDING"
        assert db.scalar(select(Item)).status == "WAITING_ORACLE"


def test_quick_release_refuses_punches_that_no_longer_match(store):
    sessions, _connector_id, _uid = store
    event_id = unknown(sessions, name="Captured employee")
    with sessions() as db:
        client, headers = client_for(db)
        response = client.post("/api/v2/attendance-direct-ords/quick-release", json={
            "event_ids": [event_id], "idempotency_key": "quick-release-test-2",
        }, headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "NOTHING_TO_RELEASE"
        assert db.scalar(select(Item)) is None
    app.dependency_overrides.clear()


def row(event_id, **values):
    return {
        "event_id": event_id, "exclusion": None, "cnic_choice_required": True,
        "punch_time_cnic": "NONE", "evidence_digest": "a" * 64, "default_cnic_choice": None,
        "cnic_choices": ["CURRENT"], "evidence_signals": [],
        "auto_release_at_next_snapshot": False, "awaiting_next_snapshot": False, **values,
    }


def test_quick_decisions_never_choose_between_people():
    choices, send_now = direct.quick_decisions([
        row(1),  # no record: today's synced CNIC
        row(2, punch_time_cnic="DIFFERS", default_cnic_choice="PUNCH_TIME", cnic_choices=["PUNCH_TIME", "CURRENT"]),
        # Another active user holds the punch-time CNIC: no default, skipped.
        row(3, punch_time_cnic="DIFFERS", cnic_choices=["PUNCH_TIME"]),
        row(4, punch_time_cnic="AMBIGUOUS"),  # records do not prove today's CNIC
        row(5, punch_time_cnic="CHANGED_NEAR_PUNCH", cnic_choices=["CURRENT"]),
        row(6, exclusion="CNIC_CONFLICT", auto_release_at_next_snapshot=True),
        row(7, cnic_choice_required=False, punch_time_cnic="MATCHES_CURRENT", awaiting_next_snapshot=True),
        row(8, evidence_signals=["NAME_DIFFERS"]),
    ])
    assert [(choice.event_id, choice.choice) for choice in choices] == [(1, "CURRENT"), (2, "PUNCH_TIME")]
    assert all(choice.held_by_other_confirmed is False for choice in choices)
    assert send_now == [7]

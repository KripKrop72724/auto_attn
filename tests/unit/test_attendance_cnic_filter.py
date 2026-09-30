"""CNIC linkage filters distinguish synced identities and paginate on the server."""

from fastapi.testclient import TestClient

from test_add_backend import (
    CNIC,
    _make_recovery_event,
    connector_fixture,
    db as db,
)
from zk_add.crypto import cnic_lookup, encrypt_cnic
from zk_add.models import DeviceUser
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db


def test_cnic_not_linked_filter_excludes_synced_and_saved_cnic_before_pagination(db):
    connector = connector_fixture(db)
    no_user = _make_recovery_event(
        db, connector, event_number=8101, ords_status="BLOCKED_IDENTITY",
        cnic_lookup_hash=None,
    )
    no_cnic = _make_recovery_event(
        db, connector, event_number=8102, ords_status="BLOCKED_IDENTITY",
        cnic_lookup_hash=None,
    )
    synced = _make_recovery_event(
        db, connector, event_number=8103, ords_status="BLOCKED_IDENTITY",
        cnic_lookup_hash=None,
    )
    saved = _make_recovery_event(db, connector, event_number=8104, ords_status="ACKED")
    retry = _make_recovery_event(db, connector, event_number=8105, ords_status="FAILED_RETRYABLE")
    db.add_all([
        DeviceUser(
            zkt_device_id=connector.zkt_device.id, uid=no_cnic.uid,
            user_id=no_cnic.user_id, display_name="No CNIC", present=True,
        ),
        DeviceUser(
            zkt_device_id=connector.zkt_device.id, uid=synced.uid,
            user_id=synced.user_id, display_name="Synced identity", present=True,
            cnic_encrypted=encrypt_cnic(CNIC), cnic_lookup_hash=cnic_lookup(CNIC),
        ),
    ])
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)

    response = client.get("/api/v1/attendance", params={
        "device_id": connector.connector_id, "ords_status": "CNIC_NOT_LINKED", "limit": 1,
    })
    assert response.status_code == 200
    page = response.json()
    assert [row["id"] for row in page["rows"]] == [no_cnic.id]
    assert page["rows"][0]["cnic_not_linked"] is True
    assert page["next_cursor"] == no_cnic.id
    assert "CNIC_NOT_LINKED" in page["status_options"]
    older = client.get("/api/v1/attendance", params={
        "device_id": connector.connector_id, "ords_status": "CNIC_NOT_LINKED",
        "limit": 1, "cursor": page["next_cursor"],
    }).json()
    assert [row["id"] for row in older["rows"]] == [no_user.id]
    assert older["next_cursor"] is None

    all_rows = client.get("/api/v1/attendance").json()["rows"]
    by_id = {row["id"]: row for row in all_rows}
    assert by_id[synced.id]["cnic_not_linked"] is False
    assert by_id[synced.id]["direct_ords_identity"]["cnic_source"] == "SYNCED_USER"
    assert by_id[saved.id]["cnic_not_linked"] is False
    combined = client.get("/api/v1/attendance", params=[
        ("ords_status", "CNIC_NOT_LINKED"), ("ords_status", "FAILED_RETRYABLE"),
    ]).json()
    assert {row["id"] for row in combined["rows"]} == {no_user.id, no_cnic.id, retry.id}
    missing_punch_cnic = client.get("/api/v1/attendance", params={"cnic_present": "false"}).json()
    assert {row["id"] for row in missing_punch_cnic["rows"]} == {no_user.id, no_cnic.id, synced.id}

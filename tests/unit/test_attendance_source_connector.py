"""Connector custody stays visible without asserting capture-time terminal proof."""

import pytest
from fastapi.testclient import TestClient

from test_add_backend import _make_recovery_event, connector_fixture, db as db
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db, serialize_attendance


@pytest.mark.parametrize("missing_serial", [None, ""])
def test_attendance_exposes_retained_owner_without_backfilling_terminal_serial(db, missing_serial):
    owner = connector_fixture(db)
    event = _make_recovery_event(db, owner, event_number=8201, ords_status="BLOCKED_IDENTITY")
    event.device_serial = missing_serial
    # Inventory labels can change after capture; they are current labels only.
    owner.display_name = "ZONE-KARACHI-01"
    owner.zone_id = "ZONE-KARACHI"
    owner.zone_name = "Karachi"
    # A binding observed later cannot prove this historical punch's origin.
    owner.zkt_device.serial = "CURRENT-BOUND-SERIAL"
    owner.zkt_device.confirmed_serial = "CURRENT-BOUND-SERIAL"
    owner.zkt_device.terminal_binding_state = "CONFIRMED"
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)

    response = client.get("/api/v1/attendance")
    assert response.status_code == 200
    saved = next(row for row in response.json()["rows"] if row["id"] == event.id)
    assert saved["source_connector"] == {
        "connector_id": owner.connector_id,
        "display_name": "ZONE-KARACHI-01",
        "zone_id": "ZONE-KARACHI",
        "zone_name": "Karachi",
    }
    assert saved["device_serial"] == missing_serial
    assert saved["terminal_provenance"]["state"] == "MISSING_TERMINAL_PROVENANCE"
    assert saved["terminal_provenance"]["confidence"] == "REVIEW_REQUIRED"
    assert saved["terminal_provenance"]["serial"] == missing_serial
    assert saved["ords_status"] == "BLOCKED_IDENTITY"
    assert event.device_serial == missing_serial


def test_serializer_does_not_attach_a_different_connectors_label(db):
    owner = connector_fixture(db)
    other = connector_fixture(db, hardware_id="aa:bb:cc:dd:ee:01")
    event = _make_recovery_event(db, owner, event_number=8202)
    event.device_serial = None

    saved = serialize_attendance(event, source_connector=other)
    assert saved["source_connector"] is None
    assert saved["terminal_provenance"]["state"] == "MISSING_TERMINAL_PROVENANCE"


def test_attendance_uses_retained_owner_when_terminal_is_now_assigned_elsewhere(db):
    owner = connector_fixture(db)
    owner.display_name = "Retained owner"
    event = _make_recovery_event(db, owner, event_number=8203, ords_status="BLOCKED_IDENTITY")
    event.device_serial = None
    terminal = owner.zkt_device
    current_owner = connector_fixture(db, hardware_id="aa:bb:cc:dd:ee:02")
    current_owner.display_name = "Different current assignment"
    db.delete(current_owner.zkt_device)
    db.flush()
    terminal.connector_id = current_owner.id
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    db.expire_all()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)

    response = client.get("/api/v1/attendance")
    assert response.status_code == 200
    saved = next(row for row in response.json()["rows"] if row["id"] == event.id)
    assert saved["source_connector"]["connector_id"] == owner.connector_id
    assert saved["source_connector"]["display_name"] == "Retained owner"
    assert saved["source_connector"]["connector_id"] != current_owner.connector_id
    assert saved["device_serial"] is None
    assert saved["terminal_provenance"]["state"] == "MISSING_TERMINAL_PROVENANCE"


def test_matching_source_reread_marker_is_shown_as_verified_provenance(db):
    owner = connector_fixture(db)
    event = _make_recovery_event(db, owner, event_number=8204)
    event.raw_event = {"terminal_provenance": "VERIFIED_SOURCE_REPLAY"}

    saved = serialize_attendance(event, source_connector=owner)
    assert saved["terminal_provenance"] == {
        "state": "VERIFIED_SOURCE_REPLAY",
        "serial": event.device_serial,
        "confidence": "VERIFIED",
        "explanation": "Terminal serial was verified by a matching terminal source reread.",
    }


@pytest.mark.parametrize("missing_serial", [None, ""])
@pytest.mark.parametrize("marker", ["VERIFIED_SOURCE_REPLAY", "VERIFIED_CONNECTOR_BINDING"])
def test_provenance_marker_without_terminal_serial_cannot_be_shown_as_verified(db, missing_serial, marker):
    owner = connector_fixture(db)
    event = _make_recovery_event(db, owner, event_number=8205)
    event.device_serial = missing_serial
    event.raw_event = {"terminal_provenance": marker}

    saved = serialize_attendance(event, source_connector=owner)
    assert saved["terminal_provenance"]["state"] == "MISSING_TERMINAL_PROVENANCE"
    assert saved["terminal_provenance"]["confidence"] == "REVIEW_REQUIRED"
    assert saved["terminal_provenance"]["serial"] == missing_serial

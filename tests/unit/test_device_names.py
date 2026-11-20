"""One naming rule: Life/Health and friendly names for /status, the installer and device rows."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_add_backend import connector_fixture, db as db  # noqa: F401
from test_device_health import admin_client
from zk_add import device_names, public_status
from zk_add.device_names import device_name, friendly_name, naming_for, numbered_places
from zk_add.public_status import OFFLINE, ONLINE, StatusTracker
from zk_add.service import serialize_connector
from zk_add.time_utils import utc_now

FIXTURE = Path(__file__).resolve().parents[2] / "apps/add_frontend/src/lib/deviceNames.fixture.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
STARTED = utc_now() - timedelta(hours=1)


@pytest.fixture(autouse=True)
def _no_remembered_fleet():
    device_names.forget_fleet()
    public_status.remember_statuses([])
    yield
    device_names.forget_fleet()
    public_status.remember_statuses([])


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: f"{case['code']}{' spare' if case['spare'] else ''}")
def test_fixture_names_division_place_unit_and_city(case):
    name = device_name(case["code"])
    numbered = numbered_places(CASES["fleet"])
    assert name.zone == device_names.division(case["code"]) == case["division"]
    assert (name.location, name.unit) == (case["place"], case["unit"])
    assert friendly_name(name, numbered, spare=case["spare"], listed=case["listed"]) == case["friendly_name"]
    assert device_names.city(name) == case["city"]


def test_fixture_covers_the_live_fleet_and_the_public_status_cases():
    codes = {case["code"] for case in CASES["cases"]}
    assert len(CASES["fleet"]) == 20 and set(CASES["fleet"]) <= codes
    assert sum(case["spare"] for case in CASES["cases"]) == 4
    # The /status unit-test codes (tests/unit/test_public_status.py) are all here.
    assert {"LF-ZONE-BLD1-01", "LF-ZONE-BLD9-03", "LF-ZONE-G&PBLD8-01", "ZONE-BLD9-01", "ZONE-SWAT-01",
            "ZONE-PESHAWAR-06", "ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-3FL", "zone-faisalabad-02",
            "ZONE-KARACHI"} <= codes


def test_friendly_names_equal_the_public_status_names_for_the_listed_fleet():
    now = utc_now()
    rows = [(SimpleNamespace(display_name=code, zone_id=code), ONLINE, now, None) for code in CASES["fleet"]]
    snapshot = public_status.build_snapshot(rows, now)
    expected = {case["code"]: case["friendly_name"] for case in CASES["cases"] if case["listed"]}
    assert {device["code"]: device["name"] for device in snapshot["devices"]} == expected


def test_public_status_keeps_its_names_module_api():
    # /status and its tests import these from public_status.
    assert public_status.device_name is device_names.device_name
    assert public_status.DeviceName is device_names.DeviceName
    assert public_status.ZONES == (("life", "Life"), ("health", "Health"))
    assert public_status.LOCATIONS == {"SLICTOWER": "SLIC Tower"}


def _device(db, code, number, *, spare=False, active=True):
    connector = connector_fixture(db, hardware_id=f"e0:72:a1:d6:f4:{number:02x}",
                                  expected_serial=f"NAMES-{number:04d}")
    connector.display_name = connector.zone_id = connector.zone_name = code
    connector.is_spare = spare
    connector.active = active
    db.commit()
    return connector


def test_naming_shows_the_unit_until_the_fleet_is_known(db):
    swat = _device(db, "ZONE-SWAT-01", 1)
    assert naming_for(swat) == {"division": "health", "friendly_name": "Swat · Device 1", "place": "Swat",
                                "unit": "Device 1", "city": "Swat"}
    device_names.remember_fleet([swat])
    assert naming_for(swat)["friendly_name"] == "Swat"


def test_spares_and_unlisted_devices_are_named_apart(db):
    one = _device(db, "LF-ZONE-BLD1-01", 1)
    spare = _device(db, "LF-ZONE-BLD1-01", 2, spare=True)
    lahore = _device(db, "ZONE-LAHORE-01", 3)
    new = _device(db, "ZONE-LAHORE-02", 4, active=False)
    device_names.remember_fleet([one, spare, lahore, new])  # spares and inactive rows are skipped
    assert naming_for(one)["friendly_name"] == "Building 1"
    assert naming_for(spare)["friendly_name"] == "Building 1 · Spare"
    # A device being set up counts itself, so neither Lahore device is just "Lahore".
    assert naming_for(new)["friendly_name"] == "Lahore · Device 2"
    assert naming_for(lahore)["friendly_name"] == "Lahore"


def test_device_rows_carry_names_and_the_public_status(db):
    life = _device(db, "LF-ZONE-BLD9-03", 1)
    _device(db, "LF-ZONE-BLD9-01", 2)
    spare = _device(db, "ZONE-LAHORE-03", 3, spare=True)
    client, _headers = admin_client(db)
    StatusTracker(STARTED).refresh_in(db, utc_now())
    rows = {row["connector_id"]: row for row in client.get("/api/v1/devices").json()["rows"]}
    row = rows[life.connector_id]
    assert (row["division"], row["friendly_name"], row["place"], row["unit"], row["city"]) == (
        "life", "Building 9 · Device 3", "Building 9", "Device 3", "Karachi")
    assert set(row["public_status"]) == {"status", "since", "reason_code"}
    assert rows[spare.connector_id]["public_status"] is None
    assert rows[spare.connector_id]["friendly_name"] == "Lahore · Spare"  # no Lahore device in use here
    # The raw codes stay for Technical details and older screens.
    assert (row["display_name"], row["zone_id"]) == ("LF-ZONE-BLD9-03", "LF-ZONE-BLD9-03")
    detail = client.get(f"/api/v1/devices/{life.connector_id}").json()
    assert detail["friendly_name"] == "Building 9 · Device 3"
    assert detail["public_status"] == row["public_status"]


def test_device_rows_offline_count_equals_the_public_status_page(db, monkeypatch):
    online = _device(db, "ZONE-MULTAN-01", 1)
    _device(db, "ZONE-MULTAN-02", 2)  # never seen: no internet
    _device(db, "ZONE-QUETTA-01", 3, spare=True)
    online.connected = True
    online.last_seen_at = utc_now()
    online.lifecycle_state = "ONLINE"
    online.zkt_device.connection_state = "ONLINE"
    online.zkt_device.online = True
    online.zkt_device.last_seen_at = utc_now()
    db.commit()
    client, _headers = admin_client(db)
    monkeypatch.setattr(public_status, "hub", public_status.StatusHub())
    monkeypatch.setattr(public_status, "tracker", StatusTracker(STARTED))

    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr(public_status, "session_scope", scope)
    public = client.get("/api/public/v1/status").json()
    rows = client.get("/api/v1/devices?view=list").json()["rows"]
    home_offline = sum((row["public_status"] or {}).get("status") == OFFLINE for row in rows)
    assert home_offline == public["offline"] == 1
    assert sum((row["public_status"] or {}).get("status") == ONLINE for row in rows) == public["online"]
    by_code = {device["code"]: device for device in public["devices"]}
    for row in rows:
        if row["public_status"] is not None:
            device = by_code[row["display_name"]]
            assert row["public_status"] == {key: device[key] for key in ("status", "since", "reason_code")}
            assert row["friendly_name"] == device["name"]


def test_serialize_connector_never_queries_for_names(db):
    from sqlalchemy import event

    connector = _device(db, "ZONE-SWAT-01", 1)
    device_names.remember_fleet([connector])
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = db.get_bind()
    db.refresh(connector)
    _ = connector.zkt_device
    event.listen(engine, "before_cursor_execute", record)
    try:
        row = serialize_connector(connector)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert row["friendly_name"] == "Swat"
    assert statements == []

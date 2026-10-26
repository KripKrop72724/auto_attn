"""Public status page: friendly names, Online/Offline rules, grace periods and the open API."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_add_backend import connector_fixture, db as db  # noqa: F401
from zk_add import public_status
from zk_add.models import PublicDeviceStatus
from zk_add.public_status import (
    OFFLINE,
    ONLINE,
    StatusHub,
    StatusTracker,
    build_snapshot,
    device_name,
    observed_status,
    offline_reason,
)
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.web import app

STARTED = utc_now() - timedelta(hours=1)  # the restart warmup is long over


@pytest.mark.parametrize(
    ("code", "zone", "location", "unit"),
    [
        ("LF-ZONE-BLD1-01", "life", "Building 1", "Device 1"),
        ("LF-ZONE-BLD9-03", "life", "Building 9", "Device 3"),
        ("LF-ZONE-G&PBLD8-01", "life", "Group & Pension Building 8", "Device 1"),
        ("ZONE-BLD9-01", "health", "Building 9", "Device 1"),
        ("ZONE-SWAT-01", "health", "Swat", "Device 1"),
        ("ZONE-PESHAWAR-06", "health", "Peshawar", "Device 6"),
        ("ZONE-SLICTOWER-13FL", "health", "SLIC Tower", "13th Floor"),
        ("ZONE-SLICTOWER-3FL", "health", "SLIC Tower", "3rd Floor"),
        ("zone-faisalabad-02", "health", "Faisalabad", "Device 2"),
        ("ZONE-KARACHI", "health", "Karachi", None),
    ],
)
def test_device_names_read_as_places(code, zone, location, unit):
    name = device_name(code)
    assert (name.zone, name.location, name.unit) == (zone, location, unit)


def test_device_number_is_shown_only_where_a_place_has_several():
    now = utc_now()
    rows = [(SimpleNamespace(display_name=code, zone_id=code), ONLINE, now, None) for code in (
        "LF-ZONE-BLD9-03", "LF-ZONE-BLD9-01", "LF-ZONE-BLD1-01", "LF-ZONE-G&PBLD8-01",
        "ZONE-BLD9-01", "ZONE-SWAT-01", "ZONE-PESHAWAR-06", "ZONE-PESHAWAR-02",
        "ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-3FL")]
    snapshot = build_snapshot(rows, now)
    assert [(device["zone"], device["name"]) for device in snapshot["devices"]] == [
        ("life", "Building 1"),
        ("life", "Building 9 · Device 1"),
        ("life", "Building 9 · Device 3"),
        ("life", "Group & Pension Building 8"),
        ("health", "Building 9"),
        ("health", "Peshawar · Device 2"),
        ("health", "Peshawar · Device 6"),
        ("health", "SLIC Tower · 3rd Floor"),
        ("health", "SLIC Tower · 13th Floor"),
        ("health", "Swat"),
    ]
    assert snapshot["zones"] == [
        {"zone": "life", "name": "Life", "total": 4, "online": 4, "offline": 0},
        {"zone": "health", "name": "Health", "total": 6, "online": 6, "offline": 0},
    ]


def _health(tier, link_state="CONNECTED", reason="LINK_UP", since=None):
    return SimpleNamespace(tier=tier, terminal_link={"state": link_state, "reason": reason, "since": since})


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (_health("ONLINE"), ONLINE),
        (_health("ONLINE_WITH_WARNINGS"), ONLINE),
        (_health("DEGRADED"), ONLINE),
        (_health("ONLINE", "STABILIZING", "STABILIZING"), ONLINE),
        (_health("ONLINE", "MAINTENANCE", "RESTARTING"), ONLINE),
        (_health("ONLINE", "RECONNECTING", "RETRY_WAIT"), ONLINE),
        (_health("ONLINE_WITH_WARNINGS", "FLAPPING", "FLAPPING"), ONLINE),
        (_health("OFFLINE", "UNKNOWN", "ESP_DISCONNECTED"), OFFLINE),
        (_health("ONLINE_WITH_WARNINGS", "DISCONNECTED", "HIK_NETWORK"), OFFLINE),
        (_health("DEGRADED", "ERROR", "HIK_SOURCE_CHANGED"), OFFLINE),
        (_health("ONLINE", "UNKNOWN", "NO_TERMINAL"), OFFLINE),
    ],
)
def test_online_means_attendance_can_reach_add(health, expected):
    connector = SimpleNamespace(last_seen_at=utc_now(), firmware_family="zkt")
    assert observed_status(connector, health, utc_now())[0] == expected


@pytest.mark.parametrize(
    ("health", "reason", "heading"),
    [
        (_health("OFFLINE", "UNKNOWN", "ESP_DISCONNECTED"), "no_internet", "No internet"),
        (_health("OFFLINE", "UNKNOWN", "NO_TERMINAL"), "no_internet", "No internet"),
        (_health("ONLINE_WITH_WARNINGS", "DISCONNECTED", "HIK_NETWORK"), "machine_not_responding",
         "Machine not responding"),
        (_health("ONLINE", "DISCONNECTED", "OFFLINE"), "machine_not_responding", "Machine not responding"),
        (_health("DEGRADED", "ERROR", "HIK_SOURCE_CHANGED"), "machine_records_changed",
         "Machine records changed"),
        (_health("DEGRADED", "ERROR", "HIK_AUTH"), "machine_settings_wrong", "Wrong machine settings"),
        (_health("DEGRADED", "ERROR", "HIK_INVALID_RESPONSE"), "machine_problem", "Machine problem"),
        (_health("ONLINE", "UNKNOWN", "NO_TERMINAL"), "not_set_up", "Not set up yet"),
    ],
)
def test_offline_devices_say_why_in_plain_words(health, reason, heading):
    assert offline_reason(health) == reason
    now = utc_now()
    device = build_snapshot([(SimpleNamespace(display_name="ZONE-SWAT-01", zone_id="ZONE-SWAT-01"),
                              OFFLINE, now, reason)], now)["devices"][0]
    assert (device["reason_code"], device["reason"]) == (reason, heading)
    # No system words for people who do not know the system.
    words = set(device["reason_detail"].lower().replace(",", " ").replace(".", " ").split())
    assert not words & {"esp", "add", "terminal", "hikvision", "zkt", "server", "connector"}


def _device(db, code, number, *, spare=False):
    connector = connector_fixture(db, hardware_id=f"e0:72:a1:d6:f3:{number:02x}",
                                  expected_serial=f"SERIAL-{number:04d}")
    connector.display_name = connector.zone_id = connector.zone_name = code
    connector.is_spare = spare
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    zkt.connection_state = "ONLINE"
    zkt.online = True
    db.commit()
    return connector


def _seen(db, connectors, now):
    for connector in connectors:
        if connector.connected:
            connector.last_seen_at = now
            connector.zkt_device.last_seen_at = now
    db.commit()


def test_spare_connectors_are_not_listed(db):
    _device(db, "ZONE-LAHORE-01", 1)
    _device(db, "ZONE-LAHORE-03", 3, spare=True)
    snapshot = StatusTracker(STARTED).refresh_in(db, utc_now())
    assert [device["code"] for device in snapshot["devices"]] == ["ZONE-LAHORE-01"]
    assert snapshot["devices"][0]["name"] == "Lahore"


def test_grace_periods_keep_since_through_short_outages(db):
    tracker = StatusTracker(STARTED)
    device = _device(db, "ZONE-SWAT-01", 1)
    start = utc_now()
    _seen(db, [device], start)
    first = tracker.refresh_in(db, start)["devices"][0]
    assert first["status"] == ONLINE

    # A reconnect shorter than the offline grace changes nothing.
    device.connected = False
    db.commit()
    blip = tracker.refresh_in(db, start + timedelta(seconds=60))["devices"][0]
    assert (blip["status"], blip["since"]) == (ONLINE, first["since"])
    device.connected = True
    _seen(db, [device], start + timedelta(seconds=90))
    assert tracker.refresh_in(db, start + timedelta(seconds=90))["devices"][0] == first

    # A real outage shows after the grace, dated from the last contact.
    last_contact = start + timedelta(seconds=90)
    device.connected = False
    db.commit()
    tracker.refresh_in(db, start + timedelta(seconds=100))
    pending = tracker.refresh_in(db, start + timedelta(seconds=200))["devices"][0]
    assert pending["status"] == ONLINE
    offline = tracker.refresh_in(db, start + timedelta(seconds=221))["devices"][0]
    assert offline["status"] == OFFLINE
    assert offline["since"] == public_status.iso_utc(last_contact)
    assert (offline["reason_code"], offline["reason"]) == ("no_internet", "No internet")
    row = db.get(PublicDeviceStatus, device.id)
    assert (row.status, ensure_utc(row.since)) == (OFFLINE, last_contact)

    # Back online once attendance can flow again for the online grace.
    back = start + timedelta(seconds=400)
    device.connected = True
    _seen(db, [device], back)
    assert tracker.refresh_in(db, back)["devices"][0]["status"] == OFFLINE
    _seen(db, [device], back + timedelta(seconds=60))
    online = tracker.refresh_in(db, back + timedelta(seconds=60))["devices"][0]
    assert (online["status"], online["since"]) == (ONLINE, public_status.iso_utc(back))
    assert (online["reason_code"], online["reason"], online["reason_detail"]) == (None, None, None)


def test_terminal_unreachable_is_offline_since_it_was_lost(db):
    device = _device(db, "LF-ZONE-BLD9-03", 3)
    now = utc_now()
    lost = now - timedelta(days=3)
    _seen(db, [device], now)
    device.zkt_device.connection_state = "OFFLINE"
    device.zkt_device.offline_since = lost
    device.zkt_device.last_transition_at = lost
    db.commit()
    seeded = StatusTracker(STARTED).refresh_in(db, now)["devices"][0]
    assert (seeded["name"], seeded["zone"], seeded["status"]) == ("Building 9", "life", OFFLINE)
    assert seeded["since"] == public_status.iso_utc(lost)
    assert seeded["reason"] == "Machine not responding"


@contextmanager
def _public_api(db, monkeypatch):
    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr(public_status, "session_scope", scope)
    monkeypatch.setattr(public_status, "hub", StatusHub())
    monkeypatch.setattr(public_status, "tracker", StatusTracker(STARTED))
    yield TestClient(app)


def test_public_api_needs_no_sign_in_and_shows_only_names_and_status(db, monkeypatch):
    _device(db, "LF-ZONE-BLD5-01", 5)
    _device(db, "ZONE-QUETTA-01", 7)
    with _public_api(db, monkeypatch) as client:
        response = client.get("/api/public/v1/status", headers={"Origin": "https://apex.example"})
        assert response.status_code == 200, response.text
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"updated_at", "total", "online", "offline", "zones", "devices"}
        assert [set(device) for device in body["devices"]] == [
            {"name", "location", "unit", "zone", "code", "status", "since",
             "reason_code", "reason", "reason_detail"}] * 2
        assert [device["name"] for device in body["devices"]] == ["Building 5", "Quetta"]

        life = client.get("/api/public/v1/status?zone=life").json()
        assert [device["code"] for device in life["devices"]] == ["LF-ZONE-BLD5-01"]
        assert life["zones"] == [{"zone": "life", "name": "Life", "total": 1,
                                  "online": life["online"], "offline": life["offline"]}]
        assert client.get("/api/public/v1/status?zone=zones").status_code == 422


def test_public_websocket_sends_the_snapshot(db, monkeypatch):
    _device(db, "ZONE-MULTAN-01", 1)
    with _public_api(db, monkeypatch) as client:
        with client.websocket_connect("/api/public/v1/status/ws?zone=health") as socket:
            body = socket.receive_json()
        assert [device["name"] for device in body["devices"]] == ["Multan"]
        assert public_status.hub._subscribers == set()


def test_hub_pushes_changes_and_heartbeats(monkeypatch):
    monkeypatch.setattr(public_status, "HEARTBEAT_SECONDS", 0.05)
    hub = StatusHub()
    first = {"updated_at": "a", "devices": [1]}
    changed = {"updated_at": "b", "devices": [2]}

    async def run():
        hub.publish(first)
        updates = hub.updates()
        assert await anext(updates) is first
        hub.publish(changed)
        assert await anext(updates) is changed
        same = {"updated_at": "c", "devices": [2]}
        hub.publish(same)  # only the time moved: no push, but the heartbeat carries it
        assert await anext(updates) is same
        assert len(hub._subscribers) == 1
        await updates.aclose()
        assert hub._subscribers == set()

    asyncio.run(run())


def test_streams_are_capped(monkeypatch):
    monkeypatch.setattr(public_status.settings, "public_status_max_streams", 1)
    hub = StatusHub()
    hub.publish({"updated_at": "a", "devices": []})

    async def run():
        updates = hub.updates()
        await anext(updates)
        assert hub.full()
        await updates.aclose()
        assert not hub.full()

    asyncio.run(run())


def test_a_new_device_is_saved_only_after_the_restart_warmup(db):
    device = _device(db, "ZONE-QUETTA-01", 1)
    device.connected = False
    db.commit()
    start = utc_now()
    tracker = StatusTracker(start)
    # Still reconnecting after the restart: shown as seen, but nothing is saved.
    assert tracker.refresh_in(db, start + timedelta(seconds=10))["devices"][0]["status"] == OFFLINE
    assert db.get(PublicDeviceStatus, device.id) is None
    device.connected = True
    _seen(db, [device], start + timedelta(seconds=95))
    assert tracker.refresh_in(db, start + timedelta(seconds=95))["devices"][0]["status"] == ONLINE
    assert db.get(PublicDeviceStatus, device.id).status == ONLINE


def _failing_hikvision(db, code, number, *, delivered):
    """A Hikvision terminal failing its checks, whose state changes every few seconds."""
    connector = _device(db, code, number)
    connector.firmware_family = "hikvision"
    zkt = connector.zkt_device
    zkt.connection_state = "OFFLINE"
    zkt.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "capture_mode": "poll", "stream_open": False,
        "last_stream_message_epoch": int(delivered.timestamp()) if delivered else 0,
        "last_successful_poll_epoch": 0,
    }}
    db.commit()
    return connector


def _flap(db, connector, now):
    connector.zkt_device.last_transition_at = now - timedelta(seconds=5)
    _seen(db, [connector], now)


def test_failing_hikvision_terminal_is_offline_since_its_last_delivery(db):
    now = utc_now()
    delivered = now - timedelta(hours=29)
    device = _failing_hikvision(db, "LF-ZONE-BLD1-01", 1, delivered=delivered)
    device.created_at = now - timedelta(days=60)
    _flap(db, device, now)
    seeded = StatusTracker(STARTED).refresh_in(db, now)["devices"][0]
    assert (seeded["name"], seeded["status"], seeded["reason"]) == ("Building 1", OFFLINE, "Machine records changed")
    assert seeded["since"] == public_status.iso_utc(delivered.replace(microsecond=0))


def test_a_saved_offline_date_moves_back_to_when_the_outage_began(db):
    now = utc_now()
    delivered = now - timedelta(hours=29)
    device = _failing_hikvision(db, "LF-ZONE-BLD1-01", 1, delivered=None)
    device.created_at = now - timedelta(days=60)
    _flap(db, device, now)
    tracker = StatusTracker(STARTED)
    # Saved before the fix: dated from the latest flap.
    assert tracker.refresh_in(db, now)["devices"][0]["since"] == public_status.iso_utc(now - timedelta(seconds=5))
    device.zkt_device.capability_profile = {"hikvision_health": {
        **device.zkt_device.capability_profile["hikvision_health"],
        "last_stream_message_epoch": int(delivered.timestamp())}}
    later = now + timedelta(seconds=10)
    _flap(db, device, later)
    corrected = tracker.refresh_in(db, later)["devices"][0]
    assert (corrected["status"], corrected["since"]) == (OFFLINE, public_status.iso_utc(delivered.replace(microsecond=0)))
    assert ensure_utc(db.get(PublicDeviceStatus, device.id).since) == delivered.replace(microsecond=0)


def test_offline_date_never_moves_back_past_a_time_add_saw_it_online(db):
    now = utc_now()
    device = _device(db, "ZONE-KARACHI-01", 1)
    _seen(db, [device], now)
    tracker = StatusTracker(STARTED)
    tracker.refresh_in(db, now)  # Online, seen by this process at now
    device.zkt_device.connection_state = "OFFLINE"
    device.zkt_device.offline_since = now - timedelta(days=2)  # left over from an older outage
    device.zkt_device.last_transition_at = now + timedelta(seconds=10)
    for step in (20, 200, 330):
        _seen(db, [device], now + timedelta(seconds=step))
        row = tracker.refresh_in(db, now + timedelta(seconds=step))["devices"][0]
    assert row["status"] == OFFLINE
    assert row["since"] >= public_status.iso_utc(now)


def test_hikvision_epochs_from_a_wrong_esp_clock_are_ignored(db):
    now = utc_now()
    device = _failing_hikvision(db, "ZONE-QUETTA-01", 1, delivered=now - timedelta(days=400))
    device.created_at = now - timedelta(days=60)
    db.commit()
    assert public_status.last_hikvision_delivery(device, now) is None
    device.zkt_device.capability_profile = {"hikvision_health": {"last_stream_message_epoch": 10**12}}
    assert public_status.last_hikvision_delivery(device, now) is None


def test_going_offline_is_never_dated_before_add_last_saw_it_online(db):
    now = utc_now()
    delivered = now - timedelta(minutes=10)  # live capture healthy, no punch since
    device = _failing_hikvision(db, "LF-ZONE-BLD1-01", 1, delivered=delivered)
    device.created_at = now - timedelta(days=60)
    health = device.zkt_device.capability_profile["hikvision_health"]
    device.zkt_device.connection_state = "ONLINE"
    device.zkt_device.capability_profile = {"hikvision_health": {**health, "poll_error": 0}}
    device.zkt_device.last_transition_at = now - timedelta(hours=2)  # Online since then
    _seen(db, [device], now)
    tracker = StatusTracker(STARTED)
    online = tracker.refresh_in(db, now)["devices"][0]
    assert (online["status"], online["since"]) == (ONLINE, public_status.iso_utc(now - timedelta(hours=2)))
    device.zkt_device.connection_state = "OFFLINE"
    device.zkt_device.capability_profile = {"hikvision_health": {**health, "poll_error": 6}}
    for step in (10, 200):
        _flap(db, device, now + timedelta(seconds=step))
        row = tracker.refresh_in(db, now + timedelta(seconds=step))["devices"][0]
    assert row["status"] == OFFLINE
    assert row["since"] == public_status.iso_utc(now)

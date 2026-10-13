"""ADD keeps the ZKT 2.7.1 heartbeat's boot report and the 2.7.3 panic trace."""
import pytest
from pydantic import ValidationError

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.schemas import HeartbeatPayload
from zk_add.service import update_heartbeat

PANIC = {"exception": "ABORT", "address": "0x42001234", "core": 1, "task": "add_ws"}
TRACE_FIELDS = {"reason": None, "cache_error": None, "fault_address": None, "cores": None}
# 3FL's 12 October panic as 2.7.3 reports it.
CACHE_ERROR = {
    "exception": "FAULT", "address": "0x00000000", "core": 4294967295, "task": "add_heartbeat",
    "reason": "Cache error", "cache_error": "MMU entry fault error",
    "cores": [
        {"core": 0, "task": "add_heartbeat", "pc": "0x42012345",
         "backtrace": ["0x42012342", "0x42034564", "0x4203aaa7"]},
        {"core": 1, "task": "zone_gateway", "pc": "0x4037abcd", "backtrace": ["0x4037abca", "0x40389a9d"],
         "corrupted": True},
    ],
}
BOOT_REPORT = {
    "reset_reason": "TASK_WATCHDOG", "boot_count": 3, "panic_count": 0, "task_wdt_count": 1,
    "int_wdt_count": 0, "other_wdt_count": 0, "brownout_count": 0,
    "previous_uptime_seconds": 3600, "previous_stage": "NETWORK_READY", "panic": PANIC,
}


def heartbeat(boot_report: dict) -> HeartbeatPayload:
    return HeartbeatPayload(
        firmware_version="zone-lite-2.7.1", uptime_seconds=600,
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        diagnostics={"boot_report": boot_report})


def test_heartbeat_boot_report_is_stored(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="watchdog-boot", sequence=1, payload=heartbeat(BOOT_REPORT))
    db.flush()
    assert connector.firmware_diagnostics["boot_report"] == {
        **BOOT_REPORT, "last_invalid_partition": None, "panic": {**PANIC, "detail": None, **TRACE_FIELDS}}


def test_panic_trace_is_stored(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="cache-error-boot", sequence=1,
                     payload=heartbeat({**BOOT_REPORT, "panic": CACHE_ERROR}))
    db.flush()
    panic = connector.firmware_diagnostics["boot_report"]["panic"]
    assert panic["core"] is None and panic["reason"] == "Cache error" and panic["fault_address"] is None
    assert panic["cache_error"] == "MMU entry fault error"
    assert panic["cores"] == [
        {**CACHE_ERROR["cores"][0], "corrupted": False}, CACHE_ERROR["cores"][1]]


@pytest.mark.parametrize("change,kept", [
    ({"reason": "Cache\u0001error"}, {"reason": None}),
    ({"reason": "x" * 41}, {"reason": None}),
    ({"cache_error": "y" * 81}, {"cache_error": None}),
    ({"fault_address": "0x3c9f000"}, {"fault_address": None}),
    ({"fault_address": 1017118720}, {"fault_address": None}),
    ({"cores": "core 0"}, {"cores": None}),
    ({"cores": [{"core": 0, "backtrace": ["0x4203aaa7", "bad"]}, {"core": 1, "task": "zone_gateway"}]},
     {"cores": [{"core": 1, "task": "zone_gateway", "pc": None, "backtrace": [], "corrupted": False}]}),
    ({"cores": [{"core": 9}, {"core": 0, "backtrace": ["0x42000000"] * 9}]}, {"cores": None}),
])
def test_unreadable_trace_keeps_the_heartbeat(db, change, kept):
    """An unreadable part of a 2.7.3 trace is dropped; the heartbeat and the rest are kept."""
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="trace-boot", sequence=1,
                     payload=heartbeat({**BOOT_REPORT, "panic": {**CACHE_ERROR, **change}}))
    db.flush()
    panic = connector.firmware_diagnostics["boot_report"]["panic"]
    assert panic["task"] == "add_heartbeat"
    for key, value in kept.items():
        assert panic[key] == value


def test_power_on_report_carries_only_the_counts(db):
    connector = connector_fixture(db)
    counts = {key: BOOT_REPORT[key] for key in BOOT_REPORT if key.endswith("_count")}
    update_heartbeat(db, connector=connector, boot_id="power-on-boot", sequence=1,
                     payload=heartbeat({"reset_reason": "POWERON", **counts}))
    db.flush()
    stored = connector.firmware_diagnostics["boot_report"]
    assert stored["reset_reason"] == "POWERON" and stored["previous_stage"] is None and stored["panic"] is None


@pytest.mark.parametrize("core,stored", [
    (4294967295, None), (-1, None), (8, None), ("1", None), (True, None), (1.0, 1), (0, 0),
])
def test_unknown_panic_core_keeps_the_heartbeat(db, core, stored):
    """3FL (12 Oct) panicked on 2.7.2; its panic core rejected every heartbeat."""
    connector = connector_fixture(db)
    report = {**BOOT_REPORT, "panic": {**PANIC, "core": core}}
    update_heartbeat(db, connector=connector, boot_id="panic-boot", sequence=1, payload=heartbeat(report))
    db.flush()
    panic = connector.firmware_diagnostics["boot_report"]["panic"]
    assert panic["core"] == stored and panic["exception"] == "ABORT" and panic["task"] == "add_ws"


@pytest.mark.parametrize("change", [
    {"reset_reason": "panic"},
    {"boot_count": -1},
    {"panic": {**PANIC, "address": "0x4200123"}},
    {"panic": {**PANIC, "task": "bad\u0001name"}},
    {"panic": {**PANIC, "detail": "x" * 129}},
])
def test_malformed_boot_report_is_rejected(change):
    with pytest.raises(ValidationError):
        heartbeat({**BOOT_REPORT, **change})

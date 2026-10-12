"""ADD keeps the ZKT 2.7.1 heartbeat's boot report."""
import pytest
from pydantic import ValidationError

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.schemas import HeartbeatPayload
from zk_add.service import update_heartbeat

PANIC = {"exception": "ABORT", "address": "0x42001234", "core": 1, "task": "add_ws"}
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
        **BOOT_REPORT, "last_invalid_partition": None, "panic": {**PANIC, "detail": None}}


def test_power_on_report_carries_only_the_counts(db):
    connector = connector_fixture(db)
    counts = {key: BOOT_REPORT[key] for key in BOOT_REPORT if key.endswith("_count")}
    update_heartbeat(db, connector=connector, boot_id="power-on-boot", sequence=1,
                     payload=heartbeat({"reset_reason": "POWERON", **counts}))
    db.flush()
    stored = connector.firmware_diagnostics["boot_report"]
    assert stored["reset_reason"] == "POWERON" and stored["previous_stage"] is None and stored["panic"] is None


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

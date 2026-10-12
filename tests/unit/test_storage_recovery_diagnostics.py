"""ADD resolves a durability alert once ZKT 2.7.1 verifies recovery, and keeps
which fault the device recovered from."""
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.models import DeviceAlert
from zk_add.schemas import HeartbeatPayload, StorageDiagnostics
from zk_add.service import update_heartbeat

LED = {"local_failures": 1, "local_failure_source": "add_connector.c:4302",
       "local_failure_last_source": "zone_lite.c:7516"}


def heartbeat(storage: dict, uptime: int) -> HeartbeatPayload:
    return HeartbeatPayload(
        firmware_version="zone-lite-2.7.1", uptime_seconds=uptime,
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        diagnostics={"storage": storage})


def test_verified_recovery_resolves_the_alert_and_keeps_the_fault(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="recovery-boot", sequence=1, payload=heartbeat({
        "durability": "DEGRADED", "error_operation": "local_write_commit", "error_code": 2,
        "error_file": "pending.jsonl", "write_failures": 1, **LED}, 600))
    db.flush()
    fault = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ESP_DURABILITY_FAULT"))
    assert fault.state == "OPEN"
    summary = fault.details["evidence"]["summary"]
    assert "local_write_commit error 2 on pending.jsonl" in summary
    assert "LED source add_connector.c:4302; latest zone_lite.c:7516" in summary
    update_heartbeat(db, connector=connector, boot_id="recovery-boot", sequence=2, payload=heartbeat({
        "durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True, "write_failures": 1,
        "recoveries": 1, "recovered_error_operation": "local_write_commit", "recovered_error_code": 2,
        "recovered_error_file": "pending.jsonl", "recovered_uptime_ms": 612345, "local_failures": 1,
        "local_failure_recoveries": 1}, 660))
    db.flush()
    assert fault.state == "RESOLVED"
    resolution = fault.details["resolution"]
    assert resolution["kind"] == "VERIFIED"
    assert resolution["recovered"] == {"recoveries": 1, "error_code": 2, "error_operation": "local_write_commit",
                                       "error_file": "pending.jsonl", "uptime_ms": 612345}
    stored = connector.firmware_diagnostics["storage"]
    assert stored["local_failure_recoveries"] == 1 and stored["local_failure_source"] is None


def test_older_firmware_resolves_without_recovery_evidence(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="old-boot", sequence=1, payload=heartbeat({
        "durability": "DEGRADED", "error_operation": "segment_append", "error_code": 5}, 600))
    update_heartbeat(db, connector=connector, boot_id="old-boot", sequence=2, payload=heartbeat({
        "durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True}, 660))
    db.flush()
    fault = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ESP_DURABILITY_FAULT"))
    assert fault.state == "RESOLVED" and "recovered" not in fault.details["resolution"]


@pytest.mark.parametrize("field, value", [
    ("recoveries", -1), ("recoveries", 0x100000000), ("local_failures", -1),
    ("local_failure_recoveries", 0x100000000), ("legacy_write_recoveries", -1),
    ("recovered_error_operation", "x" * 81), ("local_failure_last_source", "x" * 81),
    ("error_file", "x" * 33), ("recovered_error_file", "bad\u0001name"),
])
def test_recovery_fields_are_bounded(field, value):
    with pytest.raises(ValidationError):
        StorageDiagnostics(**{field: value})

"""ADD keeps the ZKT 2.7 heartbeat's memory evidence."""
from sqlalchemy import select

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.models import DeviceTelemetry
from zk_add.schemas import HeartbeatPayload
from zk_add.service import update_heartbeat

MEMORY = {
    "internal_free_bytes": 60000, "internal_largest_block_bytes": 28000, "internal_min_free_bytes": 20000,
    "dma_free_bytes": 30000, "dma_largest_block_bytes": 14000, "dma_min_free_bytes": 10000,
    "psram_free_bytes": 6000000, "psram_largest_block_bytes": 2800000, "psram_min_free_bytes": 2000000,
    "alloc_failures": 7, "alloc_failures_dropped": 2,
    "recent_alloc_failures": [{"bytes": 1024, "caps": 8}, {"bytes": 86, "caps": 8}],
    "stacks": [{"name": "add_heartbeat", "free_bytes": 2100}, {"name": "zone_gateway", "free_bytes": 5200}],
}


def test_heartbeat_memory_evidence_is_stored(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="memory-boot", sequence=1, payload=HeartbeatPayload(
        firmware_version="zone-lite-2.7.1", uptime_seconds=600,
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        diagnostics={"memory": MEMORY}))
    db.flush()
    assert connector.firmware_diagnostics["memory"] == MEMORY
    telemetry = db.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == connector.id))
    assert telemetry.payload["diagnostics"]["memory"]["stacks"][0] == {"name": "add_heartbeat", "free_bytes": 2100}


def test_older_firmware_memory_sample_is_still_accepted(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="old-boot", sequence=1, payload=HeartbeatPayload(
        firmware_version="zone-lite-2.6.15", uptime_seconds=600,
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        diagnostics={"memory": {"internal_free_bytes": 50000, "internal_largest_block_bytes": 20000}}))
    db.flush()
    memory = connector.firmware_diagnostics["memory"]
    assert memory["internal_free_bytes"] == 50000 and memory["stacks"] == [] and memory["alloc_failures"] is None

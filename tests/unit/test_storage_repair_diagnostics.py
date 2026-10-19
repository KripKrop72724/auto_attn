"""ZKT 2.7.5: ADD accepts the boot storage repair's report in every heartbeat,
keeps it with the device's firmware diagnostics, and drops an unreadable one
rather than rejecting the heartbeat that carries it."""
import copy

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.schemas import HeartbeatPayload, StorageDiagnostics, StorageRepairDiagnostics
from zk_add.service import update_heartbeat

REPAIR = {
    "result": "REPAIRED_WITH_GAPS", "mode": "FULL", "phase": "IDLE", "previous_phase": "IDLE",
    "previous_boot_phase": "CHECK", "previous_boot_deadline": False, "previous_boot_returned": False,
    "generation": 7, "incomplete": 0, "failures": 0, "level": 0, "check_skipped": False, "mirror": True,
    "commands_damaged": False, "duration_ms": 4120, "check_ms": 2210, "budget_bytes": 6291456,
    "files": 9, "files_held": 6, "bytes_held": 4612345, "files_unheld_custody": 0,
    "gaps": 1, "unreadable_bytes": 251, "records_reported": 0, "records_pending": 1, "records_dropped": 0,
    "spans_rewritten": 2, "index_rewritten": 1, "duplicates_removed": 1, "orphan_index_removed": 5,
    "blocks_compacted": 3, "reserve_pages": 4, "room_needed_pages": 6, "room_erased_pages": 0, "sizes_recovered": 1, "strays_removed": 2, "files_superseded": 0,
    "lookup_fixed": 0, "tails_terminated": 1, "files_restored": 0, "files_rebuilt": 0, "cursors_reset": 1,
    "expendables_removed": 1, "settled_removed": 0, "interrupted_losses": 0,
    "used_before": 4930000, "used_after": 3540000, "total_bytes": 7700000,
    "nvs_free_entries_before": 312, "nvs_free_entries_after": 300,
    "nvs_available_entries_before": 186, "nvs_available_entries_after": 174,
    "internal_largest_before": 77824, "internal_largest_after": 77824, "stack_free_bytes": 3100,
    "log_lines_dropped": 0,
    "check": {"passes": 1, "fix_index": 0, "fix_lookup": 3, "delete_orphaned_index": 2, "delete_page": 5512,
              "delete_bad_file": 1, "deleted_live_files": 0, "errors": 0, "last_error": 0},
    "entries": [{"name": "/blocked_identity.jsonl", "class": "attendance", "size": 3826949, "gaps": 1,
                 "unreadable_bytes": 251, "spans_rewritten": 1, "actions": ["NORMALIZED", "GAPS"]},
                {"name": "/pending.jsonl", "class": "attendance", "size": 516, "gaps": 0,
                 "unreadable_bytes": 0, "spans_rewritten": 0, "actions": ["NORMALIZED", "SIZE_RECOVERED"]},
                {"name": "/add_live.jsonl", "class": "attendance", "size": 90000, "gaps": 0,
                 "unreadable_bytes": 0, "spans_rewritten": 0, "blocked_spans": 2, "blocked_offset": 50200,
                 "actions": []}],
}


def heartbeat(storage: dict) -> HeartbeatPayload:
    return HeartbeatPayload(
        firmware_version="zone-lite-2.7.5", uptime_seconds=60,
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        diagnostics={"storage": storage})


def test_a_repair_report_is_kept_with_the_heartbeat(db):
    connector = connector_fixture(db)
    update_heartbeat(db, connector=connector, boot_id="repair-boot", sequence=1,
                     payload=heartbeat({"durability": "HEALTHY", "persistence_verified": True,
                                        "recovery_complete": True, "repair": REPAIR}))
    db.flush()
    stored = connector.firmware_diagnostics["storage"]["repair"]
    assert stored["result"] == "REPAIRED_WITH_GAPS" and stored["gaps"] == 1
    assert stored["check"]["delete_page"] == 5512
    assert stored["blocks_compacted"] == 3 and stored["reserve_pages"] == 4
    assert stored["room_needed_pages"] == 6 and stored["room_erased_pages"] == 0
    assert stored["sizes_recovered"] == 1 and stored["strays_removed"] == 2 and stored["files_superseded"] == 0
    assert stored["previous_boot_returned"] is False
    assert stored["entries"][1]["actions"] == ["NORMALIZED", "SIZE_RECOVERED"]
    entry = stored["entries"][0]
    assert entry["name"] == "/blocked_identity.jsonl" and entry["actions"] == ["NORMALIZED", "GAPS"]
    # Stored under the key the firmware sent, not the model's field name.
    assert entry["class"] == "attendance" and "class_" not in entry
    assert stored["nvs_available_entries_before"] == 186 and stored["nvs_available_entries_after"] == 174
    blocked = stored["entries"][2]
    assert blocked["blocked_spans"] == 2 and blocked["blocked_offset"] == 50200


def test_later_codes_are_text_and_older_firmware_sends_none():
    later = copy.deepcopy(REPAIR)
    later["result"] = "SOME_LATER_STATE"
    later["entries"][0]["actions"] = ["A_NEW_ACTION"]
    assert StorageDiagnostics(repair=later).repair.result == "SOME_LATER_STATE"
    assert StorageDiagnostics().repair is None
    assert StorageRepairDiagnostics().result is None


def test_an_unreadable_report_is_dropped_and_the_heartbeat_accepted():
    for field, value in [("result", "lower"), ("gaps", -1), ("level", 99), ("error_stage", "Has Space"),
                         ("blocks_compacted", -3), ("sizes_recovered", -1),
                         ("unreadable_bytes", 2**33), ("entries", [{"name": "bad\u0001"}] )]:
        broken = copy.deepcopy(REPAIR)
        broken[field] = value
        storage = StorageDiagnostics(durability="HEALTHY", repair=broken)
        assert storage.repair is None and storage.durability == "HEALTHY"
    too_many = copy.deepcopy(REPAIR)
    too_many["entries"] = too_many["entries"] * 5
    assert StorageDiagnostics(repair=too_many).repair is None
    assert heartbeat({"repair": "not an object"}).diagnostics.storage.repair is None

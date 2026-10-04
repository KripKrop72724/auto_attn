import pytest

from zk_add.runtime_contract import journal_storage_status, runtime_contract, worker_snapshot_fresh
from zk_add.schemas import FirmwareDiagnostics, HeartbeatPayload


def test_legacy_requirements_are_preserved_and_journal_does_not_need_esp_oracle():
    assert runtime_contract({}).workers == {"add_delivery", "ords_delivery"}
    evidence = {"schema_version": 2, "runtime_profile": "ZKT_JOURNAL_V1",
                "journal_format": 1, "delivery_authority": "ADD"}
    contract = runtime_contract(evidence)
    assert contract.workers == {"add_delivery", "capture", "storage_owner"}
    assert contract.queues == {"journal", "legacy_migration"}
    for field in ("schema_version", "journal_format", "delivery_authority"):
        with pytest.raises(ValueError, match="INCOMPLETE"):
            runtime_contract({k: v for k, v in evidence.items() if k != field})
    with pytest.raises(ValueError, match="MISMATCH"):
        runtime_contract(evidence, "hikvision")


def test_probe_failures_and_restart_attempts_survive_schema_validation():
    value = FirmwareDiagnostics.model_validate({
        "schema_version": 2, "storage": {"persistence_probe_failures": 7,
            "persistence_probe_total_failures": 12, "persistence_probe_error": 5,
            "persistence_probe_operation": "persistence_sync"},
        "workers": [{"name": "add_delivery", "state": "RUNNING",
                     "restart_count": 1, "restart_attempts": 9}],
    }).model_dump()
    assert value["storage"]["persistence_probe_failures"] == 7
    assert value["storage"]["persistence_probe_total_failures"] == 12
    assert value["storage"]["persistence_probe_error"] == 5
    assert value["storage"]["persistence_probe_operation"] == "persistence_sync"
    assert value["workers"][0]["restart_count"] == 1
    assert value["workers"][0]["restart_attempts"] == 9


def test_journal_start_attempts_are_distinct_from_actual_starts_and_custody():
    runtime = {"observed": True, "phase": "READER_HOLD", "reader_ready": True, "writer_ready": False,
               "start_attempts": 9, "storage_starts": 1, "delivery_starts": 1, "capture_starts": 0,
               "proof_attempts": 3, "failures": 7, "sampled_uptime_ms": 42000,
               "last_progress_uptime_ms": 40000, "compatibility": "READER_PROOF_MISSING"}
    value = FirmwareDiagnostics.model_validate({"schema_version": 2, "journal_runtime": runtime}).model_dump()
    assert value["journal_runtime"] == {**runtime, "delivery_authority": None}
    assert value["runtime_profile"] is None and value["delivery_authority"] is None
    for field in ("start_attempts", "sampled_uptime_ms"):
        with pytest.raises(ValueError):
            FirmwareDiagnostics.model_validate({"journal_runtime": {**runtime, field: -1}})


@pytest.mark.parametrize("phase", ["QUIESCING", "AUTHORITY_HOLD", "BRIDGE_VALIDATION"])
def test_cutover_and_bridge_recovery_diagnostics_remain_visible(phase):
    runtime = {"observed": True, "phase": phase, "reader_ready": False, "writer_ready": False,
               "delivery_authority": "UNKNOWN", "start_attempts": 1, "storage_starts": 1,
               "delivery_starts": 1, "capture_starts": 0, "proof_attempts": 1, "failures": 1}
    report = FirmwareDiagnostics.model_validate({"schema_version": 2, "runtime_profile": "ZKT_JOURNAL_V1",
                                                "delivery_authority": "UNKNOWN", "journal_format": 1,
                                                "journal_runtime": runtime})
    assert report.journal_runtime.phase == phase
    assert report.journal_runtime.delivery_authority == "UNKNOWN"
    with pytest.raises(ValueError, match="JOURNAL_CAPABILITIES_INCOMPLETE"):
        runtime_contract(report.model_dump())
    assert HeartbeatPayload(diagnostics=report).diagnostics.delivery_authority == "UNKNOWN"
    for field in ("schema_version", "journal_format"):
        incomplete = report.model_dump()
        incomplete.pop(field)
        with pytest.raises(ValueError):
            HeartbeatPayload(diagnostics=incomplete)
    with pytest.raises(ValueError):
        HeartbeatPayload(firmware_family="hikvision", diagnostics=report)


def test_quiet_synchronous_capture_is_fresh_but_stuck_work_and_stale_tasks_are_not():
    contract = runtime_contract({"schema_version": 2, "runtime_profile": "ZKT_JOURNAL_V1",
                                 "journal_format": 1, "delivery_authority": "ADD"})
    worker = {"name": "capture", "execution_model": "ON_DEMAND", "state": "RUNNING",
              "sampled_uptime_ms": 500000, "last_activity_uptime_ms": 1, "pending_requests": 0}
    assert worker_snapshot_fresh(worker, 500, contract)
    assert not worker_snapshot_fresh(worker, 600, contract)
    assert not worker_snapshot_fresh({**worker, "sampled_uptime_ms": 510000}, 500, contract)
    assert not worker_snapshot_fresh({**worker, "pending_requests": 1}, 500, contract)
    assert worker_snapshot_fresh({**worker, "operation_started_uptime_ms": 499000}, 500, contract)
    assert not worker_snapshot_fresh({**worker, "operation_started_uptime_ms": 485000}, 500, contract)
    assert not worker_snapshot_fresh({**worker, "name": "add_delivery"}, 500, contract)
    assert not worker_snapshot_fresh({**worker, "execution_model": "TASK"}, 500, contract)
    assert not worker_snapshot_fresh(worker, 500, runtime_contract({}))


@pytest.mark.parametrize("change,expected", [({}, "HEALTHY"), ({"ready": False}, "DEGRADED"),
    ({"last_append_result": "FULL"}, "FULL"), ({"last_append_result": "UNCERTAIN"}, "DEGRADED"),
    ({"checkpoint_recovery_pending": True}, "DEGRADED"), ({"sampled_uptime_ms": 1000}, "UNKNOWN"),
    ({"sampled_uptime_ms": 200000}, "UNKNOWN"), ({"fresh": False}, "UNKNOWN"),
    ({"observed": False}, "UNKNOWN"), ({"last_filesystem_error": 5, "last_failure_operation": "old_write"}, "HEALTHY")])
def test_journal_health_uses_current_preservation_not_historical_error_counters(change, expected):
    storage = {"observed": True, "fresh": True, "ready": True, "durability": "HEALTHY",
               "checkpoint_recovery_pending": False, "sampled_uptime_ms": 99000, **change}
    assert journal_storage_status({"runtime_profile": "ZKT_JOURNAL_V1", "journal_storage": storage}, 100) == expected
    assert journal_storage_status({"runtime_profile": "ZKT_JOURNAL_V1"}, 100) == "UNKNOWN"

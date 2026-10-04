import pytest

from zk_add.runtime_contract import runtime_contract
from zk_add.schemas import FirmwareDiagnostics


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

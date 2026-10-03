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

from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def test_recent_worker_evidence_distinguishes_timestamp_skew_from_real_fault():
    path = Path(__file__).resolve().parents[2] / "scripts" / "firmware_assignment_diagnostics.py"
    spec = spec_from_file_location("firmware_assignment_diagnostics", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    def sample(state, tick):
        return SimpleNamespace(
            created_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
            uptime_seconds=100,
            payload={
                "firmware_version": "zone-lite-2.6.9",
                "diagnostics": {
                    "memory": {"internal_free_bytes": 35000},
                    "workers": [{"name": "add_delivery", "state": state,
                                 "last_activity_uptime_ms": tick}],
                },
            },
        )

    evidence = module._recent_worker_evidence(
        [sample("RUNNING", 102500), sample("FAULT", 9000), sample("RUNNING", 99000)],
        "2.6.9",
    )
    assert evidence["target_version_samples"] == 3
    assert evidence["worker_states"] == {"add_delivery:RUNNING": 2, "add_delivery:FAULT": 1}
    assert [(row["state"], row["tick_delta_ms"]) for row in evidence["anomalies"]] == [
        ("RUNNING", -2500),
    ]
    assert evidence["anomaly_reasons"] == {"tick_range": 1, "worker_state": 1}
    assert [(row["state"], row["tick_delta_ms"]) for row in evidence["state_failures"]] == [
        ("FAULT", 91000),
    ]

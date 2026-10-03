from datetime import datetime, timedelta, timezone

import pytest

from zk_add.zkt270_qualification import (
    CapacityEvidence, DeviceQualification, REQUIRED_GATES, capacity_gate, device_gate,
    nationwide_register,
)
from zk_add.zkt270_scope import TARGETS, validate_upgrade_batch

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def ready(target=TARGETS[0]):
    start = NOW - timedelta(days=8)
    return DeviceQualification(
        connector_id=target.identity.connector_id, application_sha256="a" * 64,
        installed_at=start, qualified_at=NOW, observed_until=NOW, maximum_telemetry_gap_seconds=20,
        minimum_healthy_dependency_minutes=30, add_p95_ms=5000, add_p99_ms=15000,
        oracle_p99_ms=60000, local_commit_p99_ms=500, backlog_drain_seconds=86400,
        latency_failure_minutes=0, growing_deliverable_backlog_minutes=0,
        gates={key: "PASSED" for key in REQUIRED_GATES},
        traces=[{"occurrence_id": str(i), "custody_receipt_id": f"receipt-{i}",
                 "oracle_receipt_id": i+1,
                 "observed_at": datetime(2026, 10, 5+(i % 2), 8, i, tzinfo=timezone.utc)}
                for i in range(20)],
    )


def test_scope_contains_every_active_zkt_and_all_model_profiles():
    assert len(TARGETS) == 17
    assert len({target.model for target in TARGETS}) == 6
    assert [sum(target.wave == wave for target in TARGETS) for wave in "ABCD"] == [1, 5, 5, 6]
    validate_upgrade_batch([TARGETS[0].identity], passed=set(), active=set(), paused=False)
    with pytest.raises(ValueError, match="PREVIOUS_WAVE"):
        validate_upgrade_batch([TARGETS[1].identity], passed=set(), active=set(), paused=False)
    with pytest.raises(ValueError, match="ONE_UPGRADE_PER_LOCATION"):
        validate_upgrade_batch([TARGETS[3].identity, TARGETS[4].identity],
                               passed={TARGETS[0].identity.connector_id}, active=set(), paused=False)
    with pytest.raises(ValueError, match="PAUSED"):
        validate_upgrade_batch([TARGETS[0].identity], passed=set(), active=set(), paused=True)


def test_capacity_uses_actual_record_cost_retained_storage_and_seven_day_headroom():
    evidence = CapacityEvidence(baseline_days=30, baseline_complete=True,
        peak_daily_occurrences=1000, record_bytes_max=160, segment_header_bytes=256,
        filesystem_amplification=1.2, partition_bytes=8*1024*1024,
        retained_bytes=1024*1024, recovery_reserve_bytes=1024*1024)
    result = capacity_gate(evidence)
    assert result["seven_day_occurrences"] == 14000 and result["state"] == "PASSED"
    assert capacity_gate(evidence.model_copy(update={"baseline_days": 29}))["state"] == "UNTESTED"
    assert capacity_gate(evidence.model_copy(update={"record_bytes_max": 512}))["state"] == "FAILED"
    assert capacity_gate(evidence.model_copy(update={"retained_bytes": 6*1024*1024}))["state"] == "FAILED"
    assert capacity_gate(evidence.model_copy(update={"retained_bytes": 6*1024*1024,
                                                      "peak_daily_occurrences": 0}))["state"] == "FAILED"


def test_remote_pass_does_not_manufacture_physical_qualification():
    result = device_gate(ready(), candidate_digest="a" * 64, now=NOW)
    assert result["state"] == "PASSED"
    assert result["physical_power_cut"] == result["hardware_endurance"] == "NOT_PERFORMED"
    register = nationwide_register([ready()], candidate_digest="a" * 64, now=NOW)
    assert register["denominator"] == len(register["devices"]) == 17
    assert sum(row["state"] == "UNTESTED" for row in register["devices"]) == 16
    assert register["remote_hil"] == "INCOMPLETE"


@pytest.mark.parametrize("change,reason", [
    ({"application_sha256": "b" * 64}, "WRONG_APPLICATION"),
    ({"latency_failure_minutes": 10}, "PERFORMANCE_PROMOTION_PAUSED"),
    ({"growing_deliverable_backlog_minutes": 15}, "PERFORMANCE_PROMOTION_PAUSED"),
    ({"incidents": ("FALSE_IDENTITY",)}, "FALSE_IDENTITY"),
    ({"add_p99_ms": 15001}, "LATENCY_OR_CATCHUP_TARGET_FAILED"),
])
def test_promotion_halts_for_regression(change, reason):
    result = device_gate(ready().model_copy(update=change), candidate_digest="a" * 64, now=NOW)
    assert result["state"] == "FAILED" and reason in result["reasons"]


def test_short_stale_or_missing_evidence_never_passes():
    for change in ({"installed_at": NOW-timedelta(hours=1)}, {"gates": {}},
                   {"traces": ()}, {"maximum_telemetry_gap_seconds": 46},
                   {"incidents": ("NEW_UNCLASSIFIED_FAILURE",)},
                   {"qualified_at": None}, {"qualified_at": NOW-timedelta(days=6)}):
        assert device_gate(ready().model_copy(update=change), candidate_digest="a" * 64,
                           now=NOW)["state"] != "PASSED"


def test_fleet_observation_starts_after_final_qualification_not_final_install():
    later = NOW + timedelta(days=14)
    evidence = [ready(target).model_copy(update={"observed_until": later}) for target in TARGETS]
    early = nationwide_register(evidence, candidate_digest="a" * 64, now=later,
                               fleet_observation_started_at=NOW-timedelta(days=7))
    assert early["remote_hil"] == "INCOMPLETE"
    valid = nationwide_register(evidence, candidate_digest="a" * 64, now=later,
                               fleet_observation_started_at=NOW)
    assert valid["remote_hil"] == "PASSED"
    assert valid["production_qualification"] == "INCOMPLETE"
    with pytest.raises(ValueError, match="timezone"):
        nationwide_register(evidence, candidate_digest="a" * 64, now=later,
                            fleet_observation_started_at=NOW.replace(tzinfo=None))

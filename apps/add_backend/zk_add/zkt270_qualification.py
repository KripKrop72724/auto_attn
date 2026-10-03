"""Measurable release gates. Only trusted collectors should supply evidence.

These evaluators do not deploy, sign, or turn an operator checkbox into proof.
Unknown inputs remain untested; remote evidence cannot certify physical faults.
"""
from datetime import datetime, timedelta
from math import ceil
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

from zk_add.zkt270_scope import BY_ID, TARGETS

State = Literal["PASSED", "FAILED", "BLOCKED", "UNTESTED"]


class CapacityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_days: int = Field(ge=0, le=366)
    baseline_complete: bool
    peak_daily_occurrences: int = Field(ge=0)
    record_bytes_max: int = Field(gt=0, le=8192)
    segment_header_bytes: int = Field(ge=0, lt=65536)
    filesystem_amplification: float = Field(ge=1, allow_inf_nan=False)
    partition_bytes: int = Field(gt=0)
    retained_bytes: int = Field(ge=0)
    recovery_reserve_bytes: int = Field(ge=1048576)


def capacity_gate(evidence: CapacityEvidence) -> dict:
    # record_bytes_max includes encryption/tag/nonce, framing and checkpoint
    # allocation. Amplification is measured on the retained partition layout.
    observations = 7 * 2 * evidence.peak_daily_occurrences
    records_per_segment = (65536 - evidence.segment_header_bytes) // evidence.record_bytes_max
    if records_per_segment < 1:
        return {"state": "FAILED", "reason": "RECORD_EXCEEDS_SEGMENT"}
    segments = ceil(observations / records_per_segment)
    needed = ceil((observations * evidence.record_bytes_max + segments * evidence.segment_header_bytes)
                  * evidence.filesystem_amplification)
    available_raw = evidence.partition_bytes * 75 // 100 - evidence.retained_bytes - evidence.recovery_reserve_bytes
    available = max(0, available_raw)
    state = "FAILED" if available_raw < 0 else "UNTESTED" if evidence.baseline_days < 30 or not evidence.baseline_complete else (
        "PASSED" if needed <= available else "FAILED")
    return {"state": state, "seven_day_occurrences": observations,
            "required_bytes": needed, "available_bytes": available,
            "reason": "BASELINE_INCOMPLETE" if state == "UNTESTED" else
                      "SEVEN_DAYS_FIT" if state == "PASSED" else "INSUFFICIENT_ESP_CAPACITY"}


class Trace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurrence_id: str = Field(min_length=1, max_length=128)
    custody_receipt_id: str = Field(min_length=1, max_length=128)
    oracle_receipt_id: int = Field(gt=0)
    observed_at: AwareDatetime


class DeviceQualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connector_id: str
    application_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    installed_at: AwareDatetime
    observed_until: AwareDatetime
    # First completed device qualification for this exact application. The
    # trusted collector records this once; later telemetry does not move it.
    qualified_at: AwareDatetime | None = None
    maximum_telemetry_gap_seconds: int = Field(ge=0)
    minimum_healthy_dependency_minutes: int = Field(ge=0)
    add_p95_ms: int = Field(ge=0)
    add_p99_ms: int = Field(ge=0)
    oracle_p99_ms: int = Field(ge=0)
    local_commit_p99_ms: int = Field(ge=0)
    backlog_drain_seconds: int = Field(ge=0)
    latency_failure_minutes: int = Field(ge=0)
    growing_deliverable_backlog_minutes: int = Field(ge=0)
    traces: tuple[Trace, ...] = ()
    gates: dict[str, State] = Field(default_factory=dict)
    incidents: tuple[str, ...] = ()


REQUIRED_GATES = frozenset({
    "backup_restore", "protocol_profile", "seven_day_capacity", "custody_and_dedup",
    "source_chain", "identity", "oracle_content", "compatible_rollback", "migration_interruptions",
    "signed_image", "resource_bounds", "automated_seven_day_soak", "add_interruption_30s",
    "esp_reboot", "prerequisites",
})
HALT_INCIDENTS = frozenset({"MISSING_EVIDENCE", "FALSE_IDENTITY", "INCOMPATIBLE_ROLLBACK",
                           "BOOT_FAILURE", "CURSOR_CORRUPTION", "CHAIN_CORRUPTION",
                           "RECURRING_PERSISTENCE_FAILURE"})


def device_gate(value: DeviceQualification, *, candidate_digest: str, now: datetime) -> dict:
    if now.tzinfo is None:
        raise ValueError("Qualification time requires a timezone")
    target = BY_ID.get(value.connector_id)
    if target is None:
        raise ValueError("TARGET_OUTSIDE_APPROVED_SCOPE")
    reasons = []
    failed = set(value.incidents) & HALT_INCIDENTS
    reasons.extend(f"UNCLASSIFIED_INCIDENT:{code}" for code in set(value.incidents) - HALT_INCIDENTS)
    if value.application_sha256 != candidate_digest:
        failed.add("WRONG_APPLICATION")
    if value.observed_until > now or value.observed_until < value.installed_at:
        failed.add("INVALID_OBSERVATION_INTERVAL")
    if value.latency_failure_minutes >= 10 or value.growing_deliverable_backlog_minutes >= 15:
        failed.add("PERFORMANCE_PROMOTION_PAUSED")
    for key in REQUIRED_GATES:
        state = value.gates.get(key, "UNTESTED")
        if state == "FAILED":
            failed.add(key)
        elif state != "PASSED":
            reasons.append(f"{key}:{state}")
    if value.observed_until - value.installed_at < timedelta(hours=target.hours):
        reasons.append("OBSERVATION_WINDOW_INCOMPLETE")
    if value.qualified_at is None:
        reasons.append("QUALIFICATION_COMPLETION_MISSING")
    elif not value.installed_at + timedelta(hours=target.hours) <= value.qualified_at <= value.observed_until:
        failed.add("INVALID_QUALIFICATION_COMPLETION")
    if (now - value.observed_until).total_seconds() > 45 or value.maximum_telemetry_gap_seconds > 45:
        reasons.append("TELEMETRY_COVERAGE_INCOMPLETE")
    traces = {trace.occurrence_id: trace for trace in value.traces}
    if len(traces) != len(value.traces) or len({trace.oracle_receipt_id for trace in value.traces}) != len(traces):
        failed.add("REPEATED_ATTENDANCE_PROOF")
    if any(not value.installed_at <= trace.observed_at <= value.observed_until for trace in value.traces):
        failed.add("TRACE_OUTSIDE_OBSERVATION")
    qualified_traces = [trace for trace in traces.values()
                        if value.qualified_at is not None and trace.observed_at <= value.qualified_at]
    working_days = {trace.observed_at.astimezone(ZoneInfo("Asia/Karachi")).date()
                    for trace in qualified_traces if trace.observed_at.astimezone(ZoneInfo("Asia/Karachi")).weekday() < 5}
    if len(qualified_traces) < 20 or len(working_days) < 2:
        reasons.append("ORDINARY_ATTENDANCE_PROOF_INCOMPLETE")
    if value.minimum_healthy_dependency_minutes < 15:
        reasons.append("HEALTHY_DEPENDENCY_LOAD_WINDOW_MISSING")
    if (value.add_p95_ms > 5000 or value.add_p99_ms > 15000 or value.oracle_p99_ms > 60000
            or value.local_commit_p99_ms > 500 or value.backlog_drain_seconds > 86400):
        failed.add("LATENCY_OR_CATCHUP_TARGET_FAILED")
    return {"connector_id": value.connector_id,
            "state": "FAILED" if failed else "BLOCKED" if reasons else "PASSED",
            "reasons": sorted(failed) + sorted(reasons), "wave": target.wave,
            "physical_power_cut": "NOT_PERFORMED", "hardware_endurance": "NOT_PERFORMED"}


def nationwide_register(evidence: list[DeviceQualification], *, candidate_digest: str,
                        now: datetime, fleet_observation_started_at: datetime | None = None) -> dict:
    rows = {row.connector_id: row for row in evidence}
    if len(rows) != len(evidence) or not rows.keys() <= BY_ID.keys():
        raise ValueError("DUPLICATED_OR_UNAPPROVED_TARGET")
    results = [device_gate(rows[target.identity.connector_id], candidate_digest=candidate_digest, now=now)
               if target.identity.connector_id in rows else {
                   "connector_id": target.identity.connector_id, "wave": target.wave,
                   "state": "UNTESTED", "reasons": ["DEVICE_EVIDENCE_MISSING"],
               } for target in TARGETS]
    all_passed = all(row["state"] == "PASSED" for row in results)
    if fleet_observation_started_at is not None and fleet_observation_started_at.tzinfo is None:
        raise ValueError("Fleet observation time requires a timezone")
    final_qualification = max((row.qualified_at for row in evidence if row.qualified_at), default=now)
    fleet_passed = bool(all_passed and fleet_observation_started_at and
                        final_qualification <= fleet_observation_started_at <= now - timedelta(days=14))
    return {"denominator": 17, "devices": results,
            "remote_hil": "PASSED" if fleet_passed else "INCOMPLETE",
            "fleet_observation": "PASSED" if fleet_passed else "UNTESTED",
            "physical_power_cut": "NOT_PERFORMED", "hardware_endurance": "NOT_PERFORMED",
            "production_qualification": "INCOMPLETE"}

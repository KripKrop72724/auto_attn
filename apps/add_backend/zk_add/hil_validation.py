"""Fail-closed Islamabad smoke-test evaluation of server-collected evidence.

This evaluator does not authorize commands, publish releases, or promote firmware.
Its input must be assembled from authenticated ADD records, not an operator's
self-attested checkboxes. Hardware endurance and power-cut gates remain separate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from zk_add.hil_scope import HilTarget


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseIdentity(EvidenceModel):
    version: str = Field(min_length=1, max_length=80)
    git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_id: str = Field(min_length=1, max_length=80)


class SmokeSample(EvidenceModel):
    telemetry_id: int = Field(gt=0)
    recorded_at: AwareDatetime
    diagnostics_at: AwareDatetime
    target: HilTarget
    release: ReleaseIdentity
    boot_id: str = Field(min_length=1, max_length=100)
    storage_healthy: bool | None = None
    persistence_verified: bool | None = None
    recovery_complete: bool | None = None
    workers_healthy: bool | None = None
    counts_known: bool | None = None
    terminal_certified: bool | None = None
    source_generation: int | None = Field(default=None, ge=0)
    committed_cursor: int | None = Field(default=None, ge=0)
    source_count: int | None = Field(default=None, ge=0)
    write_failures: int | None = Field(default=None, ge=0)
    worker_restarts: int | None = Field(default=None, ge=0)
    message_rejections: int | None = Field(default=None, ge=0)


class RecoveryTest(EvidenceModel):
    command_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    kind: Literal["ADD_INTERRUPT_30S", "ESP_REBOOT"]
    target: HilTarget
    issued_at: AwareDatetime
    started_at: AwareDatetime
    expires_at: AwareDatetime
    recovered_at: AwareDatetime
    outcome: Literal["SUCCEEDED", "FAILED", "INCOMPLETE"]
    durable_dedup_verified: bool = False
    safe_checkpoint_verified: bool = False
    automatic_expiry_verified: bool = False
    interruption_observed: bool = False
    interruption_seconds: int | None = None
    boot_before: str = Field(min_length=1, max_length=100)
    boot_after: str = Field(min_length=1, max_length=100)


class AttendanceProof(EvidenceModel):
    event_id: int = Field(gt=0)
    observed_at: AwareDatetime
    logical_record_count: int = Field(ge=0)
    oracle_receipt_id: int | None = Field(default=None, gt=0)
    exception_receipt_id: str | None = Field(default=None, min_length=1, max_length=100)


class SmokeEvidence(EvidenceModel):
    run_id: str = Field(min_length=1, max_length=100)
    deployment_id: str = Field(min_length=1, max_length=100)
    target: HilTarget
    release: ReleaseIdentity
    release_state: str
    deployment_state: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    ready_at: AwareDatetime
    samples: tuple[SmokeSample, ...] = ()
    recovery_tests: tuple[RecoveryTest, ...] = ()
    attendance: tuple[AttendanceProof, ...] = ()
    certificate_ids: tuple[str, ...] = ()
    source_continuity_verified: bool | None = None
    tail_verified_at: AwareDatetime | None = None
    preserved_evidence_before: frozenset[str] = frozenset()
    preserved_evidence_after: frozenset[str] = frozenset()
    transferred_evidence_receipts: frozenset[str] = frozenset()
    queue_preservation_verified: bool | None = None
    alert_review_complete: bool | None = None
    unresolved_regression: bool | None = None


class SmokeVerdict(EvidenceModel):
    outcome: Literal["PASS", "FAILED", "INCOMPLETE"]
    reasons: tuple[str, ...]
    physical_power_cut: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    hardware_endurance: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"


def evaluate_smoke(evidence: SmokeEvidence, *, now: datetime) -> SmokeVerdict:
    failed: list[str] = []
    missing: list[str] = []
    start, end = evidence.started_at, evidence.ended_at
    if now.tzinfo is None:
        raise ValueError("Validation time must include its timezone")
    if end < start or end > now or evidence.ready_at > start:
        missing.append("INVALID_OBSERVATION_INTERVAL")
    if (end - start).total_seconds() < 900:
        missing.append("OBSERVATION_UNDER_15_MINUTES")
    if evidence.release_state != "HIL_ONLY" or evidence.deployment_state != "SUCCEEDED":
        failed.append("RELEASE_OR_DEPLOYMENT_NOT_ELIGIBLE")
    if evidence.unresolved_regression is True:
        failed.append("UNRESOLVED_REGRESSION")
    elif evidence.unresolved_regression is None:
        missing.append("REGRESSION_REVIEW_MISSING")
    for name in (
        "source_continuity_verified",
        "queue_preservation_verified",
        "alert_review_complete",
    ):
        value = getattr(evidence, name)
        if value is False:
            failed.append(name.upper())
        elif value is None:
            missing.append(name.upper())
    if not evidence.certificate_ids or any(not value for value in evidence.certificate_ids):
        missing.append("RECONCILIATION_CERTIFICATE_MISSING")
    if (
        evidence.tail_verified_at is None
        or not start + timedelta(minutes=8) <= evidence.tail_verified_at <= end
    ):
        missing.append("FINAL_TAIL_VERIFICATION_MISSING")
    if not evidence.preserved_evidence_before <= (
        evidence.preserved_evidence_after | evidence.transferred_evidence_receipts
    ):
        failed.append("PRESERVED_EVIDENCE_MISSING")

    tests = evidence.recovery_tests
    if len(tests) != 2 or {test.kind for test in tests} != {"ADD_INTERRUPT_30S", "ESP_REBOOT"}:
        missing.append("RECOVERY_TESTS_MISSING_OR_REPEATED")
    if len({test.command_id for test in tests}) != len(tests):
        failed.append("REUSED_RECOVERY_COMMAND")
    valid_tests = []
    for test in tests:
        valid = True
        if test.run_id != evidence.run_id or test.target != evidence.target:
            failed.append("RECOVERY_SCOPE_MISMATCH")
            valid = False
        if test.outcome != "SUCCEEDED":
            (failed if test.outcome == "FAILED" else missing).append("RECOVERY_" + test.outcome)
            valid = False
        if not test.durable_dedup_verified or not test.safe_checkpoint_verified:
            missing.append("RECOVERY_DURABILITY_UNVERIFIED")
            valid = False
        minute_from, minute_to = (3, 5) if test.kind == "ADD_INTERRUPT_30S" else (5, 8)
        if not (
            start <= test.issued_at <= test.started_at < test.expires_at <= end
            and start + timedelta(minutes=minute_from)
            <= test.started_at
            < test.recovered_at
            <= start + timedelta(minutes=minute_to)
        ):
            failed.append("RECOVERY_TIME_INVALID")
            valid = False
        if test.kind == "ADD_INTERRUPT_30S":
            if (
                test.interruption_seconds != 30
                or not test.automatic_expiry_verified
                or not test.interruption_observed
                or test.boot_before != test.boot_after
            ):
                failed.append("TRANSPORT_RECOVERY_INVALID")
                valid = False
        elif test.boot_before == test.boot_after:
            missing.append("REBOOT_NOT_OBSERVED")
            valid = False
        if valid:
            valid_tests.append(test)

    samples = sorted(evidence.samples, key=lambda item: item.recorded_at)
    if len({sample.telemetry_id for sample in samples}) != len(samples):
        failed.append("DUPLICATE_TELEMETRY")
    if (
        not samples
        or samples[0].recorded_at > start + timedelta(seconds=45)
        or samples[-1].recorded_at < end - timedelta(seconds=45)
    ):
        missing.append("OBSERVATION_TELEMETRY_INCOMPLETE")
    for sample in samples:
        if sample.target != evidence.target or sample.release != evidence.release:
            failed.append("DEVICE_OR_ARTIFACT_MISMATCH")
        if not start <= sample.recorded_at <= end:
            failed.append("TELEMETRY_OUTSIDE_OBSERVATION")
        if not timedelta(0) <= sample.recorded_at - sample.diagnostics_at <= timedelta(seconds=45):
            missing.append("STALE_DIAGNOSTICS")
        for name in (
            "storage_healthy",
            "persistence_verified",
            "recovery_complete",
            "workers_healthy",
            "counts_known",
            "terminal_certified",
        ):
            value = getattr(sample, name)
            if value is not True:
                (failed if value is False else missing).append(name.upper())
        for name in (
            "source_generation",
            "committed_cursor",
            "source_count",
            "write_failures",
            "worker_restarts",
            "message_rejections",
        ):
            if getattr(sample, name) is None:
                missing.append(name.upper() + "_MISSING")
    boot_changes = 0
    for previous, sample in zip(samples, samples[1:]):
        if sample.recorded_at - previous.recorded_at > timedelta(seconds=45):
            covered = any(
                previous.recorded_at >= test.started_at - timedelta(seconds=45)
                and sample.recorded_at <= test.recovered_at + timedelta(seconds=45)
                for test in valid_tests
            )
            if not covered:
                missing.append("UNEXPLAINED_TELEMETRY_GAP")
        if sample.boot_id != previous.boot_id:
            boot_changes += 1
            if not any(
                test.kind == "ESP_REBOOT"
                and test.boot_before == previous.boot_id
                and test.boot_after == sample.boot_id
                and previous.recorded_at
                <= test.started_at
                <= sample.recorded_at
                <= test.recovered_at + timedelta(seconds=45)
                for test in valid_tests
            ):
                failed.append("UNPLANNED_RESET")
        if (
            sample.source_generation is not None
            and previous.source_generation is not None
            and sample.source_generation != previous.source_generation
        ):
            failed.append("SOURCE_GENERATION_CHANGED")
        if (
            sample.committed_cursor is not None
            and previous.committed_cursor is not None
            and sample.committed_cursor < previous.committed_cursor
        ):
            failed.append("SOURCE_CURSOR_REGRESSED")
        for name in ("write_failures", "worker_restarts", "message_rejections"):
            before, after = getattr(previous, name), getattr(sample, name)
            if (
                before is not None
                and after is not None
                and sample.boot_id == previous.boot_id
                and after < before
            ):
                failed.append("COUNTER_REGRESSED_" + name.upper())
            if (
                before is not None
                and after is not None
                and (after > before if sample.boot_id == previous.boot_id else after > 0)
            ):
                failed.append("NEW_" + name.upper())
    if boot_changes != 1:
        missing.append("EXACTLY_ONE_CONTROLLED_REBOOT_REQUIRED")
    if samples and samples[-1].committed_cursor != samples[-1].source_count:
        missing.append("SOURCE_TAIL_UNFINISHED")

    if not evidence.attendance:
        missing.append("NO_QUALIFYING_ATTENDANCE")
    if len({row.event_id for row in evidence.attendance}) != len(evidence.attendance):
        failed.append("DUPLICATE_ATTENDANCE_EVIDENCE")
    for row in evidence.attendance:
        if not start <= row.observed_at <= end:
            failed.append("ATTENDANCE_OUTSIDE_OBSERVATION")
        if row.logical_record_count != 1:
            failed.append("ATTENDANCE_LOGICAL_COUNT_INVALID")
        if bool(row.oracle_receipt_id) == bool(row.exception_receipt_id):
            missing.append("ATTENDANCE_SETTLEMENT_UNPROVEN")
    for test in valid_tests:
        if not any(
            row.oracle_receipt_id
            and test.started_at - timedelta(seconds=60)
            <= row.observed_at
            <= test.recovered_at + timedelta(seconds=60)
            for row in evidence.attendance
        ):
            missing.append("NO_ORACLE_ATTENDANCE_AROUND_" + test.kind)
    reasons = tuple(dict.fromkeys(failed + missing))
    return SmokeVerdict(
        outcome="FAILED" if failed else "INCOMPLETE" if missing else "PASS", reasons=reasons
    )

from datetime import datetime, timedelta, timezone

import pytest

from zk_add.hil_scope import HilTarget
from zk_add.hil_validation import (
    AttendanceProof,
    RecoveryTest,
    ReleaseIdentity,
    SmokeEvidence,
    SmokeSample,
    evaluate_smoke,
)


@pytest.fixture
def evidence():
    start = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    target = HilTarget(
        connector_id="connector-one", mac="e0:72:a1:d6:3c:7c", terminal_serial="PGB1261200077"
    )
    release = ReleaseIdentity(
        version="2.6.0",
        git_sha="a" * 40,
        artifact_sha256="b" * 64,
        application_sha256="c" * 64,
        signing_key_id="production",
    )
    samples = tuple(
        SmokeSample(
            telemetry_id=i + 1,
            recorded_at=start + timedelta(seconds=i * 30),
            diagnostics_at=start + timedelta(seconds=i * 30),
            target=target,
            release=release,
            boot_id="before" if i < 13 else "after",
            storage_healthy=True,
            persistence_verified=True,
            recovery_complete=True,
            workers_healthy=True,
            counts_known=True,
            terminal_certified=True,
            source_generation=3,
            committed_cursor=100 + i,
            source_count=100 + i,
            write_failures=0,
            read_failures=0,
            worker_restarts=0,
            message_rejections=0,
        )
        for i in range(31)
    )
    tests = tuple(
        RecoveryTest(
            command_id=kind,
            run_id="run-one",
            kind=kind,
            target=target,
            issued_at=start + timedelta(seconds=second - 5),
            started_at=start + timedelta(seconds=second),
            expires_at=start + timedelta(seconds=second + 45),
            recovered_at=start + timedelta(seconds=second + 40),
            outcome="SUCCEEDED",
            durable_dedup_verified=True,
            safe_checkpoint_verified=True,
            automatic_expiry_verified=kind == "ADD_INTERRUPT_30S",
            interruption_observed=kind == "ADD_INTERRUPT_30S",
            interruption_seconds=30 if kind == "ADD_INTERRUPT_30S" else None,
            boot_before="before",
            boot_after="before" if kind == "ADD_INTERRUPT_30S" else "after",
        )
        for kind, second in (("ADD_INTERRUPT_30S", 210), ("ESP_REBOOT", 370))
    )
    return SmokeEvidence(
        run_id="run-one",
        deployment_id="deployment-one",
        target=target,
        release=release,
        release_state="HIL_ONLY",
        deployment_state="SUCCEEDED",
        started_at=start,
        ended_at=start + timedelta(minutes=15),
        ready_at=start - timedelta(seconds=10),
        samples=samples,
        recovery_tests=tests,
        attendance=tuple(
            AttendanceProof(
                event_id=i,
                observed_at=start + timedelta(seconds=second),
                logical_record_count=1,
                oracle_receipt_id=i,
            )
            for i, second in ((1, 220), (2, 380))
        ),
        certificate_ids=("certificate-one",),
        source_continuity_verified=True,
        tail_verified_at=start + timedelta(minutes=14),
        preserved_evidence_before=frozenset({"old"}),
        preserved_evidence_after=frozenset({"old"}),
        queue_preservation_verified=True,
        alert_review_complete=True,
        unresolved_regression=False,
    )


def verdict(evidence):
    return evaluate_smoke(evidence, now=evidence.ended_at + timedelta(seconds=5))


def replace_sample(evidence, index, **updates):
    rows = list(evidence.samples)
    rows[index] = rows[index].model_copy(update=updates)
    return evidence.model_copy(update={"samples": tuple(rows)})


def test_complete_smoke_never_claims_endurance_or_promotion(evidence):
    result = verdict(evidence)
    assert result.outcome == "PASS" and result.reasons == ()
    assert result.physical_power_cut == result.hardware_endurance == "NOT_PERFORMED"
    assert evidence.release_state == "HIL_ONLY"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("attendance", (), "NO_QUALIFYING_ATTENDANCE"),
        ("certificate_ids", (), "RECONCILIATION_CERTIFICATE_MISSING"),
        ("tail_verified_at", None, "FINAL_TAIL_VERIFICATION_MISSING"),
        ("source_continuity_verified", None, "SOURCE_CONTINUITY_VERIFIED"),
        ("queue_preservation_verified", None, "QUEUE_PRESERVATION_VERIFIED"),
        ("alert_review_complete", None, "ALERT_REVIEW_COMPLETE"),
        ("unresolved_regression", None, "REGRESSION_REVIEW_MISSING"),
    ],
)
def test_absent_proof_is_incomplete(evidence, field, value, reason):
    result = verdict(evidence.model_copy(update={field: value}))
    assert result.outcome == "INCOMPLETE" and reason in result.reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("storage_healthy", False),
        ("workers_healthy", False),
        ("terminal_certified", False),
        ("recovery_complete", False),
        ("write_failures", 1),
        ("read_failures", 1),
        ("worker_restarts", 1),
        ("message_rejections", 1),
        ("source_generation", 4),
        ("committed_cursor", 1),
        ("boot_id", "unexpected"),
        ("telemetry_id", 1),
    ],
)
def test_unsafe_observation_fails(evidence, field, value):
    assert verdict(replace_sample(evidence, 20, **{field: value})).outcome == "FAILED"


@pytest.mark.parametrize(
    "field",
    [
        "storage_healthy",
        "persistence_verified",
        "counts_known",
        "write_failures",
        "read_failures",
        "source_generation",
    ],
)
def test_old_firmware_cannot_pass_missing_diagnostics(evidence, field):
    assert verdict(replace_sample(evidence, 20, **{field: None})).outcome != "PASS"


def test_wrong_serial_spare_and_changed_artifact_fail(evidence):
    for field, value in (
        ("terminal_serial", "replacement"),
        ("mac", "e0:72:a1:d6:f3:28"),
        ("connector_id", "spare"),
    ):
        wrong = evidence.target.model_copy(update={field: value})
        assert verdict(replace_sample(evidence, 20, target=wrong)).outcome == "FAILED"
    wrong = evidence.release.model_copy(update={"application_sha256": "d" * 64})
    assert verdict(replace_sample(evidence, 20, release=wrong)).outcome == "FAILED"


def test_elapsed_time_alone_and_unfinished_setup_cannot_pass(evidence):
    result = verdict(
        evidence.model_copy(update={"samples": (), "attendance": (), "recovery_tests": ()})
    )
    assert result.outcome == "INCOMPLETE"
    assert (
        verdict(
            evidence.model_copy(update={"ready_at": evidence.started_at + timedelta(seconds=1)})
        ).outcome
        == "INCOMPLETE"
    )
    assert (
        verdict(
            evidence.model_copy(update={"started_at": evidence.started_at + timedelta(seconds=1)})
        ).outcome
        != "PASS"
    )


def test_stale_telemetry_and_unexplained_gaps_are_incomplete(evidence):
    row = evidence.samples[20]
    assert (
        verdict(
            replace_sample(evidence, 20, diagnostics_at=row.recorded_at - timedelta(seconds=46))
        ).outcome
        == "INCOMPLETE"
    )
    samples = evidence.samples[:20] + evidence.samples[23:]
    assert verdict(evidence.model_copy(update={"samples": samples})).outcome == "INCOMPLETE"


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "another-run"},
        {"outcome": "FAILED"},
        {"interruption_seconds": 60},
        {"automatic_expiry_verified": False},
        {"interruption_observed": False},
    ],
)
def test_transport_recovery_requires_exact_scope_and_observed_expiry(evidence, change):
    test = evidence.recovery_tests[0].model_copy(update=change)
    result = verdict(
        evidence.model_copy(update={"recovery_tests": (test, evidence.recovery_tests[1])})
    )
    assert result.outcome == "FAILED"


def test_missing_dedup_or_checkpoint_cannot_pass(evidence):
    for field in ("durable_dedup_verified", "safe_checkpoint_verified"):
        test = evidence.recovery_tests[0].model_copy(update={field: False})
        assert (
            verdict(
                evidence.model_copy(update={"recovery_tests": (test, evidence.recovery_tests[1])})
            ).outcome
            != "PASS"
        )


def test_preserved_exceptions_need_retention_or_durable_transfer(evidence):
    missing = evidence.model_copy(update={"preserved_evidence_after": frozenset()})
    assert verdict(missing).outcome == "FAILED"
    assert (
        verdict(
            missing.model_copy(update={"transferred_evidence_receipts": frozenset({"old"})})
        ).outcome
        == "PASS"
    )


def test_new_counters_after_controlled_reboot_fail(evidence):
    assert verdict(replace_sample(evidence, 13, write_failures=1)).outcome == "FAILED"


def test_oracle_attendance_required_around_both_recovery_tests(evidence):
    rows = tuple(
        row.model_copy(update={"oracle_receipt_id": None, "exception_receipt_id": "custody"})
        for row in evidence.attendance
    )
    assert verdict(evidence.model_copy(update={"attendance": rows})).outcome == "INCOMPLETE"
    duplicate = evidence.attendance[0].model_copy(update={"logical_record_count": 2})
    assert (
        verdict(
            evidence.model_copy(update={"attendance": (duplicate, evidence.attendance[1])})
        ).outcome
        == "FAILED"
    )


def test_revoked_release_cannot_pass(evidence):
    assert verdict(evidence.model_copy(update={"release_state": "REVOKED"})).outcome == "FAILED"


def test_future_or_naive_validation_time_rejected(evidence):
    assert evaluate_smoke(evidence, now=evidence.started_at).outcome == "INCOMPLETE"
    with pytest.raises(ValueError):
        evaluate_smoke(evidence, now=datetime(2026, 9, 16))


def test_reboot_without_a_matching_controlled_command_is_a_failure(evidence):
    result = verdict(evidence.model_copy(update={"recovery_tests": ()}))
    assert result.outcome == "FAILED"
    assert "UNPLANNED_RESET" in result.reasons

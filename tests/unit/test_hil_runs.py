from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from test_hil_scope import hil_session, target  # noqa: F401
from zk_add.hil_scope import HilTarget
from zk_add.hil_runs import cancel_run, start_run
from zk_add.models import DeviceTelemetry, ReconciliationCoverage, ReconciliationJob
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun
from zk_add.time_utils import utc_now


@pytest.fixture
def ready(hil_session):  # noqa: F811
    session, release, devices = hil_session
    device = devices[0]
    device.boot_id = "current-boot"
    campaign = FirmwareCampaign(
        campaign_id="campaign-one",
        release_id=release.id,
        zone_id=device.zone_id,
        actor="admin",
        idempotency_key="campaign-key",
        reason="HIL test",
        typed_confirmation=release.version,
        status="COMPLETED",
    )
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id="deployment-one",
        campaign_id=campaign.id,
        release_id=release.id,
        connector_id=device.id,
        status="SUCCEEDED",
        target_version=release.version,
    )
    job = ReconciliationJob(
        connector_id=device.id,
        zkt_device_id=device.zkt_device.id,
        actor="admin",
        reason="baseline",
        idempotency_key="baseline-key",
        request_digest="a" * 64,
        status="COMPLETED",
        terminal_generation=1,
        terminal_serial=target(1)["terminal_serial"],
        capture_certificate={"proof": "test"},
    )
    session.add_all([deployment, job])
    session.flush()
    coverage = ReconciliationCoverage(
        zkt_device_id=device.zkt_device.id,
        job_id=job.id,
        terminal_serial=target(1)["terminal_serial"],
        terminal_generation=1,
        certified_source_cursor=100,
        source_committed_cursor=100,
        source_chain_digest="a" * 64,
        source_committed_chain_digest="a" * 64,
        capture_state="SOURCE_CAPTURE_CERTIFIED",
        oracle_state="CONFIRMED",
    )
    diagnostics = {
        "storage": {
            "durability": "HEALTHY",
            "persistence_verified": True,
            "recovery_complete": True,
            "upgrade_ready": True,
        },
        "source_generation": 1,
        "committed_source_cursor": 100,
        "workers": [
            {"name": name, "state": "RUNNING", "last_activity_uptime_ms": 100000}
            for name in ("add_delivery", "ords_delivery")
        ],
        "queues": [
            {"name": name, "count_known": True, "records": 0}
            for name in (
                "live",
                "bulk",
                "segmented_live",
                "segmented_bulk",
                "segmented_ords",
                "segmented_blocked",
                "segmented_receipts",
                "segmented_evidence",
            )
        ],
    }
    telemetry = DeviceTelemetry(
        connector_id=device.id,
        boot_id=device.boot_id,
        uptime_seconds=100,
        created_at=utc_now(),
        payload={
            "ota": {
                "running_version": release.version,
                "image_sha256": "c" * 64,
                "running_partition": "ota_1",
                "secure_boot": True,
                "rollback_enabled": True,
            },
            "diagnostics": diagnostics,
            "zkt": {"serial": target(1)["terminal_serial"], "attendance_count": 100},
        },
    )
    session.add_all([coverage, telemetry])
    session.flush()
    return session, release, device, deployment, job, coverage, telemetry


def start(ready, key="observation-key", exact=None):
    return start_run(
        ready[0],
        deployment_id=ready[3].deployment_id,
        target=HilTarget.model_validate(exact or target(1)),
        actor="admin",
        idempotency_key=key,
    )


def test_observation_starts_after_readiness_and_is_idempotent(ready):
    run = start(ready)
    assert run.status == "OBSERVING" and run.result == {}
    assert (run.ends_at - run.started_at).total_seconds() == 900
    assert run.baseline["telemetry_id"] == ready[-1].id
    assert start(ready).id == run.id
    assert ready[1].state == "HIL_ONLY"
    assert not list(ready[0].scalars(select(FirmwareEvent)))


def test_observation_accepts_worker_tick_sampled_after_rounded_uptime(ready):
    ready[-1].payload["diagnostics"]["workers"][0]["last_activity_uptime_ms"] = 102000
    assert start(ready).status == "OBSERVING"


def test_legacy_bridge_observation_keeps_optional_journal_counts_unknown(ready):
    diagnostics = ready[-1].payload["diagnostics"]
    diagnostics.update(schema_version=2, runtime_profile="ZKT_LEGACY", delivery_authority="LEGACY_DUAL")
    diagnostics["queues"].extend([
        {"name": "journal", "count_known": False, "count_reason": "NONEMPTY_OR_UNVERIFIED"},
        {"name": "legacy_migration", "count_known": False, "count_reason": "UNVERIFIED_MIGRATION"},
    ])
    assert start(ready).status == "OBSERVING"
    assert diagnostics["queues"][-1]["count_known"] is False
    assert "records" not in diagnostics["queues"][-1]


@pytest.mark.parametrize("value", [None, True, -1, "0"])
def test_required_queue_depth_must_be_an_observed_nonnegative_integer(ready, value):
    ready[-1].payload["diagnostics"]["queues"][0]["records"] = value
    with pytest.raises(ValueError, match="required runtime queue"):
        start(ready)


@pytest.mark.parametrize("variant", ["healthy", "idle-capture", "stalled-capture", "missing-worker",
                                     "unknown-worker", "duplicate-worker", "unknown-migration", "stale-storage", "append-full"])
def test_journal_hil_requires_its_workers_and_keeps_legacy_custody_obligations(ready, variant):
    diagnostics = ready[-1].payload["diagnostics"]
    diagnostics.update(schema_version=2, runtime_profile="ZKT_JOURNAL_V1", journal_format=1, delivery_authority="ADD")
    diagnostics["workers"] = [{"name": name, "state": "RUNNING", "last_activity_uptime_ms": 100000}
                              for name in ("capture", "storage_owner", "add_delivery", "legacy_add_delivery", "legacy_ords_delivery")]
    diagnostics["journal_storage"] = {"observed": True, "fresh": True, "ready": True, "durability": "HEALTHY",
        "checkpoint_recovery_pending": False, "sampled_uptime_ms": 99000}
    diagnostics["queues"] = [{"name": name, "count_known": True, "records": 0} for name in ("journal", "legacy_migration")]
    if variant in {"idle-capture", "stalled-capture"}:
        diagnostics["workers"][0].update(execution_model="ON_DEMAND", sampled_uptime_ms=100000,
                                         last_activity_uptime_ms=0, pending_requests=0)
        if variant == "stalled-capture":
            diagnostics["workers"][0]["operation_started_uptime_ms"] = 85000
    if variant == "missing-worker":
        diagnostics["workers"].pop(0)
    if variant == "unknown-worker":
        diagnostics["workers"].append({"name": "invented-worker", "state": "RUNNING", "last_activity_uptime_ms": 100000})
    if variant == "duplicate-worker":
        diagnostics["workers"].append(diagnostics["workers"][0])
    if variant == "unknown-migration":
        diagnostics["queues"][1]["count_known"] = False
    if variant == "stale-storage":
        diagnostics["journal_storage"]["sampled_uptime_ms"] = 55000
    if variant == "append-full":
        diagnostics["journal_storage"]["last_append_result"] = "FULL"
    if variant in {"healthy", "idle-capture"}:
        assert start(ready).status == "OBSERVING"
    else:
        with pytest.raises(ValueError):
            start(ready)


@pytest.mark.parametrize(
    "change",
    [
        "revoked",
        "wrong-image",
        "stale",
        "old-boot",
        "spare",
        "offline",
        "recovery",
        "probe-error",
        "legacy_read_faults",
        "legacy_append_faults",
        "legacy_retire_faults",
        "legacy_error_code",
        "unknown-queue",
        "worker",
        "reconciling",
        "uncertified",
        "tail-pending",
        "wrong-serial",
        "deployment-pending",
        "campaign-paused",
    ],
)
def test_observation_cannot_start_before_every_precondition(ready, change):
    session, release, device, deployment, job, coverage, telemetry = ready
    if change == "revoked":
        release.state = "REVOKED"
    if change == "wrong-image":
        telemetry.payload["ota"]["image_sha256"] = "d" * 64
    if change == "stale":
        telemetry.created_at = utc_now() - timedelta(seconds=46)
    if change == "old-boot":
        telemetry.boot_id = "old-boot"
    if change == "spare":
        device.is_spare = True
    if change == "offline":
        device.connected = False
    if change == "recovery":
        telemetry.payload["diagnostics"]["storage"]["recovery_complete"] = False
    if change == "probe-error":
        telemetry.payload["diagnostics"]["storage"]["persistence_probe_error"] = 5
    if change.startswith("legacy_"):
        telemetry.payload["diagnostics"]["storage"][change] = 1
    if change == "unknown-queue":
        telemetry.payload["diagnostics"]["queues"][0]["count_known"] = False
    if change == "worker":
        telemetry.payload["diagnostics"]["workers"][0]["state"] = "STOPPED"
    if change == "reconciling":
        job.status = "RUNNING"
    if change == "uncertified":
        coverage.active = False
    if change == "tail-pending":
        telemetry.payload["zkt"]["attendance_count"] = 101
    if change == "wrong-serial":
        telemetry.payload["zkt"]["serial"] = "replacement"
    if change == "deployment-pending":
        deployment.status = "RECONCILING"
    if change == "campaign-paused":
        session.get(FirmwareCampaign, deployment.campaign_id).status = "PAUSED"
    session.flush()
    with pytest.raises(ValueError):
        start(ready)


def test_second_target_and_reused_key_cannot_retarget_run(ready):
    start(ready)
    with pytest.raises(ValueError, match="different scope"):
        start(ready, exact=target(2))
    with pytest.raises(ValueError, match="next exact"):
        start(ready, key="another-key", exact=target(2))


def test_concurrent_observation_is_blocked_by_service_and_database(ready):
    run = start(ready)
    with pytest.raises(ValueError, match="already has an active"):
        start(ready, key="second-key")
    duplicate = FirmwareHilRun(
        run_id="other-run",
        deployment_id=run.deployment_id,
        connector_id=run.connector_id,
        release_id=run.release_id,
        actor="other",
        idempotency_key="other",
        status="OBSERVING",
        target=run.target,
        release_identity=run.release_identity,
        baseline=run.baseline,
        started_at=run.started_at,
        ends_at=run.ends_at,
    )
    with pytest.raises(IntegrityError):
        with ready[0].begin_nested():
            ready[0].add(duplicate)
            ready[0].flush()


def test_cancellation_records_incomplete_once_and_never_promotes(ready):
    run = start(ready)
    cancel_run(ready[0], run.run_id, actor="admin")
    cancel_run(ready[0], run.run_id, actor="admin")
    ready[0].flush()
    events = list(ready[0].scalars(select(FirmwareEvent)))
    assert len(events) == 1 and events[0].state == "HIL_INCOMPLETE"
    assert run.status == "CANCELLED" and run.result["outcome"] == "INCOMPLETE"
    assert events[0].details["target"] == target(1)
    assert ready[1].state == "HIL_ONLY"
    assert start(ready, key="retry-key").status == "OBSERVING"

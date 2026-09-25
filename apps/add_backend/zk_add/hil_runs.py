"""Durable exact-target HIL observation ownership; no release promotion."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.hil_scope import HilTarget, target_matches
from zk_add.hil_validation import ReleaseIdentity
from zk_add.models import Connector, DeviceTelemetry, ReconciliationCoverage, ReconciliationJob
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareEvent,
    FirmwareHilRun,
    FirmwareRelease,
    _application_sha256,
    _ordered_hil_target,
)
from zk_add.time_utils import ensure_utc, utc_now


def _release_identity(release: FirmwareRelease) -> ReleaseIdentity:
    return ReleaseIdentity(
        version=release.version,
        git_sha=release.git_sha,
        artifact_sha256=release.image_sha256,
        application_sha256=_application_sha256(release),
        signing_key_id=release.signing_key_id,
    )


def start_run(
    session: Session, *, deployment_id: str, target: HilTarget, actor: str, idempotency_key: str
) -> FirmwareHilRun:
    if not actor or len(actor) > 120 or not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("HIL actor and idempotency key are required and bounded")
    deployment = session.scalar(
        select(FirmwareDeployment)
        .where(FirmwareDeployment.deployment_id == deployment_id)
        .with_for_update()
    )
    if deployment is None:
        raise ValueError("HIL deployment was not found")
    release = session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == deployment.release_id).with_for_update()
    )
    connector = session.scalar(
        select(Connector).where(Connector.id == deployment.connector_id).with_for_update()
    )
    if release is None or connector is None:
        raise ValueError("HIL release or device was not found")
    identity = _release_identity(release).model_dump(mode="json")
    prior = session.scalar(
        select(FirmwareHilRun).where(
            FirmwareHilRun.actor == actor, FirmwareHilRun.idempotency_key == idempotency_key
        )
    )
    if prior is not None:
        if (
            prior.deployment_id != deployment.id
            or prior.target != target.model_dump()
            or prior.release_identity != identity
        ):
            raise ValueError("HIL idempotency key belongs to a different scope")
        return prior
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    if campaign is None or campaign.status not in {"ACTIVE", "COMPLETED"}:
        raise ValueError("The HIL campaign is paused or unavailable")
    if release.state != "HIL_ONLY" or deployment.status != "SUCCEEDED":
        raise ValueError("HIL requires an installed, successful quarantined deployment")
    if _ordered_hil_target(session, release) != target or not target_matches(target, connector):
        raise ValueError("HIL target is not the next exact permitted device")
    if session.scalar(
        select(FirmwareHilRun.id).where(
            FirmwareHilRun.connector_id == connector.id, FirmwareHilRun.status == "OBSERVING"
        )
    ):
        raise ValueError("This device already has an active HIL observation")
    latest_deployment = session.scalar(
        select(FirmwareDeployment.id)
        .where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc())
        .limit(1)
    )
    if latest_deployment != deployment.id:
        raise ValueError("A newer deployment supersedes this HIL scope")
    now = utc_now()
    telemetry = session.scalar(
        select(DeviceTelemetry)
        .where(DeviceTelemetry.connector_id == connector.id)
        .order_by(DeviceTelemetry.id.desc())
        .limit(1)
    )
    if (
        not connector.connected
        or telemetry is None
        or not 0 <= (now - ensure_utc(telemetry.created_at)).total_seconds() <= 45
        or not telemetry.boot_id
        or telemetry.boot_id != connector.boot_id
    ):
        raise ValueError("Fresh telemetry from the current boot is required before observation")
    payload = telemetry.payload or {}
    ota = payload.get("ota") or {}
    if (
        ota.get("running_version") != release.version
        or ota.get("image_sha256") != identity["application_sha256"]
        or ota.get("running_partition") not in {"ota_0", "ota_1"}
        or ota.get("secure_boot") is not True
        or ota.get("rollback_enabled") is not True
    ):
        raise ValueError("Current-boot signed application evidence is missing or mismatched")
    diagnostics = payload.get("diagnostics") or {}
    storage = diagnostics.get("storage") or {}
    if (
        storage.get("durability") != "HEALTHY"
        or storage.get("persistence_verified") is not True
        or storage.get("recovery_complete") is not True
        or storage.get("error_code")
        or storage.get("upgrade_ready") is not True
    ):
        raise ValueError("Local persistence and queue recovery must be verified before observation")
    workers = diagnostics.get("workers") or []
    if {row.get("name") for row in workers} != {"add_delivery", "ords_delivery"} or len(
        workers
    ) != 2:
        raise ValueError("Both delivery workers must report their current health")
    for worker in workers:
        tick = worker.get("last_activity_uptime_ms")
        if (
            worker.get("state") not in {"RUNNING", "WAITING_NETWORK"}
            or tick is None
            or telemetry.uptime_seconds is None
            # Uptime is sampled before the diagnostics and rounded to seconds.
            or not -5_000 <= telemetry.uptime_seconds * 1000 - tick <= 90_000
        ):
            raise ValueError("Delivery workers are not healthy and fresh")
    queues = diagnostics.get("queues") or []
    required = {
        "live",
        "bulk",
        "segmented_live",
        "segmented_bulk",
        "segmented_ords",
        "segmented_blocked",
        "segmented_receipts",
        "segmented_evidence",
    }
    if (
        len({row.get("name") for row in queues}) != len(queues)
        or not required <= {row.get("name") for row in queues}
        or any(row.get("count_known") is not True or row.get("records") is None for row in queues)
    ):
        raise ValueError("Every queue must have verified recovery and known depth")
    terminal = payload.get("zkt") or {}
    if terminal.get("serial") != target.terminal_serial:
        raise ValueError("Telemetry belongs to a different terminal")
    if session.scalar(
        select(ReconciliationJob.id).where(
            ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status.not_in(["COMPLETED", "CANCELLED", "FAILED", "INVALIDATED"]),
        )
    ):
        raise ValueError("Initial reconciliation is still active")
    coverage = session.scalar(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.zkt_device_id == connector.zkt_device.id,
            ReconciliationCoverage.active.is_(True),
        )
    )
    if (
        coverage is None
        or coverage.terminal_serial != target.terminal_serial
        or coverage.capture_state
        not in {"SOURCE_CAPTURE_CERTIFIED", "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"}
        or diagnostics.get("source_generation") != coverage.terminal_generation
        or diagnostics.get("committed_source_cursor") != coverage.source_committed_cursor
        or terminal.get("attendance_count") != coverage.source_committed_cursor
    ):
        raise ValueError(
            "Certified source and committed device cursor must agree before observation"
        )
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is None or job.status != "COMPLETED" or not job.capture_certificate:
        raise ValueError("Completed initial reconciliation evidence is required")
    run = FirmwareHilRun(
        run_id=str(uuid4()),
        deployment_id=deployment.id,
        connector_id=connector.id,
        release_id=release.id,
        actor=actor,
        idempotency_key=idempotency_key,
        status="OBSERVING",
        target=target.model_dump(),
        release_identity=identity,
        started_at=now,
        ends_at=now + timedelta(minutes=15),
        baseline={
            "telemetry_id": telemetry.id,
            "boot_id": telemetry.boot_id,
            "coverage_id": coverage.coverage_id,
            "job_id": job.job_id,
            "source_generation": coverage.terminal_generation,
            "source_cursor": coverage.source_committed_cursor,
            "source_chain": coverage.source_committed_chain_digest,
            "diagnostics": diagnostics,
        },
        result={},
    )
    session.add(run)
    session.flush()
    return run


def cancel_run(session: Session, run_id: str, *, actor: str) -> FirmwareHilRun:
    run = session.scalar(
        select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id).with_for_update()
    )
    if run is None:
        raise ValueError("HIL observation was not found")
    if run.status != "OBSERVING":
        return run
    run.status = "CANCELLED"
    run.completed_at = utc_now()
    run.result = {"outcome": "INCOMPLETE", "reasons": ["CANCELLED"], "actor": actor}
    session.add(
        FirmwareEvent(
            deployment_id=run.deployment_id,
            state="HIL_INCOMPLETE",
            details={
                **run.release_identity,
                "target": run.target,
                "outcome": "INCOMPLETE",
                "run_id": run.run_id,
                "reason": "CANCELLED",
                "actor": actor,
            },
        )
    )
    return run


def serialize_run(run: FirmwareHilRun) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "target": run.target,
        "release_identity": run.release_identity,
        "baseline": run.baseline,
        "started_at": run.started_at,
        "ends_at": run.ends_at,
        "completed_at": run.completed_at,
        "result": run.result,
    }

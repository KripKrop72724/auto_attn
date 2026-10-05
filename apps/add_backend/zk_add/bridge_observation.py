"""Server-observed bridge readiness, distinct from writer HIL acceptance.

This only permits preparing the next bridge target. It never claims attendance
delivery, fault injection, physical qualification, or authority to enable a writer.
"""
from datetime import datetime, timedelta

from sqlalchemy import select

from zk_add.hil_scope import HilTarget, target_matches
from zk_add.models import Connector, DeviceTelemetry
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease
from zk_add.runtime_contract import RUNTIMES, journal_owner_status, worker_snapshot_fresh
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_bridge_contract import REPLACEMENT_BRIDGE_VERSION, validate_bridge_manifest

PROFILE = "BRIDGE_READINESS_V1"
FULL_PROFILE = "FULL_REMOTE_HIL_V1"
EVENTS = {"BRIDGE_READY", "BRIDGE_FAILED", "BRIDGE_INCOMPLETE"}
SAMPLE_LIMIT = 2048


def _integer(value):
    return type(value) is int and value >= 0


def sample_evidence(row, *, target, identity, boot_id):
    """Consume authenticated stored telemetry, never administrator-supplied facts."""
    errors = []
    payload = row.payload or {}
    diagnostics = payload.get("diagnostics") or {}
    recorded = ensure_utc(row.created_at)
    try:
        sampled = datetime.fromisoformat(payload["_trusted_envelope_sent_at"].replace("Z", "+00:00"))
        fresh = sampled.tzinfo is not None and 0 <= (recorded - sampled).total_seconds() <= 45
    except (KeyError, TypeError, ValueError):
        fresh = False
    tick = diagnostics.get("sampled_uptime_ms")
    if (not fresh or diagnostics.get("schema_version") != 2 or not _integer(tick)
            or not _integer(row.uptime_seconds) or not -5000 <= row.uptime_seconds * 1000 - tick <= 5000):
        errors.append("DIAGNOSTICS_FRESHNESS_UNPROVEN")
    if not row.boot_id or row.boot_id != boot_id or diagnostics.get("boot_id") != row.boot_id:
        errors.append("UNPLANNED_RESET_OR_BOOT_MISMATCH")
    ota = payload.get("ota") or {}
    if (ota.get("running_version") != identity["version"]
            or ota.get("image_sha256") != identity["application_sha256"]
            or ota.get("running_partition") not in {"ota_0", "ota_1"}
            or ota.get("secure_boot") is not True or ota.get("rollback_enabled") is not True):
        errors.append("SIGNED_APPLICATION_MISMATCH")
    runtime = diagnostics.get("journal_runtime") or {}
    if (diagnostics.get("runtime_profile") != "ZKT_LEGACY"
            or diagnostics.get("delivery_authority") != "LEGACY_DUAL"
            or runtime.get("observed") is not True or runtime.get("phase") != "READY"
            or runtime.get("reader_ready") is not True or runtime.get("compatibility") != ""
            or runtime.get("delivery_authority") != "LEGACY"
            or journal_owner_status(diagnostics, row.uptime_seconds) != "HEALTHY"):
        errors.append("COMPATIBLE_READER_NOT_READY")
    storage = diagnostics.get("storage") or {}
    if (storage.get("durability") != "HEALTHY" or storage.get("persistence_verified") is not True
            or storage.get("recovery_complete") is not True or storage.get("upgrade_ready") is not True
            or any(storage.get(key) for key in ("error_code", "upgrade_error", "persistence_probe_error",
                "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))):
        errors.append("LOCAL_PERSISTENCE_NOT_READY")
    contract = RUNTIMES["ZKT_LEGACY"]
    workers = diagnostics.get("workers") or []
    by_name = {worker.get("name"): worker for worker in workers}
    required = contract.workers | contract.auxiliary_workers
    if (set(by_name) != required or len(by_name) != len(workers)
            or any(worker.get("state") not in {"RUNNING", "WAITING_NETWORK"}
                   or not worker_snapshot_fresh(worker, row.uptime_seconds, contract) for worker in workers)):
        errors.append("WORKER_HEALTH_UNPROVEN")
    queues = diagnostics.get("queues") or []
    by_queue = {queue.get("name"): queue for queue in queues}
    if (len(by_queue) != len(queues) or not contract.queues <= by_queue.keys()
            or any(by_queue[name].get("count_known") is not True
                   or not _integer(by_queue[name].get("records")) for name in contract.queues & by_queue.keys())):
        errors.append("LEGACY_QUEUE_RECOVERY_UNPROVEN")
    terminal = payload.get("zkt") or {}
    if terminal.get("serial") != target.terminal_serial or terminal.get("online") is not True:
        errors.append("TERMINAL_CAPTURE_UNAVAILABLE")
    counters = {key: storage.get(key) for key in ("read_failures", "write_failures")}
    counters.update({"worker:" + str(name): worker.get("restart_count") for name, worker in by_name.items()})
    if any(not _integer(value) for value in counters.values()):
        errors.append("FAILURE_COUNTERS_UNPROVEN")
    source = {"generation": diagnostics.get("source_generation"),
              "cursor": diagnostics.get("committed_source_cursor"), "count": terminal.get("attendance_count")}
    if (not all(_integer(value) for value in source.values())
            or source["cursor"] > source["count"]):
        errors.append("SOURCE_PROGRESS_UNPROVEN")
    return {"telemetry_id": row.id, "recorded_at": recorded.isoformat(), "sequence": row.sequence,
            "uptime_seconds": row.uptime_seconds, "boot_id": row.boot_id,
            "source": source, "counters": counters, "errors": errors}


def require_bridge_baseline(release, row, target, identity):
    if release.version != REPLACEMENT_BRIDGE_VERSION:
        raise ValueError("Bridge readiness requires the replacement compatibility bridge")
    validate_bridge_manifest(release.manifest)
    evidence = sample_evidence(row, target=target, identity=identity, boot_id=row.boot_id)
    if evidence["errors"]:
        raise ValueError("Bridge readiness evidence: " + ", ".join(evidence["errors"]))


def complete_bridge_run(session, run_id, *, actor):
    from zk_add.hil_runs import _release_identity

    if not actor or len(actor) > 120:
        raise ValueError("A bounded administrator identity is required")
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id).with_for_update())
    if run is None or run.baseline.get("profile") != PROFILE:
        raise ValueError("Bridge readiness observation was not found")
    if run.status != "OBSERVING":
        return run
    now = utc_now()
    start, end = ensure_utc(run.started_at), ensure_utc(run.ends_at)
    if now < end or (end - start).total_seconds() < 900:
        raise ValueError("Bridge readiness requires the complete 15-minute observation")
    release = session.get(FirmwareRelease, run.release_id)
    deployment = session.get(FirmwareDeployment, run.deployment_id)
    connector = session.get(Connector, run.connector_id)
    target = HilTarget.model_validate(run.target)
    errors = []
    if (release is None or release.state != "HIL_ONLY" or release.revoked_at is not None
            or release.version != REPLACEMENT_BRIDGE_VERSION
            or _release_identity(release).model_dump(mode="json") != run.release_identity):
        errors.append("RELEASE_CHANGED")
    campaign = session.get(FirmwareCampaign, deployment.campaign_id) if deployment else None
    latest_deployment = session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.connector_id == run.connector_id).order_by(FirmwareDeployment.id.desc()).limit(1))
    if (deployment is None or deployment.status != "SUCCEEDED" or latest_deployment != run.deployment_id
            or campaign is None or campaign.status not in {"ACTIVE", "COMPLETED"}):
        errors.append("DEPLOYMENT_OR_CAMPAIGN_CHANGED")
    if (connector is None or not target_matches(target, connector) or not connector.connected
            or not connector.zkt_device.online or connector.boot_id != run.baseline["boot_id"]):
        errors.append("CURRENT_TARGET_NOT_READY")
    rows = list(session.scalars(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == run.connector_id,
        DeviceTelemetry.created_at >= start, DeviceTelemetry.created_at <= end,
    ).order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(SAMPLE_LIMIT + 1)))
    if len(rows) > SAMPLE_LIMIT:
        errors.append("OBSERVATION_SAMPLE_LIMIT")
        rows = rows[:SAMPLE_LIMIT]
    samples = [sample_evidence(row, target=target, identity=run.release_identity,
                              boot_id=run.baseline["boot_id"]) for row in rows]
    baseline = session.get(DeviceTelemetry, run.baseline["telemetry_id"])
    if baseline is None or baseline.connector_id != run.connector_id:
        errors.append("BASELINE_MISSING")
    else:
        initial = sample_evidence(baseline, target=target, identity=run.release_identity,
                                  boot_id=run.baseline["boot_id"])
        if not samples or samples[0]["telemetry_id"] != initial["telemetry_id"]:
            samples.insert(0, initial)
    if (not samples or datetime.fromisoformat(samples[0]["recorded_at"]) > start + timedelta(seconds=45)
            or datetime.fromisoformat(samples[-1]["recorded_at"]) < end - timedelta(seconds=45)):
        errors.append("OBSERVATION_TELEMETRY_INCOMPLETE")
    for sample in samples:
        errors.extend(sample["errors"])
    for before, after in zip(samples, samples[1:]):
        gap = datetime.fromisoformat(after["recorded_at"]) - datetime.fromisoformat(before["recorded_at"])
        if not timedelta(0) < gap <= timedelta(seconds=45):
            errors.append("TELEMETRY_GAP_OR_REPLAY")
        if (not _integer(before["sequence"]) or not _integer(after["sequence"])
                or after["sequence"] <= before["sequence"]
                or not _integer(before["uptime_seconds"]) or not _integer(after["uptime_seconds"])
                or after["uptime_seconds"] <= before["uptime_seconds"]):
            errors.append("BOOT_PROGRESS_OR_SEQUENCE_REGRESSED")
        if before["counters"] != after["counters"]:
            errors.append("FAILURE_COUNTER_CHANGED")
        if before["source"]["generation"] != after["source"]["generation"]:
            errors.append("SOURCE_GENERATION_CHANGED")
        if (all(_integer(sample["source"]["cursor"]) for sample in (before, after))
                and before["source"]["cursor"] > after["source"]["cursor"]):
            errors.append("SOURCE_CURSOR_REGRESSED")
    if samples and samples[-1]["source"]["cursor"] != samples[-1]["source"]["count"]:
        errors.append("SOURCE_TAIL_PENDING")
    latest = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == run.connector_id)
                            .order_by(DeviceTelemetry.id.desc()).limit(1))
    if latest is None or not 0 <= (now - ensure_utc(latest.created_at)).total_seconds() <= 45:
        errors.append("CURRENT_TELEMETRY_STALE")
    else:
        current = sample_evidence(latest, target=target, identity=run.release_identity,
                                  boot_id=run.baseline["boot_id"])
        errors.extend(current["errors"])
        if samples and (current["counters"] != samples[-1]["counters"]
                        or current["source"]["generation"] != samples[-1]["source"]["generation"]):
            errors.append("CURRENT_HEALTH_CHANGED")
    run.status = "BRIDGE_INCOMPLETE" if errors else "BRIDGE_READY"
    run.completed_at = now
    run.result = {"profile": PROFILE, "outcome": "INCOMPLETE" if errors else "READY",
                  "reasons": sorted(set(errors)), "samples": samples, "actor": actor,
                  "writer_hil": "NOT_ASSERTED", "oracle_delivery": "NOT_ASSERTED",
                  "fault_injection": "NOT_PERFORMED", "physical_qualification": "NOT_PERFORMED"}
    session.add(FirmwareEvent(deployment_id=run.deployment_id, state=run.status,
        details={**run.release_identity, "target": run.target, "run_id": run.run_id,
                 "profile": PROFILE, "outcome": run.result["outcome"], "reasons": run.result["reasons"]}))
    session.flush()
    return run


def ready_event_matches(session, event, deployment, release):
    """Scope progression needs the stored server verdict, not an event label."""
    details = event.details or {}
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == details.get("run_id")))
    return bool(event.state == "BRIDGE_READY" and details.get("profile") == PROFILE
        and details.get("outcome") == "READY" and run is not None and run.status == "BRIDGE_READY"
        and run.deployment_id == deployment.id and run.connector_id == deployment.connector_id
        and run.release_id == release.id and run.target == details.get("target")
        and run.baseline.get("profile") == PROFILE and run.result.get("outcome") == "READY"
        and run.result.get("reasons") == [] and run.completed_at is not None
        and ensure_utc(run.completed_at) >= ensure_utc(run.ends_at)
        and (ensure_utc(run.ends_at) - ensure_utc(run.started_at)).total_seconds() >= 900
        and all(run.release_identity.get(key) == details.get(key) for key in
                ("version", "git_sha", "artifact_sha256", "application_sha256", "signing_key_id")))

"""Interrupted OTA attempts: bounded download resumption and interrupted target boots.

Nothing here reboots, re-installs or starts an attempt on its own:

* A 2.6.0-2.6.15 download that stopped with DOWNLOAD_INCOMPLETE or
  DOWNLOAD_BEGIN_FAILED keeps its durable DOWNLOADING journal and resumes the
  same deployment from its checkpoint when it is offered the assignment again.
  ADD withholds that assignment (204) for a backoff, at most three download
  attempts in total, then fails the deployment with DOWNLOAD_RETRIES_EXHAUSTED.
  The signed image is still verified end to end (signature at finish, then
  the partition digest) before it can be selected for boot.
* A target image that ADD saw boot, that sent no failure report and that the
  ESP left within five minutes is labelled TARGET_BOOT_INTERRUPTED with
  evidence. Only the owner's step-up "Retry same target" makes a new attempt.

2.4.12 and 2.5.2 keep a different sticky-error behaviour, and the 2.6.16+
journal bridges, the 2.6.24+ recovery images and every 2.7.x image keep the
existing fail-closed path unchanged.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.models import Connector, DeviceLog, DeviceTelemetry
from zk_add.ota import (
    ACTIVE_DEPLOYMENT_STATES,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareDownloadGrant,
    FirmwareEvent,
    FirmwareRelease,
    _versions_match,
    semantic_version,
)
from zk_add.esp_stability import boot_history, peer_resets, stability_warnings
from zk_add.storage_recovery import RELEASE_IDS as RECOVERY_RELEASE_IDS
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now

RESUMABLE_DOWNLOAD_ERRORS = frozenset({"DOWNLOAD_INCOMPLETE", "DOWNLOAD_BEGIN_FAILED"})
# Verified identical download, journal and sticky-error code in 2.6.0-2.6.15
# (ota_manager.c, a88998346d5b): the journal stays DOWNLOADING after either
# error, last_error is never cleared until reboot, the heartbeat reports
# UPDATING while a fetched assignment runs, and a re-fetched assignment for
# the same deployment and digest resumes from the journal's checkpoint.
RESUMABLE_RUNNING_VERSIONS = ((2, 6, 0), (2, 6, 15))
DOWNLOAD_ATTEMPT_LIMIT = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = (120, 600)
DOWNLOAD_INTERRUPTED = "DOWNLOAD_INTERRUPTED"
DOWNLOAD_RETRY_OFFERED = "DOWNLOAD_RETRY_OFFERED"
DOWNLOAD_RETRIES_EXHAUSTED = "DOWNLOAD_RETRIES_EXHAUSTED"

TARGET_BOOT_INTERRUPTED = "TARGET_BOOT_INTERRUPTED"
UNCLASSIFIED = "UNCLASSIFIED"
TARGET_LIFETIME_LIMIT_SECONDS = 300
TERMINAL_TIME_SYNC_SECONDS = 30
RETRY_REQUESTED = "RETRY_REQUESTED"
# Interrupted boots of one release on one ESP before the owner's retry is
# refused here: after two, capture the ESP's UART at the site first.
TARGET_BOOT_RETRY_LIMIT = 2
# Journal bridges, the 2.7.0 writer and the one-shot recovery images carry
# their own return evidence and HIL acceptance flows.
_OWN_RETURN_EVIDENCE = frozenset({"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22",
                                  "2.6.23", "2.7.0", *RECOVERY_RELEASE_IDS})
_RECONCILE_ACTIVITIES = frozenset({"FULL_RECONCILE", "HISTORICAL_RECONCILE", "LIGHT_RECONCILE"})


def _iso(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value is not None else None


def _resumable_running_version(value: str | None) -> bool:
    version = semantic_version(value)
    return version is not None and RESUMABLE_RUNNING_VERSIONS[0] <= version <= RESUMABLE_RUNNING_VERSIONS[1]


def _interruptions(session: Session, deployment: FirmwareDeployment) -> list[FirmwareEvent]:
    return list(session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED,
    ).order_by(FirmwareEvent.id)))


def download_interruption(session: Session, *, connector: Connector, deployment: FirmwareDeployment,
                          release: FirmwareRelease, payload: Any, error_code: str, runtime_state: str,
                          now: datetime | None = None) -> dict | None:
    """Decide a sticky resumable download error from a 2.6.x heartbeat.

    None means the heartbeat is outside this path and the caller fails closed
    as before. WAIT keeps the deployment active because the error belongs to
    an attempt already counted, or no heartbeat after the latest offer has
    proved that attempt ended. RETRY_SCHEDULED records an interruption and a
    backoff. EXHAUSTED records the last interruption and carries the message
    the caller fails the deployment with.

    The ESP polls the assignment every 60 s and reports UPDATING while a
    fetched attempt runs, so an attempt is counted only once a heartbeat that
    arrived after the attempt's grant precedes this non-UPDATING one: a
    heartbeat built just before the fetch cannot end an attempt.
    """
    if (connector.firmware_family != "zkt" or runtime_state != "DOWNLOADING"
            or error_code not in RESUMABLE_DOWNLOAD_ERRORS
            or deployment.status not in {"OFFERED", "DOWNLOADING"}
            or not _resumable_running_version(payload.firmware_version)
            or not _resumable_running_version(deployment.previous_version)
            or not _versions_match(payload.ota.target_version, deployment.target_version)):
        return None
    now = now or utc_now()
    interruptions = _interruptions(session, deployment)
    statement = select(func.max(FirmwareDownloadGrant.created_at)).where(
        FirmwareDownloadGrant.deployment_id == deployment.id)
    if interruptions:
        statement = statement.where(FirmwareDownloadGrant.created_at > interruptions[-1].created_at)
    offered_at = session.scalar(statement)
    if offered_at is None:
        return {"decision": "WAIT"}
    previous = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == connector.id)
                              .order_by(DeviceTelemetry.id.desc()).limit(1))
    if (previous is None or previous.boot_id != connector.boot_id
            or ensure_utc(previous.created_at) <= ensure_utc(offered_at)):
        return {"decision": "WAIT"}
    number = len(interruptions) + 1
    bytes_written = min(release.image_size, max(deployment.bytes_written, payload.ota.bytes_written))
    exhausted = number >= DOWNLOAD_ATTEMPT_LIMIT
    retry_after = None if exhausted else now + timedelta(seconds=DOWNLOAD_RETRY_BACKOFF_SECONDS[number - 1])
    session.add(FirmwareEvent(deployment_id=deployment.id, state=DOWNLOAD_INTERRUPTED, created_at=now, details={
        "interruption": number, "attempt_limit": DOWNLOAD_ATTEMPT_LIMIT, "error_code": error_code,
        "bytes_written": bytes_written, "image_size": release.image_size,
        "running_version": payload.firmware_version, "boot_id": connector.boot_id,
        "attempt_offered_at": _iso(offered_at), "retry_after": _iso(retry_after),
    }))
    if exhausted:
        return {"decision": "EXHAUSTED", "message": (
            f"The resumable download stopped {number} times; the last stop was {error_code} at "
            f"{bytes_written} of {release.image_size} bytes. The campaign is paused; check the site's "
            "network path to ADD before a new attempt.")}
    # The device's checkpoint, not this heartbeat's in-RAM byte count, is
    # where it resumes; bytes_written stays the last reported progress.
    deployment.error_code = "DOWNLOAD_INTERRUPTED"
    deployment.error_message = (
        f"The resumable download stopped ({error_code}) at {bytes_written} of {release.image_size} bytes. "
        f"ADD re-offers the same signed image after {retry_after.strftime('%H:%M:%S')} UTC; this was attempt "
        f"{number} of {DOWNLOAD_ATTEMPT_LIMIT}.")
    deployment.updated_at = now
    return {"decision": "RETRY_SCHEDULED", "retry_after": retry_after.isoformat(), "interruption": number}


def download_retry_hold(session: Session, deployment: FirmwareDeployment, *, now: datetime | None = None) -> bool:
    """Withhold an active deployment's assignment during its retry backoff.

    A deployment with no recorded interruption is untouched. The first offer
    after each backoff is recorded once and counts as a new attempt.
    """
    latest = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED,
    ).order_by(FirmwareEvent.id.desc()).limit(1))
    if latest is None:
        return False
    details = latest.details or {}
    now = now or utc_now()
    try:
        retry_after = parse_datetime(details["retry_after"])
    except (KeyError, TypeError, ValueError):
        return True
    if now < retry_after:
        return True
    offered = session.scalar(select(FirmwareEvent.id).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_RETRY_OFFERED,
        FirmwareEvent.id > latest.id).limit(1))
    if offered is None:
        deployment.attempt_count += 1
        session.add(FirmwareEvent(deployment_id=deployment.id, state=DOWNLOAD_RETRY_OFFERED, details={
            "after_interruption": details.get("interruption"), "attempt": int(details.get("interruption") or 0) + 1,
            "attempt_limit": DOWNLOAD_ATTEMPT_LIMIT}))
        session.flush()  # sessions run without autoflush; record the attempt once
    return False


def _boot_events(events: list[FirmwareEvent]) -> list[FirmwareEvent]:
    return [event for event in events if event.state in {"BOOTED_PENDING", "RECONCILING"}]


def rollback_evidence(session: Session, *, connector: Connector, deployment: FirmwareDeployment,
                      return_evidence: dict, now: datetime | None = None) -> dict:
    """Evidence around a target boot that ended in a return to the previous image (A2).

    Every field is an observation or a bounded estimate; none names a cause.
    """
    now = now or utc_now()
    observed_at = parse_datetime(str(return_evidence["observed_at"]))
    returned_at = observed_at - timedelta(seconds=int(return_evidence["uptime_seconds"]))
    return_boot = return_evidence.get("boot_id")
    events = list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id)
                                  .order_by(FirmwareEvent.id)))
    booted = _boot_events(events)
    first, last = (booted[0], booted[-1]) if booted else (None, None)
    ready = ([event for event in events if event.state == "READY_TO_BOOT" and first is not None
              and event.id < first.id] or [None])[-1]
    anchor_until = ensure_utc(first.created_at) if first is not None else returned_at
    pre_install = None
    for created_at, boot_id, activity, payload in session.execute(
            select(DeviceTelemetry.created_at, DeviceTelemetry.boot_id, DeviceTelemetry.current_activity,
                   DeviceTelemetry.payload)
            .where(DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.created_at < anchor_until,
                   DeviceTelemetry.created_at >= anchor_until - timedelta(hours=1))
            .order_by(DeviceTelemetry.created_at.desc(), DeviceTelemetry.id.desc()).limit(40)):
        version = (payload or {}).get("firmware_version") if isinstance(payload, dict) else None
        if boot_id != return_boot and _versions_match(version, deployment.previous_version):
            pre_install = {"at": ensure_utc(created_at), "boot_id": boot_id, "activity": activity}
            break
    anchors = [value for value in (ensure_utc(ready.created_at) if ready is not None else None,
                                   pre_install["at"] if pre_install else None) if value is not None]
    started_after = max(anchors) if anchors else None
    lifetime_min = (returned_at - ensure_utc(first.created_at)).total_seconds() if first is not None else None
    lifetime_max = (returned_at - started_after).total_seconds() if started_after is not None else None
    failures = [{"state": event.state, "error_code": (event.details or {}).get("error_code"),
                 "at": _iso(event.created_at)}
                for event in events if first is not None and event.id > first.id
                and event.state in {"FAILED", "ROLLED_BACK"} and "return_evidence" not in (event.details or {})]
    window_start = started_after or (ensure_utc(first.created_at) - timedelta(minutes=2) if first else returned_at)
    window_end = returned_at + timedelta(seconds=5)
    excluded = {return_boot, pre_install["boot_id"] if pre_install else None}
    logs = [row for row in session.execute(
        select(DeviceLog.boot_id, DeviceLog.code, DeviceLog.subsystem, DeviceLog.received_at)
        .where(DeviceLog.connector_id == connector.id, DeviceLog.received_at >= window_start,
               DeviceLog.received_at <= window_end).order_by(DeviceLog.received_at, DeviceLog.id).limit(500))
        if row.boot_id not in excluded]
    samples = [row for row in session.execute(
        select(DeviceTelemetry.boot_id, DeviceTelemetry.current_activity, DeviceTelemetry.created_at)
        .where(DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.created_at >= window_start,
               DeviceTelemetry.created_at <= window_end)
        .order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(500))
        if row.boot_id not in excluded]
    reconcile_logs = [row for row in logs if row.subsystem == "reconcile"
                      or (row.code or "").startswith(("FULL_RECONCILE", "TRUTH_"))]
    last_sample = samples[-1] if samples else None
    sync = session.execute(select(DeviceLog.device_time, DeviceLog.received_at).where(
        DeviceLog.connector_id == connector.id, DeviceLog.boot_id == return_boot,
        DeviceLog.code == "ZKT_TIME_SYNC_OK").order_by(DeviceLog.received_at, DeviceLog.id).limit(1)).first()
    sync_at = ensure_utc(sync.device_time or sync.received_at) if sync is not None else None
    sync_after = round((sync_at - returned_at).total_seconds()) if sync_at is not None else None
    return {
        "schema_version": 1,
        "observation": "PREVIOUS_FIRMWARE_OBSERVED",
        "reset_cause": "not_reported",
        "target_version": deployment.target_version,
        "previous_version": deployment.previous_version,
        "target_boot": {
            "ready_to_boot_at": _iso(ready.created_at) if ready is not None else None,
            "last_previous_firmware_heartbeat_at": _iso(pre_install["at"]) if pre_install else None,
            "first_progress_at": _iso(first.created_at) if first is not None else None,
            "last_progress_at": _iso(last.created_at) if last is not None else None,
            "last_progress_state": last.state if last is not None else None,
            "last_progress_code": (last.details or {}).get("error_code") if last is not None else None,
            "progress_reports": len(booted),
            "estimated_lifetime_seconds": {
                "min": round(lifetime_min) if lifetime_min is not None and lifetime_min >= 0 else None,
                "max": round(lifetime_max) if lifetime_max is not None and lifetime_max >= 0 else None,
            },
            "failure_reports": failures,
            "logs_received": len(logs),
            "telemetry_received": len(samples),
            "boot_ids": sorted({row.boot_id for row in [*logs, *samples] if row.boot_id}),
        },
        "return_boot": {"boot_id": return_boot, "estimated_started_at": returned_at.isoformat(),
                        "uptime_seconds_at_observation": int(return_evidence["uptime_seconds"]),
                        "observed_at": observed_at.isoformat()},
        "previous_firmware_resets": boot_history(
            session, connector, until=started_after or returned_at, firmware_version=deployment.previous_version),
        "terminal": {
            "time_sync_after_new_boot_seconds": sync_after,
            "answered_within_30_seconds": (None if sync_after is None
                                           else sync_after <= TERMINAL_TIME_SYNC_SECONDS),
            "source": None if sync is None else ("device_time" if sync.device_time else "received_at"),
        },
        "location_resets": peer_resets(session, connector, at=returned_at, now=now),
        "reconcile_at_reset": {
            "running": (None if last_sample is None
                        else (last_sample.current_activity or "") in _RECONCILE_ACTIVITIES),
            "last_target_activity": last_sample.current_activity if last_sample is not None else None,
            "last_target_activity_at": _iso(last_sample.created_at) if last_sample is not None else None,
            "last_reconcile_log": ({"code": reconcile_logs[-1].code, "at": _iso(reconcile_logs[-1].received_at)}
                                   if reconcile_logs else None),
            "previous_firmware_activity_before_install": pre_install["activity"] if pre_install else None,
        },
        "evaluated_at": now.isoformat(),
    }


def classify_return(deployment: FirmwareDeployment, release: FirmwareRelease | None,
                    evidence: dict) -> tuple[str, list[str]]:
    """TARGET_BOOT_INTERRUPTED only when every condition holds; otherwise the reasons."""
    target = evidence["target_boot"]
    reasons = []
    if release is None or release.version in _OWN_RETURN_EVIDENCE:
        reasons.append("TARGET_HAS_OWN_RETURN_EVIDENCE")
    if deployment.status != "BOOTED_PENDING":
        reasons.append("TARGET_REPORTED_CONFIRMATION")
    if target["last_progress_code"] == "RUNTIME_HEALTHY":
        reasons.append("TARGET_REPORTED_RUNTIME_HEALTHY")
    if target["failure_reports"]:
        reasons.append("TARGET_REPORTED_FAILURE")
    lifetime = target["estimated_lifetime_seconds"]["max"]
    if lifetime is None:
        reasons.append("TARGET_BOOT_START_UNBOUNDED")
    elif lifetime >= TARGET_LIFETIME_LIMIT_SECONDS:
        reasons.append("TARGET_LIFETIME_NOT_SHORT")
    return (UNCLASSIFIED, reasons) if reasons else (TARGET_BOOT_INTERRUPTED, reasons)


def evidence_summary(evidence: dict) -> str:
    target = evidence["target_boot"]
    lifetime = target["estimated_lifetime_seconds"]
    resets = evidence["previous_firmware_resets"]
    terminal = evidence["terminal"]
    location = evidence["location_resets"]
    reconcile = evidence["reconcile_at_reset"]["running"]
    span = (f"{lifetime['min']}-{lifetime['max']} s" if lifetime["min"] is not None and lifetime["max"] is not None
            else f"at most {lifetime['max']} s" if lifetime["max"] is not None else "an unbounded time")
    sync = terminal["time_sync_after_new_boot_seconds"]
    return (
        f"{evidence['target_version']} ran {span} before the ESP returned to {evidence['previous_version']}; "
        f"{len(target['failure_reports'])} failure reports, {target['logs_received']} target-boot logs; "
        f"{resets['resets']} resets in {resets['online_hours']} online hours on {evidence['previous_version']} "
        f"({resets['reset_rate_per_online_hour']}/h); terminal "
        + (f"answered a time sync {sync} s after the new boot" if sync is not None else "time sync not observed")
        + f"; {len(location['resets'])} other ESP resets at {location['location'] or 'this location'} within "
        f"+-{location['window_seconds']} s; reconcile at reset "
        + ("unknown" if reconcile is None else "yes" if reconcile else "no") + "."
    )[:300]


def diagnose_return(session: Session, *, connector: Connector, deployment: FirmwareDeployment,
                    return_evidence: dict, now: datetime | None = None) -> dict:
    """Classification and evidence; call before the deployment leaves BOOTED_PENDING."""
    release = session.get(FirmwareRelease, deployment.release_id)
    evidence = rollback_evidence(session, connector=connector, deployment=deployment,
                                 return_evidence=return_evidence, now=now)
    classification, reasons = classify_return(deployment, release, evidence)
    return {**evidence, "classification": classification, "unclassified_reasons": reasons,
            "summary": evidence_summary(evidence)}


def interrupted_message(diagnosis: dict) -> str:
    lifetime = diagnosis["target_boot"]["estimated_lifetime_seconds"]
    return (
        f"The ESP reset about {lifetime['min'] if lifetime['min'] is not None else '?'}-{lifetime['max']} s after "
        f"booting {diagnosis['target_version']} and before the image was confirmed, so the bootloader returned "
        f"it to {diagnosis['previous_version']}. The new image sent no failure report: ADD cannot tell a crash "
        "from a power or reset event, so read the rollback evidence. The campaign is paused; the owner may "
        "retry the same target with password step-up after checking the ESP's power and reset history."
    )


def stored_diagnosis(session: Session, deployment: FirmwareDeployment) -> FirmwareEvent | None:
    return session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "ROLLED_BACK",
    ).order_by(FirmwareEvent.id.desc()).limit(1))


def refreshed_diagnosis(session: Session, deployment: FirmwareDeployment, *, now: datetime | None = None) -> dict | None:
    """The stored classification with evidence re-read now (late logs and peer heartbeats)."""
    event = stored_diagnosis(session, deployment)
    details = (event.details or {}) if event is not None else {}
    stored, return_evidence = details.get("diagnosis"), details.get("return_evidence")
    connector = session.get(Connector, deployment.connector_id)
    if not isinstance(stored, dict) or not isinstance(return_evidence, dict) or connector is None:
        return None
    try:
        evidence = rollback_evidence(session, connector=connector, deployment=deployment,
                                     return_evidence=return_evidence, now=now)
    except (KeyError, TypeError, ValueError):
        return stored
    return {**evidence, "classification": stored.get("classification"),
            "unclassified_reasons": stored.get("unclassified_reasons", []),
            "classified_at": _iso(event.created_at), "summary": evidence_summary(evidence)}


def _interrupted_boots(session: Session, deployment: FirmwareDeployment) -> int:
    return int(session.scalar(select(func.count(FirmwareDeployment.id)).where(
        FirmwareDeployment.connector_id == deployment.connector_id,
        FirmwareDeployment.release_id == deployment.release_id,
        FirmwareDeployment.status == "ROLLED_BACK",
        FirmwareDeployment.error_code == TARGET_BOOT_INTERRUPTED)) or 0)


def retry_refusal(session: Session, campaign: FirmwareCampaign, deployment: FirmwareDeployment) -> str | None:
    """Why the owner's retry is refused now, or None. The action re-checks everything."""
    from zk_add.zkt_hil_schedule import applies as nationwide_schedule_applies

    if deployment.status != "ROLLED_BACK" or deployment.error_code != TARGET_BOOT_INTERRUPTED:
        return "Only a target boot that ADD classified TARGET_BOOT_INTERRUPTED can be retried."
    if campaign.status != "PAUSED":
        return ("A cancelled campaign cannot retry; start a new campaign." if campaign.status == "CANCELLED"
                else "The campaign must be paused to retry a target.")
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or release.state not in {"AVAILABLE", "HIL_ONLY"}:
        return "The signed release is no longer available."
    if release.version in _OWN_RETURN_EVIDENCE or nationwide_schedule_applies(release):
        return "This release has its own return evidence and HIL schedule; start a new campaign."
    later = session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.campaign_id == campaign.id, FirmwareDeployment.connector_id == deployment.connector_id,
        FirmwareDeployment.id > deployment.id).limit(1))
    if later is not None:
        return "A later attempt for this ESP already exists in the campaign."
    busy = session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.campaign_id == campaign.id,
        FirmwareDeployment.status.in_(["PENDING", *ACTIVE_DEPLOYMENT_STATES])).limit(1))
    if busy is not None:
        return "Another deployment in this campaign is still pending or active."
    if _interrupted_boots(session, deployment) >= TARGET_BOOT_RETRY_LIMIT:
        return (f"{TARGET_BOOT_RETRY_LIMIT} boots of {release.version} on this ESP were interrupted. Capture the "
                "ESP's UART at the site before another attempt.")
    connector = session.get(Connector, deployment.connector_id)
    if connector is None or not connector.active or connector.zone_id != campaign.zone_id:
        return "The ESP is no longer active in this campaign's zone."
    if not connector.connected:
        return "The ESP is offline."
    if not _versions_match(connector.firmware_version, deployment.previous_version):
        return f"The ESP no longer runs {deployment.previous_version}."
    if connector.ota_running_partition not in {"ota_0", "ota_1"}:
        return "The ESP is not running from an OTA application slot."
    return None


def retry_view(session: Session, campaign: FirmwareCampaign, deployment: FirmwareDeployment) -> dict | None:
    """A UI hint for an interrupted deployment; never authority."""
    if deployment.status != "ROLLED_BACK" or deployment.error_code != TARGET_BOOT_INTERRUPTED:
        return None
    refusal = retry_refusal(session, campaign, deployment)
    connector = session.get(Connector, deployment.connector_id)
    warnings = stability_warnings(session, connector) if refusal is None and connector is not None else []
    return {"available": refusal is None, "refusal": refusal,
            "interrupted_boots": _interrupted_boots(session, deployment),
            "limit": TARGET_BOOT_RETRY_LIMIT, "stability_warnings": warnings}


def retry_interrupted_target(session: Session, *, campaign_public_id: str, deployment_public_id: str,
                             actor: str, reason: str, typed_confirmation: str,
                             override_stability_warnings: bool, now: datetime | None = None) -> dict:
    """The owner's step-up retry: one new PENDING attempt in the same paused campaign.

    The caller has already verified the owner's password. The new attempt is
    then offered only through every ordinary admission check.
    """
    now = now or utc_now()
    campaign_row = session.scalar(select(FirmwareCampaign.id).where(
        FirmwareCampaign.campaign_id == campaign_public_id))
    if campaign_row is None:
        raise LookupError("Firmware campaign not found.")
    # Deployment before campaign, the order record_progress locks them in.
    deployment = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.deployment_id == deployment_public_id,
        FirmwareDeployment.campaign_id == campaign_row).with_for_update()
        .execution_options(populate_existing=True))
    if deployment is None:
        raise LookupError("Firmware deployment not found in this campaign.")
    campaign = session.scalar(select(FirmwareCampaign).where(FirmwareCampaign.id == campaign_row)
                              .with_for_update().execution_options(populate_existing=True))
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or typed_confirmation != release.version:
        raise ValueError("Typed firmware version does not match the release.")
    refusal = retry_refusal(session, campaign, deployment)
    if refusal:
        raise ValueError(refusal)
    connector = session.get(Connector, deployment.connector_id)
    warnings = stability_warnings(session, connector, now=now)
    if warnings and not override_stability_warnings:
        raise ValueError("ESP stability warnings need the owner's override: "
                         + " ".join(warning["message"] for warning in warnings))
    retry = FirmwareDeployment(
        deployment_id=secrets.token_hex(16), campaign_id=campaign.id, release_id=release.id,
        connector_id=connector.id, previous_version=connector.firmware_version, target_version=release.version)
    session.add(retry)
    session.flush()
    overridden = [{"code": warning["code"], "message": warning["message"]} for warning in warnings]
    session.add(FirmwareEvent(deployment_id=retry.id, state=RETRY_REQUESTED, details={
        "retry_of": deployment.deployment_id, "classification": TARGET_BOOT_INTERRUPTED, "actor": actor,
        "reason": reason, "interrupted_boots": _interrupted_boots(session, deployment),
        "stability_warnings_overridden": overridden}))
    campaign.status = "ACTIVE"
    campaign.pause_reason = None
    campaign.updated_at = now
    return {"campaign_id": campaign.campaign_id, "status": campaign.status,
            "deployment_id": retry.deployment_id, "retry_of": deployment.deployment_id,
            "connector_id": connector.connector_id, "stability_warnings_overridden": overridden}


def rollback_slot_residue_record(session: Session, connector: Connector) -> dict | None:
    """ADD's own record that a 2.7.x download wrote the other slot after the last 2.6.15 install.

    The latest successful deployment on this ESP must be the 2.6.15 install,
    and a later 2.7.x deployment must have reported written bytes.
    """
    accepted = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.connector_id == connector.id, FirmwareDeployment.status == "SUCCEEDED",
        FirmwareDeployment.completed_at.is_not(None),
    ).order_by(FirmwareDeployment.completed_at.desc(), FirmwareDeployment.id.desc()).limit(1))
    if accepted is None or not _versions_match(accepted.target_version, "2.6.15"):
        return None
    for row in session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.connector_id == connector.id, FirmwareDeployment.id > accepted.id,
            FirmwareDeployment.created_at > accepted.completed_at, FirmwareDeployment.bytes_written > 0,
    ).order_by(FirmwareDeployment.id.desc()).limit(20)):
        version = semantic_version(row.target_version)
        if version is not None and version[:2] == (2, 7):
            return {"accepted_deployment_id": accepted.deployment_id,
                    "accepted_at": _iso(accepted.completed_at),
                    "deployment_id": row.deployment_id, "target_version": row.target_version,
                    "status": row.status, "bytes_written": row.bytes_written}
    return None

"""Interrupted OTA attempts: bounded download resumption and interrupted target boots.

Nothing here reboots, re-installs or starts an attempt on its own:

* Only with settings.firmware_download_resume_enabled (off by default), a
  2.6.0-2.6.15 download that stopped with DOWNLOAD_INCOMPLETE or
  DOWNLOAD_BEGIN_FAILED keeps its DOWNLOADING journal. Offered the same
  deployment and digest again, the ESP resumes the write: without a reboot
  from its in-RAM byte count, after one from the journal's last 64 KiB
  checkpoint. ADD withholds that assignment (204) for a backoff, at most three
  download attempts within an hour of the first stop, and only while the
  campaign stays ACTIVE and the release offerable. Every grant after the first
  stop counts as an attempt, so the limit holds even when no heartbeat counted
  a stop before the ESP asked again. A third stop, a re-offer
  not taken within 15 minutes of its backoff, the closed window, an admission
  refusal, a pause, a cancel, a revoke or the switch turned off fails the
  deployment closed as before; the maintenance sweep does so for an ESP that
  stays offline. The signed image is still verified end to end (signature at
  finish, then the partition digest) before boot selection. The resume has
  not yet run on a device: ADD failed every such stop before.
* A 2.6.0-2.6.15 ESP that left a target image ADD saw boot, with no failure
  report and within five minutes, is labelled TARGET_BOOT_INTERRUPTED with
  evidence, whether ADD saw the return in two heartbeats or the ESP reported
  ROLLED_BACK BOOTLOADER_ROLLBACK first. Only the owner's step-up "Retry same
  target" makes a new attempt.

Every other running version (2.4.12, 2.5.2, 2.6.16+ and every 2.7.x) and every
target with its own return evidence (the journal bridges, 2.7.0, the 2.6.24+
recovery images and 2.7.6) keep the existing fail-closed path unchanged.

Lock order, everywhere: connector, then deployment, then campaign (the
WebSocket heartbeat locks its connector first, record_progress and the HTTPS
progress report lock it before the deployment). The assignment poll never
waits for a row lock: it answers 204 instead.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.exc import OperationalError
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
from zk_add.esp_stability import boot_history, peer_resets, stability_warnings, warnings_digest
from zk_add.settings import settings
from zk_add.storage_recovery import RELEASE_IDS as RECOVERY_RELEASE_IDS
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now

RESUMABLE_DOWNLOAD_ERRORS = frozenset({"DOWNLOAD_INCOMPLETE", "DOWNLOAD_BEGIN_FAILED"})
# ota_manager.c is identical in 2.6.0 (3f6504f) and 2.6.15 (a88998346d5b) for
# everything relied on here: the journal stays DOWNLOADING after either error,
# last_error is cleared only by a reboot, the heartbeat reports UPDATING while
# a fetched attempt runs, a re-fetched assignment for the same deployment and
# digest resumes at the journal's byte count, and a return from an unconfirmed
# image reports ROLLED_BACK BOOTLOADER_ROLLBACK every 60 s until ADD answers 2xx.
RESUMABLE_RUNNING_VERSIONS = ((2, 6, 0), (2, 6, 15))
OFFERABLE_RELEASE_STATES = frozenset({"AVAILABLE", "HIL_ONLY"})
DOWNLOAD_ATTEMPT_LIMIT = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = (120, 600)
# Grants ADD issues after the first stop: the two resumed attempts, each
# re-sent once when the ESP lost the assignment response. A hard bound that
# holds even when no heartbeat counts a stop before the ESP asks again.
RESUMED_GRANT_LIMIT = 2 * (DOWNLOAD_ATTEMPT_LIMIT - 1)
# A re-offer the ESP has not taken this long after its backoff ended (HTTPS
# failing while heartbeats arrive, an ESP that reset or went offline, or a
# refusal ADD cannot name here) fails the deployment instead of holding the
# zone.
DOWNLOAD_RETRY_DEADLINE_SECONDS = 900
# No re-offer at all once this long has passed since the first stop.
DOWNLOAD_RESUME_WINDOW_SECONDS = 3600
# A heartbeat counts as built after a grant only by its uptime, not by ADD's
# receipt: the boot's start is estimated from its recent samples (receipt
# minus uptime is never earlier than the true start), and whole-second
# uptimes need a little slack. A heartbeat built that close before the fetch
# is followed by an UPDATING one when the attempt really started.
BOOT_START_SAMPLES = 60
ATTEMPT_UPTIME_SLACK_SECONDS = 2
DOWNLOAD_INTERRUPTED = "DOWNLOAD_INTERRUPTED"
DOWNLOAD_RETRY_OFFERED = "DOWNLOAD_RETRY_OFFERED"
DOWNLOAD_RETRIES_EXHAUSTED = "DOWNLOAD_RETRIES_EXHAUSTED"
DOWNLOAD_RETRY_NOT_TAKEN = "DOWNLOAD_RETRY_NOT_TAKEN"
DOWNLOAD_RETRY_WITHDRAWN = "DOWNLOAD_RETRY_WITHDRAWN"
HELD_DOWNLOAD_SWEEP_LIMIT = 50

TARGET_BOOT_INTERRUPTED = "TARGET_BOOT_INTERRUPTED"
TARGET_BOOT_INTERRUPTED_REPEATEDLY = "TARGET_BOOT_INTERRUPTED_REPEATEDLY"
UNCLASSIFIED = "UNCLASSIFIED"
TARGET_LIFETIME_LIMIT_SECONDS = 300
TERMINAL_TIME_SYNC_SECONDS = 30
# A log is received at most this long before ADD's estimate of the boot it
# belongs to (heartbeat transit delay); anything earlier is inconsistent.
RECEIPT_SKEW_SECONDS = 10
# A 2.6.x return report waits (409, repeated by the ESP every 60 s) at most
# this long after the target's last progress report for the return boot's
# first heartbeat, so both return paths read the same evidence.
RETURN_REPORT_WAIT_SECONDS = 600
RETRY_REQUESTED = "RETRY_REQUESTED"
# Interrupted boots of one release on one ESP before the owner's retry is
# refused here (and a new campaign needs the owner's override): after two,
# capture the ESP's UART at the site first.
TARGET_BOOT_RETRY_LIMIT = 2
# Journal bridges, the 2.7.0 writer, the one-shot recovery images and the
# 2.7.6 last-resort reformat carry their own return evidence, rollback
# expectations and HIL acceptance flows.
_OWN_RETURN_EVIDENCE = frozenset({"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22",
                                  "2.6.23", "2.7.0", "2.7.6", *RECOVERY_RELEASE_IDS})
_RECONCILE_ACTIVITIES = frozenset({"FULL_RECONCILE", "HISTORICAL_RECONCILE", "LIGHT_RECONCILE"})


class ReturnEvidencePending(ValueError):
    """The ESP's return report arrived before the return boot's first heartbeat."""


def _iso(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value is not None else None


def resume_enabled() -> bool:
    return bool(settings.firmware_download_resume_enabled)


def in_scope_version(value: str | None) -> bool:
    """2.6.0-2.6.15: the firmware whose download, journal and return code was verified."""
    version = semantic_version(value)
    return version is not None and RESUMABLE_RUNNING_VERSIONS[0] <= version <= RESUMABLE_RUNNING_VERSIONS[1]


def has_own_return_evidence(release: FirmwareRelease | None) -> bool:
    return release is None or release.version in _OWN_RETURN_EVIDENCE


def return_in_scope(deployment: FirmwareDeployment, release: FirmwareRelease | None) -> bool:
    """A return this module may classify: a 2.6.0-2.6.15 ESP, a release without its own evidence."""
    return not has_own_return_evidence(release) and in_scope_version(deployment.previous_version)


def assignment_needs_serializing(connector: Connector, deployment: FirmwareDeployment) -> bool:
    """An active 2.6.0-2.6.15 deployment whose assignment may be held, re-offered or failed here."""
    return (connector.firmware_family == "zkt" and in_scope_version(connector.firmware_version)
            and in_scope_version(deployment.previous_version))


LOCK_NOT_AVAILABLE = "55P03"


def _lock_not_available(error: OperationalError) -> bool:
    original = getattr(error, "orig", None)
    return (getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)) == LOCK_NOT_AVAILABLE


def lock_for_assignment(session: Session, connector: Connector,
                        deployment: FirmwareDeployment) -> FirmwareDeployment | None:
    """Lock the connector, then the deployment, and re-read both states.

    The heartbeat that records a stop holds the same connector lock, so a
    grant and an interruption are never decided from each other's stale
    reads, and a pause, cancel or revoke that failed the held download first
    is seen here. None when the deployment or its campaign is no longer active.

    Never waits: the poll runs on the event loop that serves every device
    WebSocket and holds the fleet-wide assignment lock, so a row another
    transaction holds (this ESP's own message, a campaign control) answers
    204 at once and the ESP asks again on its next 60 s poll; a 204 leaves
    its journal untouched.
    """
    def lock() -> FirmwareDeployment | None:
        session.scalar(select(Connector).where(Connector.id == connector.id).with_for_update(nowait=True)
                       .execution_options(populate_existing=True))
        return session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.id == deployment.id)
                              .with_for_update(nowait=True).execution_options(populate_existing=True))

    if session.get_bind().dialect.name != "postgresql":
        locked = lock()  # no row locks to wait for
    else:
        try:
            with session.begin_nested():  # a refused lock rolls back only this savepoint
                locked = lock()
        except OperationalError as error:
            if not _lock_not_available(error):
                raise
            return None
    if locked is None or locked.status not in ACTIVE_DEPLOYMENT_STATES:
        return None
    status = session.scalar(select(FirmwareCampaign.status).where(FirmwareCampaign.id == locked.campaign_id))
    return locked if status == "ACTIVE" else None


def _interruptions(session: Session, deployment: FirmwareDeployment) -> list[FirmwareEvent]:
    return list(session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED,
    ).order_by(FirmwareEvent.id)))


def _retry_after(event: FirmwareEvent) -> datetime | None:
    try:
        return parse_datetime((event.details or {})["retry_after"])
    except (KeyError, TypeError, ValueError):
        return None


def _hold_expired(session: Session, deployment: FirmwareDeployment, latest: FirmwareEvent,
                  now: datetime) -> str | None:
    """Why a held download may no longer be re-offered, or None."""
    retry_after = _retry_after(latest)
    if retry_after is None:
        return "no backoff was recorded for the stop"
    if now >= retry_after + timedelta(seconds=DOWNLOAD_RETRY_DEADLINE_SECONDS):
        return (f"the ESP did not take ADD's re-offer within {DOWNLOAD_RETRY_DEADLINE_SECONDS // 60} min of "
                f"{retry_after.strftime('%H:%M:%S')} UTC")
    first = session.scalar(select(func.min(FirmwareEvent.created_at)).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED))
    if first is not None and now >= ensure_utc(first) + timedelta(seconds=DOWNLOAD_RESUME_WINDOW_SECONDS):
        return f"the {DOWNLOAD_RESUME_WINDOW_SECONDS // 60} min resume window after the first stop closed"
    return None


def _boot_start(session: Session, connector: Connector, boot_id: str | None) -> datetime | None:
    """The latest estimate of a boot's start: the smallest receipt minus uptime over its recent samples."""
    if not boot_id:
        return None
    starts = [ensure_utc(created_at) - timedelta(seconds=uptime) for created_at, uptime in session.execute(
        select(DeviceTelemetry.created_at, DeviceTelemetry.uptime_seconds).where(
            DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.boot_id == boot_id,
            DeviceTelemetry.uptime_seconds.is_not(None))
        .order_by(DeviceTelemetry.id.desc()).limit(BOOT_START_SAMPLES)) if uptime >= 0]
    return min(starts) if starts else None


def _built_after(session: Session, connector: Connector, sample: DeviceTelemetry, at: datetime) -> bool:
    """Whether the ESP built `sample` after `at`, judged by its uptime as well as ADD's receipt.

    A heartbeat built before the ESP fetched the re-offer but delivered after
    the grant (a WebSocket read pause, a reconnect burst) is not after it.
    """
    at = ensure_utc(at)
    if (sample.uptime_seconds is None or sample.uptime_seconds < 0
            or ensure_utc(sample.created_at) <= at):
        return False
    started = _boot_start(session, connector, sample.boot_id)
    if started is None:
        return False
    return sample.uptime_seconds >= (at - started).total_seconds() - ATTEMPT_UPTIME_SLACK_SECONDS


def download_interruption(session: Session, *, connector: Connector, deployment: FirmwareDeployment,
                          release: FirmwareRelease, payload: Any, error_code: str, runtime_state: str,
                          now: datetime | None = None) -> dict | None:
    """Decide a sticky resumable download error from a 2.6.x heartbeat.

    None means the heartbeat is outside this path (the switch off, a target
    with its own return evidence, a campaign that is no longer ACTIVE or a
    release that is no longer offerable) and the caller fails closed as
    before. WAIT keeps the deployment active because the error belongs to an
    attempt already counted, or no heartbeat built after the latest offer has
    proved that attempt ended. RETRY_SCHEDULED records an interruption and a
    backoff. EXHAUSTED and NOT_TAKEN carry the error code and message the
    caller fails the deployment with.

    The caller holds the connector lock (the WebSocket heartbeat takes it
    first), which the assignment of a 2.6.x deployment also takes, so the
    latest grant read here is never stale. The ESP polls the assignment every
    60 s and reports UPDATING while a fetched attempt runs, so an attempt is
    counted only once a heartbeat built after the attempt's grant precedes
    this non-UPDATING one: a heartbeat built before the fetch cannot end it.
    """
    if (not resume_enabled() or connector.firmware_family != "zkt" or runtime_state != "DOWNLOADING"
            or error_code not in RESUMABLE_DOWNLOAD_ERRORS
            or deployment.status not in {"OFFERED", "DOWNLOADING"}
            or has_own_return_evidence(release)
            or not in_scope_version(payload.firmware_version)
            or not in_scope_version(deployment.previous_version)
            or not _versions_match(payload.ota.target_version, deployment.target_version)):
        return None
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    if campaign is None or campaign.status != "ACTIVE" or release.state not in OFFERABLE_RELEASE_STATES:
        # A paused, cancelled or revoked attempt is never held for a re-offer.
        return None
    now = now or utc_now()
    interruptions = _interruptions(session, deployment)
    statement = select(func.max(FirmwareDownloadGrant.created_at)).where(
        FirmwareDownloadGrant.deployment_id == deployment.id)
    if interruptions:
        statement = statement.where(FirmwareDownloadGrant.created_at > interruptions[-1].created_at)
    offered_at = session.scalar(statement)
    if offered_at is None:
        if not interruptions:
            return None  # an attempt ADD never granted is not resumed
        expired = _hold_expired(session, deployment, interruptions[-1], now)
        if expired:
            last = interruptions[-1].details or {}
            return {"decision": "NOT_TAKEN", "error_code": DOWNLOAD_RETRY_NOT_TAKEN, "message": (
                f"The resumable download stopped ({last.get('error_code') or error_code}) and {expired}. The "
                "campaign is paused; check the ESP's HTTPS path to ADD and its OTA admission before a new "
                "attempt.")[:1000]}
        return {"decision": "WAIT"}
    previous = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == connector.id)
                              .order_by(DeviceTelemetry.id.desc()).limit(1))
    if (previous is None or previous.boot_id != connector.boot_id
            or not _built_after(session, connector, previous, offered_at)):
        return {"decision": "WAIT"}
    number = len(interruptions) + 1
    bytes_written = min(release.image_size, max(deployment.bytes_written, payload.ota.bytes_written))
    window_closed = bool(interruptions) and now >= ensure_utc(interruptions[0].created_at) + timedelta(
        seconds=DOWNLOAD_RESUME_WINDOW_SECONDS)
    exhausted = number >= DOWNLOAD_ATTEMPT_LIMIT or window_closed
    retry_after = None if exhausted else now + timedelta(seconds=DOWNLOAD_RETRY_BACKOFF_SECONDS[number - 1])
    session.add(FirmwareEvent(deployment_id=deployment.id, state=DOWNLOAD_INTERRUPTED, created_at=now, details={
        "interruption": number, "attempt_limit": DOWNLOAD_ATTEMPT_LIMIT, "error_code": error_code,
        "bytes_written": bytes_written, "image_size": release.image_size,
        "running_version": payload.firmware_version, "boot_id": connector.boot_id,
        "attempt_offered_at": _iso(offered_at), "retry_after": _iso(retry_after),
    }))
    if exhausted:
        stops = (f"stopped {number} times" if not window_closed else
                 f"stopped again after the {DOWNLOAD_RESUME_WINDOW_SECONDS // 60} min resume window closed")
        return {"decision": "EXHAUSTED", "error_code": DOWNLOAD_RETRIES_EXHAUSTED, "message": (
            f"The resumable download {stops}; the last stop was {error_code} at "
            f"{bytes_written} of {release.image_size} bytes. The campaign is paused; check the site's "
            "network path to ADD before a new attempt.")}
    # The ESP resumes at its in-RAM byte count (the heartbeat's figure) on
    # this boot, or at the journal's last 64 KiB checkpoint after a reboot.
    # ADD's bytes_written stays the last progress the ESP reported, which
    # keeps it monotonic; the heartbeat's figure is kept in the event.
    deployment.error_code = DOWNLOAD_INTERRUPTED
    deployment.error_message = (
        f"The resumable download stopped ({error_code}) at {bytes_written} of {release.image_size} bytes. "
        f"ADD re-offers the same signed image after {retry_after.strftime('%H:%M:%S')} UTC; this was attempt "
        f"{number} of {DOWNLOAD_ATTEMPT_LIMIT}.")
    deployment.updated_at = now
    return {"decision": "RETRY_SCHEDULED", "retry_after": retry_after.isoformat(), "interruption": number}


def awaiting_retry_offer(session: Session, deployment: FirmwareDeployment) -> FirmwareEvent | None:
    """The latest interruption of an attempt ADD has neither re-offered nor granted since, or None."""
    latest = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED,
    ).order_by(FirmwareEvent.id.desc()).limit(1))
    if latest is None:
        return None
    offered = session.scalar(select(FirmwareEvent.id).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_RETRY_OFFERED,
        FirmwareEvent.id > latest.id).limit(1))
    if offered is not None:
        return None
    granted = session.scalar(select(FirmwareDownloadGrant.id).where(
        FirmwareDownloadGrant.deployment_id == deployment.id,
        FirmwareDownloadGrant.created_at >= latest.created_at).limit(1))
    return None if granted is not None else latest


def _first_interruption_at(session: Session, deployment: FirmwareDeployment) -> datetime | None:
    first = session.scalar(select(func.min(FirmwareEvent.created_at)).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == DOWNLOAD_INTERRUPTED))
    return ensure_utc(first) if first is not None else None


def _re_offers_spent(session: Session, deployment: FirmwareDeployment) -> str | None:
    """Why ADD may grant this interrupted download no further attempt, or None.

    Counts every grant issued after the first stop, whether or not a
    heartbeat counted the previous attempt's stop before the ESP asked again,
    so the bound holds without heartbeats too.
    """
    first = _first_interruption_at(session, deployment)
    if first is None:
        return None
    granted = int(session.scalar(select(func.count(FirmwareDownloadGrant.id)).where(
        FirmwareDownloadGrant.deployment_id == deployment.id, FirmwareDownloadGrant.created_at >= first)) or 0)
    if granted >= RESUMED_GRANT_LIMIT:
        return (f"ADD already issued the {RESUMED_GRANT_LIMIT} assignments allowed after the first stop (two "
                "resumed attempts, each re-sent once)")
    return None


def resumed_attempt_refusal(session: Session, connector: Connector, deployment: FirmwareDeployment, *,
                            now: datetime | None = None) -> bool:
    """Fail closed, instead of granting, a poll for an interrupted download ADD already re-offered.

    The caller holds the connector and deployment locks (lock_for_assignment),
    passed every admission check and found no un-offered stop
    (awaiting_retry_offer is None). 2.6.0-2.6.15 polls the assignment only
    while no attempt runs, so this poll means the latest resumed attempt
    ended (or the ESP reset) before a heartbeat counted it: with the grant
    bound reached, the resume window closed or the switch off, the deployment
    fails as a counted final stop would. False lets the caller grant it.
    """
    if deployment.status not in {"OFFERED", "DOWNLOADING"}:
        return False  # a downloaded, verified or booted attempt keeps its unchanged path
    now = now or utc_now()
    first = _first_interruption_at(session, deployment)
    if first is None:
        return False
    if not resume_enabled():
        reason, code = "download resume is turned off", DOWNLOAD_RETRY_WITHDRAWN
    else:
        reason, code = _re_offers_spent(session, deployment), DOWNLOAD_RETRIES_EXHAUSTED
        if reason is None and now >= first + timedelta(seconds=DOWNLOAD_RESUME_WINDOW_SECONDS):
            reason = f"the {DOWNLOAD_RESUME_WINDOW_SECONDS // 60} min resume window after the first stop closed"
        if reason is None:
            return False
    from zk_add.service import fail_ota_deployment

    message = (f"The resumable download stopped and the ESP asked for it again, but {reason}. The campaign is "
               "paused; check the site's network path to ADD before a new attempt." if code ==
               DOWNLOAD_RETRIES_EXHAUSTED else
               f"The resumable download stopped and ADD withdrew its re-offer: {reason}.")[:1000]
    fail_ota_deployment(session, connector=connector, deployment=deployment, error_code=code,
                        error_message=message, runtime_state="DOWNLOADING")
    return True


def download_retry_hold(session: Session, deployment: FirmwareDeployment, latest: FirmwareEvent, *,
                        now: datetime | None = None) -> bool:
    """Withhold a held deployment's assignment, fail it closed, or record its one re-offer.

    The caller holds the connector and deployment locks (lock_for_assignment)
    and passed every admission check; `latest` is the un-offered interruption.
    True withholds the assignment (204); False lets the caller grant it.
    """
    now = now or utc_now()
    if not resume_enabled():
        fail_held_download(session, deployment, reason="download resume is turned off")
        return True
    expired = _hold_expired(session, deployment, latest, now)
    if expired:
        fail_held_download(session, deployment, reason=expired, error_code=DOWNLOAD_RETRY_NOT_TAKEN)
        return True
    retry_after = _retry_after(latest)
    if now < retry_after:
        return True
    spent = _re_offers_spent(session, deployment)
    if spent:
        fail_held_download(session, deployment, reason=spent, error_code=DOWNLOAD_RETRIES_EXHAUSTED)
        return True
    details = latest.details or {}
    deployment.attempt_count += 1
    session.add(FirmwareEvent(deployment_id=deployment.id, state=DOWNLOAD_RETRY_OFFERED, details={
        "after_interruption": details.get("interruption"), "attempt": int(details.get("interruption") or 0) + 1,
        "attempt_limit": DOWNLOAD_ATTEMPT_LIMIT}))
    session.flush()  # sessions run without autoflush; record the attempt once
    return False


def fail_held_download(session: Session, deployment: FirmwareDeployment, *, reason: str,
                       error_code: str = DOWNLOAD_RETRY_WITHDRAWN) -> bool:
    """Fail closed a deployment ADD holds for a re-offer; its ESP already stopped.

    Locks the connector, then the deployment, and re-checks the hold: no
    re-offer and no grant since the ESP reported the stop, so nothing can be
    writing the slot (the same outcome as the first sticky error before).
    """
    session.scalar(select(Connector.id).where(Connector.id == deployment.connector_id).with_for_update())
    locked = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.id == deployment.id)
                            .with_for_update().execution_options(populate_existing=True))
    if locked is None or locked.status not in {"OFFERED", "DOWNLOADING"}:
        return False
    latest = awaiting_retry_offer(session, locked)
    connector = session.get(Connector, locked.connector_id)
    if latest is None or connector is None:
        return False
    details = latest.details or {}
    stop = (f"The resumable download stopped ({details.get('error_code') or 'DOWNLOAD_INCOMPLETE'}) at "
            f"{details.get('bytes_written')} of {details.get('image_size')} bytes")
    message = (f"{stop} and {reason}. The campaign is paused; check the ESP's HTTPS path to ADD and its OTA "
               "admission before a new attempt." if error_code == DOWNLOAD_RETRY_NOT_TAKEN
               else f"{stop} and {reason}. The campaign is paused; check the site's network path to ADD before "
               "a new attempt." if error_code == DOWNLOAD_RETRIES_EXHAUSTED
               else f"{stop} and ADD withdrew its re-offer: {reason}.")[:1000]
    from zk_add.service import fail_ota_deployment

    fail_ota_deployment(session, connector=connector, deployment=locked, error_code=error_code,
                        error_message=message, runtime_state="DOWNLOADING")
    return True


def fail_held_downloads(session: Session, campaign_id: int, *, reason: str) -> int:
    """Fail every held download of one campaign (pause, cancel, revoke).

    Only a deployment that is held on an unlocked read is locked (connector,
    then deployment) and re-checked; every other attempt takes no row lock.
    """
    rows = list(session.scalars(select(FirmwareDeployment).where(
        FirmwareDeployment.campaign_id == campaign_id,
        FirmwareDeployment.status.in_(["OFFERED", "DOWNLOADING"]),
        exists().where(FirmwareEvent.deployment_id == FirmwareDeployment.id,
                       FirmwareEvent.state == DOWNLOAD_INTERRUPTED)).order_by(FirmwareDeployment.id)))
    return sum(1 for row in rows
               if awaiting_retry_offer(session, row) is not None and fail_held_download(session, row, reason=reason))


def expire_held_downloads(session: Session, *, now: datetime | None = None,
                          limit: int = HELD_DOWNLOAD_SWEEP_LIMIT) -> int:
    """Maintenance sweep: fail held downloads that may no longer be re-offered.

    The assignment and heartbeat paths decide the same on the ESP's next
    request; this covers an ESP that reset or went offline during its
    backoff, a campaign paused by any path, and the switch turned off, so a
    hold never keeps the zone busy for long.
    """
    now = now or utc_now()
    rows = list(session.scalars(select(FirmwareDeployment).where(
        FirmwareDeployment.status.in_(["OFFERED", "DOWNLOADING"]),
        exists().where(FirmwareEvent.deployment_id == FirmwareDeployment.id,
                       FirmwareEvent.state == DOWNLOAD_INTERRUPTED),
    ).order_by(FirmwareDeployment.id).limit(limit)))
    failed = 0
    for row in rows:
        latest = awaiting_retry_offer(session, row)
        if latest is None:
            continue
        campaign_status = session.scalar(select(FirmwareCampaign.status).where(
            FirmwareCampaign.id == row.campaign_id))
        if not resume_enabled():
            reason, code = "download resume is turned off", DOWNLOAD_RETRY_WITHDRAWN
        elif campaign_status != "ACTIVE":
            reason, code = f"the campaign is {str(campaign_status or 'missing').lower()}", DOWNLOAD_RETRY_WITHDRAWN
        else:
            reason, code = _hold_expired(session, row, latest, now), DOWNLOAD_RETRY_NOT_TAKEN
            if reason is None:
                continue
        failed += int(fail_held_download(session, row, reason=reason, error_code=code))
    return failed


def _boot_events(events: list[FirmwareEvent]) -> list[FirmwareEvent]:
    return [event for event in events if event.state in {"BOOTED_PENDING", "RECONCILING"}]


def rollback_evidence(session: Session, *, connector: Connector, deployment: FirmwareDeployment,
                      return_evidence: dict, now: datetime | None = None,
                      previous_firmware_resets: dict | None = None,
                      location_resets: dict | None = None) -> dict:
    """Evidence around a target boot that ended in a return to the previous image (A2).

    Every field is an observation or a bounded estimate; none names a cause.
    previous_firmware_resets reuses a stored 7-day reset history, which no
    later sample can change, and location_resets a stored complete peer scan.
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
    # The log's device_time is the ESP's unvalidated envelope send time, not
    # the sync time, so ADD's receipt is used: an upper bound on when the
    # terminal answered (delivery can lag), never a lower one.
    received = session.scalar(select(DeviceLog.received_at).where(
        DeviceLog.connector_id == connector.id, DeviceLog.boot_id == return_boot,
        DeviceLog.code == "ZKT_TIME_SYNC_OK").order_by(DeviceLog.received_at, DeviceLog.id).limit(1))
    sync_after, sync_source = None, None
    if received is not None:
        delta = (ensure_utc(received) - returned_at).total_seconds()
        if delta < -RECEIPT_SKEW_SECONDS:
            sync_source = "inconsistent"  # received before the boot ADD estimated
        else:
            sync_after, sync_source = max(0, round(delta)), "received_at"
    if previous_firmware_resets is None:
        previous_firmware_resets = boot_history(
            session, connector, until=started_after or returned_at, firmware_version=deployment.previous_version)
    if location_resets is None:
        location_resets = peer_resets(session, connector, at=returned_at, now=now)
    return {
        "schema_version": 2,
        "observation": "PREVIOUS_FIRMWARE_OBSERVED",
        "return_source": return_evidence.get("source"),
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
            # Any report of the boot, not only the last: a RUNTIME_HEALTHY
            # ADD committed but the ESP never received is followed by WAITING
            # again when health drops, and that boot was not interrupted early.
            "reported_runtime_healthy": any((event.details or {}).get("error_code") == "RUNTIME_HEALTHY"
                                            for event in booted),
            "reported_reconciling": any(event.state == "RECONCILING" for event in booted),
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
        "previous_firmware_resets": previous_firmware_resets,
        "terminal": {
            "time_sync_after_new_boot_seconds": sync_after,
            # True only when even the upper bound is within 30 s; a later
            # receipt cannot show the terminal answered late.
            "answered_within_30_seconds": (True if sync_after is not None
                                           and sync_after <= TERMINAL_TIME_SYNC_SECONDS else None),
            "bound": "upper" if sync_after is not None else None,
            "source": sync_source,
        },
        "location_resets": location_resets,
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
    if has_own_return_evidence(release):
        reasons.append("TARGET_HAS_OWN_RETURN_EVIDENCE")
    if not in_scope_version(deployment.previous_version):
        # 2.4.12 and 2.5.2 write through plain SPIFFS after a torn first boot
        # (owner decision 6); 2.6.16+ and 2.7.x report their own returns.
        reasons.append("PREVIOUS_FIRMWARE_OUT_OF_SCOPE")
    if deployment.status != "BOOTED_PENDING" or target.get("reported_reconciling"):
        reasons.append("TARGET_REPORTED_CONFIRMATION")
    if target["last_progress_code"] == "RUNTIME_HEALTHY" or target.get("reported_runtime_healthy"):
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
        + (f"time sync received {sync} s after the new boot" if sync is not None else "time sync not observed")
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


def _started_after_target(session: Session, connector: Connector, deployment: FirmwareDeployment,
                          target_boot: FirmwareEvent, *, boot_id: str, started: datetime) -> bool:
    """Whether a boot of the previous firmware started after the target's digest-checked boot.

    A boot that started more than RECEIPT_SKEW_SECONDS after ADD received the
    target's boot report is after it. A faster reset is recognised from
    READY_TO_BOOT instead: the restart into the target followed that report,
    so a boot that started after it and is not the boot that sent it (the
    pre-install boot, whose delayed samples could otherwise look new) is the
    return. ADD's start estimates are never earlier than the true start.
    """
    if started > ensure_utc(target_boot.created_at) + timedelta(seconds=RECEIPT_SKEW_SECONDS):
        return True
    ready = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "READY_TO_BOOT",
        FirmwareEvent.id < target_boot.id).order_by(FirmwareEvent.id.desc()).limit(1))
    if ready is None or started <= ensure_utc(ready.created_at):
        return False
    pre_install_boot = session.scalar(select(DeviceTelemetry.boot_id).where(
        DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.created_at < ready.created_at,
        DeviceTelemetry.boot_id.is_not(None),
    ).order_by(DeviceTelemetry.created_at.desc(), DeviceTelemetry.id.desc()).limit(1))
    return pre_install_boot is not None and pre_install_boot != boot_id


def device_return_evidence(session: Session, *, connector: Connector,
                           deployment: FirmwareDeployment) -> dict | None:
    """Return-boot evidence for an ESP's own BOOTLOADER_ROLLBACK report, from ADD's heartbeats.

    The same shape as the heartbeat-inferred return: the latest heartbeat of
    the current boot, on the previous firmware, from a boot that started after
    the target's digest-checked boot report. None while there is none yet.
    """
    target_boot = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["BOOTED_PENDING", "RECONCILING"]),
    ).order_by(FirmwareEvent.id.desc()).limit(1))
    if target_boot is None or not connector.boot_id:
        return None
    row = session.execute(select(
        DeviceTelemetry.id, DeviceTelemetry.created_at, DeviceTelemetry.uptime_seconds, DeviceTelemetry.payload,
    ).where(DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.boot_id == connector.boot_id)
        .order_by(DeviceTelemetry.id.desc()).limit(1)).first()
    if row is None or row.uptime_seconds is None or row.uptime_seconds < 0:
        return None
    version = row.payload.get("firmware_version") if isinstance(row.payload, dict) else None
    if (not _versions_match(version, deployment.previous_version)
            or _versions_match(version, deployment.target_version)):
        return None
    observed_at = ensure_utc(row.created_at)
    started = observed_at - timedelta(seconds=row.uptime_seconds)
    if not _started_after_target(session, connector, deployment, target_boot,
                                 boot_id=connector.boot_id, started=started):
        return None  # a sample of the pre-install boot cannot show a return
    return {
        "source": "device_reported_bootloader_rollback", "reset_cause": "not_reported",
        "deployment_id": deployment.deployment_id, "target_version": deployment.target_version,
        "previous_version": deployment.previous_version, "target_boot_event_id": target_boot.id,
        "prior_telemetry_id": row.id, "boot_id": connector.boot_id, "uptime_seconds": row.uptime_seconds,
        "observed_at": observed_at.isoformat(),
    }


def return_report_may_wait(session: Session, deployment: FirmwareDeployment, *, now: datetime | None = None) -> bool:
    """Whether a return report still waits for the return boot's first heartbeat."""
    last = session.scalar(select(func.max(FirmwareEvent.created_at)).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["BOOTED_PENDING", "RECONCILING"])))
    return last is not None and (now or utc_now()) < ensure_utc(last) + timedelta(seconds=RETURN_REPORT_WAIT_SECONDS)


def interrupted_message(diagnosis: dict) -> str:
    lifetime = diagnosis["target_boot"]["estimated_lifetime_seconds"]
    return (
        f"The ESP reset about {lifetime['min'] if lifetime['min'] is not None else '?'}-{lifetime['max']} s after "
        f"booting {diagnosis['target_version']} and before the image was confirmed, so the bootloader returned "
        f"it to {diagnosis['previous_version']}. The new image sent no failure report: ADD cannot tell a crash "
        "from a power or reset event, so read the rollback evidence. The campaign is paused. Before any new "
        "attempt check the ESP's power, its reset history and its current storage and queue state (shown with "
        "the retry); the owner may then retry the same target with password step-up."
    )


def stored_diagnosis(session: Session, deployment: FirmwareDeployment) -> FirmwareEvent | None:
    return session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "ROLLED_BACK",
    ).order_by(FirmwareEvent.id.desc()).limit(1))


def refreshed_diagnosis(session: Session, deployment: FirmwareDeployment, *, now: datetime | None = None) -> dict | None:
    """The stored classification with bounded windows re-read now (late logs and peer heartbeats).

    The 7-day reset history and a peer scan that was already complete are
    reused from the stored diagnosis.
    """
    event = stored_diagnosis(session, deployment)
    details = (event.details or {}) if event is not None else {}
    stored, return_evidence = details.get("diagnosis"), details.get("return_evidence")
    connector = session.get(Connector, deployment.connector_id)
    if not isinstance(stored, dict) or not isinstance(return_evidence, dict) or connector is None:
        return None
    resets = stored.get("previous_firmware_resets")
    location = stored.get("location_resets")
    if not isinstance(resets, dict):
        # No stored evidence (EVIDENCE_UNAVAILABLE): a detail view never runs
        # the 7-day reset scan, so it shows what was stored.
        return stored
    try:
        evidence = rollback_evidence(session, connector=connector, deployment=deployment,
                                     return_evidence=return_evidence, now=now,
                                     previous_firmware_resets=resets,
                                     location_resets=(location if isinstance(location, dict)
                                                      and location.get("complete") is True else None))
    except (KeyError, TypeError, ValueError):
        return stored
    return {**evidence, "classification": stored.get("classification"),
            "unclassified_reasons": stored.get("unclassified_reasons", []),
            "classified_at": _iso(event.created_at), "summary": evidence_summary(evidence)}


def _interrupted_boots(session: Session, *, connector_id: int, release_id: int) -> int:
    return int(session.scalar(select(func.count(FirmwareDeployment.id)).where(
        FirmwareDeployment.connector_id == connector_id,
        FirmwareDeployment.release_id == release_id,
        FirmwareDeployment.status == "ROLLED_BACK",
        FirmwareDeployment.error_code == TARGET_BOOT_INTERRUPTED)) or 0)


def repeated_interruption_warnings(session: Session, connector: Connector, release: FirmwareRelease) -> list[dict]:
    """A campaign warning (owner-overridable) for an ESP whose boots of this release were interrupted twice."""
    count = _interrupted_boots(session, connector_id=connector.id, release_id=release.id)
    if count < TARGET_BOOT_RETRY_LIMIT:
        return []
    return [{
        "code": TARGET_BOOT_INTERRUPTED_REPEATEDLY, "connector_id": connector.connector_id,
        "display_name": connector.display_name, "interrupted_boots": count, "limit": TARGET_BOOT_RETRY_LIMIT,
        "message": (f"{connector.display_name}: {count} boots of {release.version} on this ESP were interrupted "
                    "before confirmation; capture the ESP's UART at the site before another attempt."),
    }]


def _pending_others(session: Session, campaign: FirmwareCampaign) -> int:
    """PENDING deployments that resuming the campaign would also offer."""
    return int(session.scalar(select(func.count(FirmwareDeployment.id)).where(
        FirmwareDeployment.campaign_id == campaign.id, FirmwareDeployment.status == "PENDING")) or 0)


def _rollback_pause_reason(connector: Connector | None, deployment: FirmwareDeployment) -> str | None:
    """The pause reason record_progress wrote when this deployment rolled back.

    record_progress keeps an earlier pause reason (the owner's hold, a
    revoke) instead of writing this one, so the retry never lifts a pause
    the rollback did not cause.
    """
    from zk_add.ota import deployment_pause_reason

    if connector is None:
        return None
    return deployment_pause_reason(connector, state="ROLLED_BACK", error_code=deployment.error_code)


STORAGE_IO_FAULT_HISTORY = "ESP_STORAGE_IO_FAULT_HISTORY"


def storage_history_warnings(session: Session, connector: Connector, *,
                             now: datetime | None = None) -> list[dict]:
    """An owner-overridable retry warning for storage I/O faults on the ESP's earlier boots.

    The current boot's faults refuse the retry outright (retry_refusal); the
    target's first boot repeats its storage work, so faults its earlier boots
    reported in the last 14 days are shown and need the owner's override.
    """
    from zk_add.device_health import rollback_slot_residue_history

    history = rollback_slot_residue_history(session, connector, now=now)
    if history.get("clean"):
        return []
    faults = history.get("faults") or []
    latest = max(faults, key=lambda fault: str(fault.get("at") or "")) if faults else {}
    names = ", ".join(sorted(key for key in latest if key not in {"boot_id", "at"}))[:120]
    return [{
        "code": STORAGE_IO_FAULT_HISTORY, "connector_id": connector.connector_id,
        "display_name": connector.display_name, "days": history.get("days"),
        "boots_checked": history.get("boots_checked"), "faults": faults,
        "message": (f"{connector.display_name}: {len(faults)}{'+' if len(faults) >= 10 else ''} earlier boot(s) "
                    f"in the last {history.get('days')} days reported storage I/O faults (latest at "
                    f"{latest.get('at') or 'an unknown time'}: {names or 'unnamed'}). The target's first boot "
                    "repeats its storage work; check the ESP's storage and power before another attempt."),
    }]


def retry_warnings(session: Session, connector: Connector, *, now: datetime | None = None) -> list[dict]:
    """Every owner-overridable warning the retry shows: stability (A3) and earlier storage I/O faults."""
    return [*stability_warnings(session, connector, now=now), *storage_history_warnings(session, connector, now=now)]


def esp_state(session: Session, connector: Connector | None) -> dict | None:
    """The ESP's current storage and queue state, shown to the owner with the retry."""
    if connector is None:
        return None
    from zk_add.device_health import storage_io_fault

    diagnostics = connector.firmware_diagnostics if isinstance(connector.firmware_diagnostics, dict) else {}
    storage = diagnostics.get("storage") if isinstance(diagnostics.get("storage"), dict) else None
    latest = session.execute(select(DeviceTelemetry.created_at, DeviceTelemetry.outbox_depth).where(
        DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.boot_id == connector.boot_id,
    ).order_by(DeviceTelemetry.id.desc()).limit(1)).first() if connector.boot_id else None
    return {
        "connected": bool(connector.connected),
        "firmware_version": connector.firmware_version,
        "boot_id": connector.boot_id,
        "storage_reported": storage is not None,
        "storage_durability": storage.get("durability") if storage else None,
        "storage_io_faults": (storage_io_fault(storage) or None) if storage else None,
        "diagnostics_at": _iso(connector.firmware_diagnostics_at),
        "outbox_depth": latest.outbox_depth if latest is not None else None,
        "last_heartbeat_at": _iso(latest.created_at) if latest is not None else None,
    }


def retry_refusal(session: Session, campaign: FirmwareCampaign, deployment: FirmwareDeployment) -> str | None:
    """Why the owner's retry is refused now, or None. The action re-checks everything."""
    from zk_add.zkt_hil_schedule import applies as nationwide_schedule_applies

    if deployment.status != "ROLLED_BACK" or deployment.error_code != TARGET_BOOT_INTERRUPTED:
        return "Only a target boot that ADD classified TARGET_BOOT_INTERRUPTED can be retried."
    if campaign.status != "PAUSED":
        return ("A cancelled campaign cannot retry; start a new campaign." if campaign.status == "CANCELLED"
                else "The campaign must be paused to retry a target.")
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or release.state not in OFFERABLE_RELEASE_STATES:
        return "The signed release is no longer available."
    if has_own_return_evidence(release) or nationwide_schedule_applies(release):
        return "This release has its own return evidence and HIL schedule; start a new campaign."
    if not in_scope_version(deployment.previous_version):
        return (f"A return to {deployment.previous_version} after an interrupted first boot can lose queued "
                "punches with no gap record (owner decision 6). Check the ESP's queue and the terminal's records "
                "against ADD, then start a new campaign.")
    later = session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.campaign_id == campaign.id, FirmwareDeployment.connector_id == deployment.connector_id,
        FirmwareDeployment.id > deployment.id).limit(1))
    if later is not None:
        return "A later attempt for this ESP already exists in the campaign."
    busy = session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.campaign_id == campaign.id,
        FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES)).limit(1))
    if busy is not None:
        return "Another deployment in this campaign is still active."
    if _interrupted_boots(session, connector_id=deployment.connector_id,
                          release_id=deployment.release_id) >= TARGET_BOOT_RETRY_LIMIT:
        return (f"{TARGET_BOOT_RETRY_LIMIT} boots of {release.version} on this ESP were interrupted. Capture the "
                "ESP's UART at the site before another attempt.")
    connector = session.get(Connector, deployment.connector_id)
    if campaign.pause_reason != _rollback_pause_reason(connector, deployment):
        # Resuming would also clear a pause the owner has not reviewed here.
        return (f"The campaign was paused for another reason ({campaign.pause_reason or 'none recorded'}). "
                "Review it, then resume the campaign or start a new one.")
    if connector is None or not connector.active or connector.zone_id != campaign.zone_id:
        return "The ESP is no longer active in this campaign's zone."
    if not connector.connected:
        return "The ESP is offline."
    if not _versions_match(connector.firmware_version, deployment.previous_version):
        return f"The ESP no longer runs {deployment.previous_version}."
    if connector.ota_running_partition not in {"ota_0", "ota_1"}:
        return "The ESP is not running from an OTA application slot."
    state = esp_state(session, connector) or {}
    if state.get("storage_io_faults"):
        faults = ", ".join(f"{name}={value}" for name, value in sorted(state["storage_io_faults"].items()))
        return (f"The ESP reports storage I/O faults on its current boot ({faults})"[:240]
                + "; recover its storage before another attempt.")
    return None


def retry_view(session: Session, campaign: FirmwareCampaign, deployment: FirmwareDeployment) -> dict | None:
    """A UI hint for an interrupted deployment in campaign detail; never authority.

    No 7-day stability scan here: the owner's dialog reads retry_preflight.
    """
    if deployment.status != "ROLLED_BACK" or deployment.error_code != TARGET_BOOT_INTERRUPTED:
        return None
    refusal = retry_refusal(session, campaign, deployment)
    return {"available": refusal is None, "refusal": refusal,
            "interrupted_boots": _interrupted_boots(session, connector_id=deployment.connector_id,
                                                    release_id=deployment.release_id),
            "limit": TARGET_BOOT_RETRY_LIMIT,
            "pending_deployments": _pending_others(session, campaign) if refusal is None else 0}


def _load_retry_target(session: Session, campaign_public_id: str, deployment_public_id: str, *,
                       lock: bool) -> tuple[FirmwareCampaign, FirmwareDeployment]:
    campaign_row = session.scalar(select(FirmwareCampaign.id).where(
        FirmwareCampaign.campaign_id == campaign_public_id))
    if campaign_row is None:
        raise LookupError("Firmware campaign not found.")
    statement = select(FirmwareDeployment).where(
        FirmwareDeployment.deployment_id == deployment_public_id, FirmwareDeployment.campaign_id == campaign_row)
    campaign_statement = select(FirmwareCampaign).where(FirmwareCampaign.id == campaign_row)
    if lock:
        # Deployment before campaign, the order record_progress and campaign
        # control lock them in.
        statement = statement.with_for_update().execution_options(populate_existing=True)
        campaign_statement = campaign_statement.with_for_update().execution_options(populate_existing=True)
    deployment = session.scalar(statement)
    if deployment is None:
        raise LookupError("Firmware deployment not found in this campaign.")
    return session.scalar(campaign_statement), deployment


def retry_preflight(session: Session, *, campaign_public_id: str, deployment_public_id: str,
                    now: datetime | None = None) -> dict:
    """What the owner reviews before the retry: refusal, stability warnings, pending offers, ESP state."""
    campaign, deployment = _load_retry_target(session, campaign_public_id, deployment_public_id, lock=False)
    if deployment.status != "ROLLED_BACK" or deployment.error_code != TARGET_BOOT_INTERRUPTED:
        raise ValueError("Only a target boot that ADD classified TARGET_BOOT_INTERRUPTED can be retried.")
    refusal = retry_refusal(session, campaign, deployment)
    connector = session.get(Connector, deployment.connector_id)
    warnings = retry_warnings(session, connector, now=now) if refusal is None and connector is not None else []
    return {"campaign_id": campaign.campaign_id, "deployment_id": deployment.deployment_id,
            "target_version": deployment.target_version, "available": refusal is None, "refusal": refusal,
            "interrupted_boots": _interrupted_boots(session, connector_id=deployment.connector_id,
                                                    release_id=deployment.release_id),
            "limit": TARGET_BOOT_RETRY_LIMIT, "stability_warnings": warnings,
            "stability_warnings_digest": warnings_digest(warnings),
            "pending_deployments": _pending_others(session, campaign) if refusal is None else 0,
            "esp_state": esp_state(session, connector)}


def retry_interrupted_target(session: Session, *, campaign_public_id: str, deployment_public_id: str,
                             actor: str, override_stability_warnings: bool, reason: str | None = None,
                             typed_confirmation: str | None = None, stability_warnings_digest: str | None = None,
                             resume_pending_deployments: int = 0, now: datetime | None = None) -> dict:
    """The owner's one-click retry: one new PENDING attempt in the same paused campaign.

    No typed version, reason or password: typed_confirmation is ignored. The
    new attempt is offered only through every ordinary admission check.
    Resuming the campaign also offers its other PENDING deployments, so the
    request names their number (from the review), and overrides exactly the
    stability warnings the review showed.
    """
    now = now or utc_now()
    reason = (reason or "").strip() or "One-click retry of an interrupted target boot"
    campaign, deployment = _load_retry_target(session, campaign_public_id, deployment_public_id, lock=True)
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None:
        raise ValueError("The signed release is no longer available.")
    refusal = retry_refusal(session, campaign, deployment)
    if refusal:
        raise ValueError(refusal)
    pending = _pending_others(session, campaign)
    if pending != resume_pending_deployments:
        raise ValueError(f"Retrying resumes the campaign, which also offers its {pending} other pending "
                         "deployment(s); review and confirm that number.")
    connector = session.get(Connector, deployment.connector_id)
    warnings = retry_warnings(session, connector, now=now)
    if warnings and not override_stability_warnings:
        raise ValueError("ESP stability warnings need the owner's override: "
                         + " ".join(warning["message"] for warning in warnings))
    if warnings and stability_warnings_digest != warnings_digest(warnings):
        raise ValueError("The ESP stability warnings changed since they were reviewed; review them again.")
    retry = FirmwareDeployment(
        deployment_id=secrets.token_hex(16), campaign_id=campaign.id, release_id=release.id,
        connector_id=connector.id, previous_version=connector.firmware_version, target_version=release.version)
    session.add(retry)
    session.flush()
    overridden = [{"code": warning["code"], "message": warning["message"]} for warning in warnings]
    session.add(FirmwareEvent(deployment_id=retry.id, state=RETRY_REQUESTED, details={
        "retry_of": deployment.deployment_id, "classification": TARGET_BOOT_INTERRUPTED, "actor": actor,
        "reason": reason, "interrupted_boots": _interrupted_boots(
            session, connector_id=deployment.connector_id, release_id=deployment.release_id),
        "stability_warnings_overridden": overridden, "resumed_pending_deployments": pending,
        "esp_state": esp_state(session, connector)}))
    campaign.status = "ACTIVE"
    campaign.pause_reason = None
    campaign.updated_at = now
    return {"campaign_id": campaign.campaign_id, "status": campaign.status,
            "deployment_id": retry.deployment_id, "retry_of": deployment.deployment_id,
            "connector_id": connector.connector_id, "stability_warnings_overridden": overridden,
            "resumed_pending_deployments": pending}


def rollback_slot_residue_record(session: Session, connector: Connector) -> dict | None:
    """ADD's own record that a 2.7.x download wrote the other slot after the last 2.6.15 install.

    The latest successful deployment on this ESP must be the 2.6.15 install,
    and a later 2.7.x deployment must have reported written bytes. A connector
    row is one ESP: onboarding keys it by the ESP's hardware ID and never
    rebinds it, so a replacement ESP is a new connector with no such record.
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
            return {"hardware_id": connector.hardware_id,
                    "accepted_deployment_id": accepted.deployment_id,
                    "accepted_at": _iso(accepted.completed_at),
                    "deployment_id": row.deployment_id, "target_version": row.target_version,
                    "status": row.status, "bytes_written": row.bytes_written}
    return None

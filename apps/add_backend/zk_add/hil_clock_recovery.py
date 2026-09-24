"""Bounded clock recovery for signed requests from the current 2.6.1 HIL target."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.hil_scope import target_matches
from zk_add.models import Connector, DeviceTelemetry
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    _application_sha256,
    _ordered_hil_target,
    _storage_predecessor_exclusion,
    _versions_match,
)
from zk_add.settings import settings
from zk_add.storage_contract import DIRECT_BASELINES, DIRECT_VERSION
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now


RECOVERY_DEPLOYMENT_STATES = {
    "PENDING", "OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT",
    "BOOTED_PENDING", "RECONCILING",
}
MAX_OFFSET = timedelta(hours=6)
MAX_SAMPLE_AGE = timedelta(seconds=90)
MAX_SAMPLE_GAP = timedelta(seconds=120)
MIN_SAMPLE_GAP = timedelta(seconds=10)
MAX_CLOCK_RATE_ERROR = timedelta(seconds=15)
MAX_REQUEST_ERROR = timedelta(seconds=60)


def _sample_time(row: DeviceTelemetry) -> datetime | None:
    value = (row.payload or {}).get("_trusted_envelope_sent_at")
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def trusted_hil_clock_matches(
    session: Session,
    *,
    connector: Connector,
    request_timestamp: datetime,
    now: datetime | None = None,
) -> bool:
    """Accept a live advancing ESP clock only for its exact active HIL campaign.

    The normal token, body hash, HMAC, and nonce checks still run. A stale
    captured request cannot match the latest advancing WebSocket clock.
    """
    if not settings.firmware_hil_enabled or not connector.connected:
        return False
    row = session.execute(
        select(FirmwareDeployment, FirmwareRelease)
        .join(FirmwareCampaign, FirmwareDeployment.campaign_id == FirmwareCampaign.id)
        .join(FirmwareRelease, FirmwareDeployment.release_id == FirmwareRelease.id)
        .where(
            FirmwareDeployment.connector_id == connector.id,
            FirmwareDeployment.status.in_(RECOVERY_DEPLOYMENT_STATES),
            FirmwareCampaign.status == "ACTIVE",
            FirmwareCampaign.zone_id == connector.zone_id,
            FirmwareCampaign.release_id == FirmwareDeployment.release_id,
            FirmwareRelease.version == DIRECT_VERSION,
            FirmwareRelease.state == "HIL_ONLY",
        )
        .order_by(FirmwareDeployment.id.desc()).limit(1)
    ).first()
    if row is None:
        return False
    deployment, release = row
    try:
        target = _ordered_hil_target(session, release)
    except ValueError:
        return False
    if target is None or not target_matches(target, connector):
        return False
    baseline_running = any(
        _versions_match(connector.firmware_version, version) for version in DIRECT_BASELINES
    )
    if baseline_running:
        if _storage_predecessor_exclusion(session, release, connector):
            return False
    elif not (
        _versions_match(connector.firmware_version, DIRECT_VERSION)
        and deployment.status in {"READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"}
        and connector.ota_running_partition in {"ota_0", "ota_1"}
        and connector.ota_image_sha256 == _application_sha256(release)
    ):
        return False

    observed_now = ensure_utc(now or utc_now())
    samples = list(session.scalars(
        select(DeviceTelemetry)
        .where(DeviceTelemetry.connector_id == connector.id)
        .order_by(DeviceTelemetry.id.desc()).limit(24)
    ))
    if not samples:
        return False
    latest = samples[0]
    latest_server = ensure_utc(latest.created_at)
    latest_device = _sample_time(latest)
    if (latest_device is None or latest.boot_id != connector.boot_id or
            not timedelta(0) <= observed_now - latest_server <= MAX_SAMPLE_AGE or
            abs(latest_server - latest_device) > MAX_OFFSET):
        return False
    for earlier in samples[1:]:
        if earlier.boot_id != latest.boot_id:
            continue
        earlier_device = _sample_time(earlier)
        if earlier_device is None:
            continue
        server_gap = latest_server - ensure_utc(earlier.created_at)
        if not MIN_SAMPLE_GAP <= server_gap <= MAX_SAMPLE_GAP:
            continue
        device_gap = latest_device - earlier_device
        if device_gap <= timedelta(0) or abs(device_gap - server_gap) > MAX_CLOCK_RATE_ERROR:
            continue
        expected_device_now = latest_device + (observed_now - latest_server)
        return abs(ensure_utc(request_timestamp) - expected_device_now) <= MAX_REQUEST_ERROR
    return False

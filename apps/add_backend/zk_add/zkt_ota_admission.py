"""Serialize assignment reservations and enforce the approved ZKT fleet limits.

This is an additional negative guard, never qualification or release authority.
The first registered bridge/writer activates these limits for subsequent ZKT
offers, including older releases competing for the same terminals and slots.
"""
from sqlalchemy import and_, or_, select, text

from zk_add.hil_scope import target_matches
from zk_add.models import Connector
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID

JOURNAL_VERSIONS = ("2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.23", "2.7.0")
# Fixed two-key namespace; independent of Python's randomized hash function.
LOCK_NAMESPACE, LOCK_KEY = 0x5A4B54, 270
RESERVATION_SCAN_LIMIT = 128
UNCERTAIN_STATES = ("CANCELLED", "SUPERSEDED", "RELEASE_REVOKED")


def _later_installation(session, deployment, campaign):
    """An authenticated later boot can settle a cancelled offer's uncertainty.

    Never changes either attempt's outcome. Cancellation, elapsed time, a
    version claim, or an unrelated device's success cannot release a slot.
    """
    from zk_add.ota import (FirmwareDeployment, FirmwareEvent, FirmwareRelease,
                           _application_sha256, _versions_match)

    if campaign.status != "CANCELLED" and deployment.status not in UNCERTAIN_STATES:
        return None
    if deployment.offered_at is None or deployment.updated_at is None:
        return None
    observed_after = max(ensure_utc(deployment.offered_at), ensure_utc(deployment.updated_at))
    row = session.execute(select(FirmwareDeployment, FirmwareEvent, FirmwareRelease)
        .join(FirmwareEvent, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(FirmwareRelease, FirmwareRelease.id == FirmwareDeployment.release_id)
        .where(FirmwareDeployment.connector_id == deployment.connector_id,
               FirmwareDeployment.id > deployment.id,
               FirmwareDeployment.status == "SUCCEEDED",
               FirmwareDeployment.offered_at > observed_after,
               FirmwareEvent.state == "SUCCEEDED")
        .order_by(FirmwareEvent.id.desc()).limit(1)).first()
    if row is None:
        return None
    later, event, release = row
    detail = event.details if isinstance(event.details, dict) else {}
    digest = _application_sha256(release)
    if (not digest or later.completed_at is None or later.offered_at is None
            or not ensure_utc(later.offered_at) < ensure_utc(event.created_at) <= utc_now()
            or not ensure_utc(later.offered_at) < ensure_utc(later.completed_at) <= utc_now()
            # The event's default timestamp is assigned during flush, just
            # after the deployment's completion timestamp in record_progress.
            or abs((ensure_utc(event.created_at) - ensure_utc(later.completed_at)).total_seconds()) > 5
            or release.image_size <= 0 or later.bytes_written != release.image_size
            or type(detail.get("bytes_written")) is not int or detail["bytes_written"] != release.image_size
            or detail.get("image_sha256") != digest or detail.get("error_code")
            or detail.get("running_partition") not in {"ota_0", "ota_1"}
            or not _versions_match(later.target_version, release.version)
            or not _versions_match(detail.get("running_version"), release.version)):
        return None
    return {"deployment_id": later.deployment_id, "event_id": event.id,
            "application_sha256": digest, "verified_at": event.created_at.isoformat()}


def reservation_snapshot(session):
    """Bounded read-only explanation of occupied and historically settled slots."""
    from zk_add.ota import ACTIVE_DEPLOYMENT_STATES, FirmwareCampaign, FirmwareDeployment

    rows = session.execute(select(Connector.connector_id, FirmwareDeployment, FirmwareCampaign)
        .join(FirmwareDeployment, FirmwareDeployment.connector_id == Connector.id)
        .join(FirmwareCampaign, FirmwareCampaign.id == FirmwareDeployment.campaign_id)
        .where(Connector.firmware_family == "zkt",
               or_(FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES),
                   and_(FirmwareDeployment.status.in_(UNCERTAIN_STATES),
                        FirmwareDeployment.offered_at.is_not(None))))
        .order_by(FirmwareDeployment.id).limit(RESERVATION_SCAN_LIMIT + 1)).all()
    result = {"scan_complete": len(rows) <= RESERVATION_SCAN_LIMIT, "reservations": []}
    for connector_id, deployment, campaign in rows[:RESERVATION_SCAN_LIMIT]:
        result["reservations"].append({"connector_id": connector_id,
            "deployment_id": deployment.deployment_id, "status": deployment.status,
            "campaign_status": campaign.status,
            "later_installation": _later_installation(session, deployment, campaign)})
    return result


def try_assignment_lock(session, connector):
    if connector.firmware_family != "zkt" or session.get_bind().dialect.name != "postgresql":
        return True
    # Held through the caller's assignment/grant commit, without waiting on a
    # different connector. A missed poll can retry without a partial offer.
    return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                               {"namespace": LOCK_NAMESPACE, "key": LOCK_KEY}))


def pending_offer_hold(session, connector):
    from zk_add.ota import FirmwareRelease

    if connector.firmware_family != "zkt":
        return None
    journal_release = session.scalar(select(FirmwareRelease.id)
        .where(FirmwareRelease.version.in_(JOURNAL_VERSIONS)).limit(1))
    if journal_release is None:
        return None
    target = BY_ID.get(connector.connector_id)
    if target is None or not target_matches(target.identity, connector):
        return "NATIONWIDE_EXACT_TARGET_REQUIRED"
    if not connector.connected:
        return "NATIONWIDE_TARGET_OFFLINE"
    # Paused campaigns, offline connectors and any older ZKT image still occupy
    # their reservation. An administrative cancellation after offering bytes is
    # not evidence that the ESP stopped. Only a later verified installation
    # settles that uncertainty. Multiple attempts on one ESP reserve one slot.
    snapshot = reservation_snapshot(session)
    if not snapshot["scan_complete"]:
        return "NATIONWIDE_RESERVATION_SCAN_LIMIT"
    occupied = {row["connector_id"] for row in snapshot["reservations"] if not row["later_installation"]}
    if len(occupied) >= 2:
        return "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    for connector_id in sorted(occupied):
        other = BY_ID.get(connector_id)
        if other is None:
            return "NATIONWIDE_ACTIVE_LOCATION_UNKNOWN"
        if other.location == target.location:
            return "NATIONWIDE_LOCATION_BUSY"
    return None

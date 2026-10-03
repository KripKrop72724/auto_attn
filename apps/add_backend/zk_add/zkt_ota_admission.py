"""Serialize assignment reservations and enforce the approved ZKT fleet limits.

This is an additional negative guard, never qualification or release authority.
The first registered bridge/writer activates these limits for subsequent ZKT
offers, including older releases competing for the same terminals and slots.
"""
from sqlalchemy import and_, or_, select, text

from zk_add.hil_scope import target_matches
from zk_add.models import Connector
from zk_add.zkt270_scope import BY_ID

JOURNAL_VERSIONS = ("2.6.16", "2.7.0")
# Fixed two-key namespace; independent of Python's randomized hash function.
LOCK_NAMESPACE, LOCK_KEY = 0x5A4B54, 270


def try_assignment_lock(session, connector):
    if connector.firmware_family != "zkt" or session.get_bind().dialect.name != "postgresql":
        return True
    # Held through the caller's assignment/grant commit, without waiting on a
    # different connector. A missed poll can retry without a partial offer.
    return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                               {"namespace": LOCK_NAMESPACE, "key": LOCK_KEY}))


def pending_offer_hold(session, connector):
    from zk_add.ota import ACTIVE_DEPLOYMENT_STATES, FirmwareDeployment, FirmwareRelease

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
    # not evidence that the ESP stopped; retain that uncertainty too.
    occupied = session.execute(select(Connector.connector_id, FirmwareDeployment.id)
        .join(FirmwareDeployment, FirmwareDeployment.connector_id == Connector.id)
        .where(Connector.firmware_family == "zkt",
               or_(FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES),
                   and_(FirmwareDeployment.status.in_(["CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"]),
                        FirmwareDeployment.offered_at.is_not(None))))
        .order_by(FirmwareDeployment.id).limit(3)).all()
    if len(occupied) >= 2:
        return "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    for connector_id, _ in occupied:
        other = BY_ID.get(connector_id)
        if other is None:
            return "NATIONWIDE_ACTIVE_LOCATION_UNKNOWN"
        if other.location == target.location:
            return "NATIONWIDE_LOCATION_BUSY"
    return None

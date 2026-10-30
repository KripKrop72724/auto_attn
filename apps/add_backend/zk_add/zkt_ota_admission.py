"""Serialize ZKT assignment and offer firmware only to the exact fleet targets.

This is an additional negative guard, never qualification or release authority.
The first registered bridge/writer activates it for subsequent ZKT offers. It
no longer limits how many ESPs upgrade at once (owner decision, 23 Oct 2026).
"""
from sqlalchemy import and_, or_, select, text

from zk_add.hil_scope import target_matches
from zk_add.models import Connector
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID, TARGETS

JOURNAL_VERSIONS = ("2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23", "2.7.0")
# Fixed two-key namespace; independent of Python's randomized hash function.
LOCK_NAMESPACE, LOCK_KEY = 0x5A4B54, 270
RESERVATION_SCAN_LIMIT = 128
# Terminal models the 17 were qualified on. A ZKT added later may take a
# production image only on one of these; the K50 and SenseFace are not.
QUALIFIED_MODELS = frozenset(target.model for target in TARGETS)
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


def admitted_addition(connector, release) -> bool:
    """A ZKT ESP onboarded after the 17 may take a production (AVAILABLE) image.

    Owner decision (29 Oct 2026). The 17 stay the exact scope of every
    HIL_ONLY image; a later ESP reaches only AVAILABLE ones, and only with an
    administrator-confirmed terminal binding on a qualified model. A connector
    of the 17 whose identity changed is not an addition. ADD's predecessor and
    storage checks still decide whether its running image may take the release.
    """
    from zk_add.zkt274_bld9_k50 import TARGET as K50

    terminal = connector.zkt_device
    return bool(
        release is not None and release.state == "AVAILABLE"
        and connector.connector_id not in BY_ID and connector.connector_id != K50.connector_id
        and connector.active and not connector.is_spare
        and terminal is not None and terminal.terminal_binding_state == "CONFIRMED"
        and terminal.confirmed_serial and terminal.serial == terminal.confirmed_serial
        and terminal.model in QUALIFIED_MODELS
    )


def terminal_unconfirmed(terminal) -> bool:
    """No terminal both confirmed and seen: never identified, not yet confirmed,
    confirmed to a serial the ESP does not report, or pinned at onboarding from
    the label (serial too) while the ESP never reported its model or platform."""
    return (terminal is None or terminal.terminal_binding_state != "CONFIRMED"
            or not terminal.confirmed_serial or terminal.serial != terminal.confirmed_serial
            or (terminal.serial_confirmed_by == "AUTO_ONBOARD_EXPECTED_SERIAL"
                and not terminal.model and not terminal.platform))


def unconfirmed_terminal_offer(connector, release) -> bool:
    """A production image never waits on terminal verification.

    Owner decision (30 Oct 2026): an ESP whose terminal is not confirmed may
    take any AVAILABLE image, so firmware reaches ESPs that cannot log in to
    their terminal yet (a SenseFace behind 2.7.x answers CONNECT with 6001;
    2.8.0 is the image that logs in). The ESP itself stays exact: one of the 17
    on its own connector and MAC, or a ZKT ESP added later. HIL_ONLY images
    keep the 17 with their confirmed terminals, a terminal confirmed by an
    administrator still needs a qualified model (``admitted_addition``), and
    the K50 keeps its reviewed path. ADD's predecessor and storage checks still
    decide whether the running image may take the release.
    """
    from zk_add.zkt274_bld9_k50 import TARGET as K50

    target = BY_ID.get(connector.connector_id)
    return bool(
        release is not None and release.state == "AVAILABLE"
        and connector.connector_id != K50.connector_id
        and connector.active and not connector.is_spare
        and (target is None or (connector.hardware_id or "").lower() == target.identity.mac)
        and terminal_unconfirmed(connector.zkt_device)
    )


def pending_offer_hold(session, connector, release=None):
    """Offer only to an exact, online fleet target; never limit how many upgrade at once.

    The owner removed the nationwide concurrency limits on 23 Oct 2026 (two
    upgrading ESPs at most, one per physical location), so every campaign's
    devices upgrade in parallel. ``reservation_snapshot`` still explains which
    ESPs hold an unsettled offer, but no longer blocks another offer.
    """
    from zk_add.ota import FirmwareRelease
    from zk_add.zkt274_bld9_k50 import offer_location

    if connector.firmware_family != "zkt":
        return None
    journal_release = session.scalar(select(FirmwareRelease.id)
        .where(FirmwareRelease.version.in_(JOURNAL_VERSIONS)).limit(1))
    if journal_release is None:
        return None
    target = BY_ID.get(connector.connector_id)
    # Outside the exact 17: the one reviewed K50 with its exact signed 2.4.12
    # bridge or 2.7.4, later qualified additions for production images, and
    # any exact ESP whose terminal is not confirmed for production images.
    if (not (target is not None and target_matches(target.identity, connector))
            and offer_location(release, connector) is None and not admitted_addition(connector, release)
            and not unconfirmed_terminal_offer(connector, release)):
        return "NATIONWIDE_EXACT_TARGET_REQUIRED"
    if not connector.connected:
        return "NATIONWIDE_TARGET_OFFLINE"
    return None

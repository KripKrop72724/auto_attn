"""Ledger classification for punches without a saved or unique synced CNIC."""

from sqlalchemy import and_, func, or_, select

from zk_add.models import AttendanceEvent, DeviceUser


# Identity holds a one-click release may lift. QUARANTINED_IDENTITY_REUSE is
# a quarantine (the user ID changed hands), not a hold, so it stays out.
UNKNOWN_SYNCED_HELD_STATUSES = ("BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT")


def _linkage():
    """(no CNIC saved with the punch, active user count, that user has a CNIC)."""
    matching_user = (
        DeviceUser.zkt_device_id == AttendanceEvent.zkt_device_id,
        DeviceUser.user_id == AttendanceEvent.user_id,
        DeviceUser.present.is_(True),
        DeviceUser.lifecycle_state == "ACTIVE",
    )
    user_count = (
        select(func.count(DeviceUser.id))
        .where(*matching_user)
        .correlate(AttendanceEvent)
        .scalar_subquery()
    )
    synced_cnic = (
        select(DeviceUser.id)
        .where(
            *matching_user,
            DeviceUser.cnic_lookup_hash.is_not(None),
            DeviceUser.cnic_lookup_hash != "",
            DeviceUser.cnic_encrypted.is_not(None),
            DeviceUser.cnic_encrypted != "",
        )
        .correlate(AttendanceEvent)
        .exists()
    )
    saved_cnic_missing = or_(
        AttendanceEvent.cnic_lookup_hash.is_(None),
        AttendanceEvent.cnic_lookup_hash == "",
        AttendanceEvent.cnic_encrypted.is_(None),
        AttendanceEvent.cnic_encrypted == "",
    )
    return saved_cnic_missing, user_count, synced_cnic


def cnic_not_linked_expression():
    """Classify before pagination using the same unique active-user lookup as delivery hints.

    This describes available CNIC linkage, not proof authorizing Oracle delivery.
    A synced CNIC may still need historical identity evidence before release.
    """
    saved_cnic_missing, user_count, synced_cnic = _linkage()
    return and_(saved_cnic_missing, or_(user_count != 1, ~synced_cnic))


def unknown_synced_cnic_expression():
    """Held punches the ledger shows as "Unknown identity · CNIC on synced user".

    No name or CNIC was saved with the punch, Oracle has not confirmed it, and
    exactly one active terminal user holds this user ID with a CNIC. Whether
    that CNIC may be sent is still decided per punch by the direct send checks.
    """
    saved_cnic_missing, user_count, synced_cnic = _linkage()
    return and_(
        AttendanceEvent.ords_status.in_(UNKNOWN_SYNCED_HELD_STATUSES),
        AttendanceEvent.oracle_confirmed_at.is_(None),
        or_(AttendanceEvent.display_name.is_(None), AttendanceEvent.display_name == ""),
        saved_cnic_missing,
        user_count == 1,
        synced_cnic,
    )

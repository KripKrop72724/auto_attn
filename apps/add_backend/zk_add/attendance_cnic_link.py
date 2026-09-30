"""Ledger classification for punches without a saved or unique synced CNIC."""

from sqlalchemy import and_, func, or_, select

from zk_add.models import AttendanceEvent, DeviceUser


def cnic_not_linked_expression():
    """Classify before pagination using the same unique active-user lookup as delivery hints.

    This describes available CNIC linkage, not proof authorizing Oracle delivery.
    A synced CNIC may still need historical identity evidence before release.
    """
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
    return and_(
        or_(
            AttendanceEvent.cnic_lookup_hash.is_(None),
            AttendanceEvent.cnic_lookup_hash == "",
            AttendanceEvent.cnic_encrypted.is_(None),
            AttendanceEvent.cnic_encrypted == "",
        ),
        or_(user_count != 1, ~synced_cnic),
    )

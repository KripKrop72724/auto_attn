"""Read-side helpers for the People pages: sorting, quick-view rules, totals and search.

Nothing here writes. CNICs and their lookup hashes are used only to match and
are never returned.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from zk_add import device_names
from zk_add.models import (
    Connector,
    DeviceCommand,
    DeviceUser,
    DeviceUserSnapshot,
    TemporaryAdminLease,
    ZKTDevice,
)
from zk_add.time_utils import ensure_utc, utc_now

ADMIN_PRIVILEGE = 14
ACTIVE_LEASE_STATES = ("GRANTING", "ACTIVE", "REVOKING", "OVERDUE")
ADDED_RECENTLY_DAYS = 7
# A machine's first list read: everyone on it is "new" to the dashboard then,
# so only people who appeared a day after it count as added.
FIRST_READ_SETTLE = timedelta(hours=24)
SORTS = ("problems", "name", "employee_id", "newest")
SEARCH_LIMIT = 20
_PRINTABLE_ID = re.compile(r"[\x21-\x7e]+")


def looks_damaged(user: DeviceUser, *, hikvision: bool) -> bool:
    """A ZKTeco entry that looks corrupted on the machine.

    User ID longer than 9 characters or with unprintable bytes, or a name that
    is just the user ID or "tt" (FSDMAIN 7 Nov, MB460 3 Nov). Removing such an
    entry from the dashboard can erase every fingerprint on the machine.
    """
    if hikvision:
        return False
    user_id = (user.user_id or "").strip()
    name = (user.display_name or "").strip()
    if len(user_id) > 9 or (user_id and not _PRINTABLE_ID.fullmatch(user_id)):
        return True
    return bool(name) and (name == user_id or name.casefold() == "tt")


def first_list_read_at(db: Session, zkt: ZKTDevice) -> datetime | None:
    value = db.scalar(
        select(func.min(DeviceUserSnapshot.received_at)).where(DeviceUserSnapshot.zkt_device_id == zkt.id)
    )
    return ensure_utc(value) if value is not None else None


def added_since(first_read: datetime | None, days: int, now: datetime | None = None):
    """A predicate: was this user added to the machine in the last ``days``?"""
    if first_read is None:
        return lambda _user: False
    window = (now or utc_now()) - timedelta(days=days)
    settled = first_read + FIRST_READ_SETTLE

    def added(user: DeviceUser) -> bool:
        if user.created_at is None:
            return False
        created = ensure_utc(user.created_at)
        return created >= window and created > settled

    return added


def command_ids(db: Session, users: Iterable[DeviceUser]) -> dict[int, str]:
    """Public command ids of the users' running changes, in one query."""
    ids = {user.current_command_id for user in users if user.current_command_id}
    if not ids:
        return {}
    return dict(db.execute(select(DeviceCommand.id, DeviceCommand.command_id).where(DeviceCommand.id.in_(ids))).all())


def _natural(value: str | None) -> list:
    return device_names.natural(value or "")


def sort_users(rows: list[DeviceUser], sort: str, *, needs_fixing) -> list[DeviceUser]:
    if sort == "name":
        return sorted(rows, key=lambda row: ((row.display_name or "").casefold(), _natural(row.user_id), row.id))
    if sort == "employee_id":
        return sorted(rows, key=lambda row: (_natural(row.user_id), row.id))
    if sort == "newest":
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(rows, key=lambda row: (ensure_utc(row.created_at) if row.created_at else oldest, row.id),
                      reverse=True)
    return sorted(rows, key=lambda row: (0 if needs_fixing(row) else 1, (row.display_name or "").casefold(),
                                         _natural(row.user_id), row.id))


def active_users(zkt_ids: Iterable[int] | None = None):
    statement = select(DeviceUser).where(DeviceUser.lifecycle_state == "ACTIVE", DeviceUser.present.is_(True))
    if zkt_ids is not None:
        statement = statement.where(DeviceUser.zkt_device_id.in_(list(zkt_ids)))
    return statement


def lease_users(db: Session, leases: Iterable[TemporaryAdminLease]) -> dict[int, DeviceUser]:
    ids = {lease.device_user_id for lease in leases if lease is not None}
    if not ids:
        return {}
    return {user.id: user for user in db.scalars(select(DeviceUser).where(DeviceUser.id.in_(ids)))}


def people_summary(db: Session, integrity_for) -> dict:
    """Per device: totals for the People picker. Never contains a CNIC or a hash.

    ``integrity_for(db, zkt)`` is web.device_user_identity_integrity, so the
    numbers equal what each device's People list shows.
    """
    connectors = db.scalars(
        select(Connector).options(selectinload(Connector.zkt_device)).order_by(Connector.id)
    ).all()
    zkts = {connector.zkt_device.id: connector for connector in connectors if connector.zkt_device is not None}
    leases = db.scalars(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.zkt_device_id.in_(list(zkts) or [-1]),
            TemporaryAdminLease.state.in_(ACTIVE_LEASE_STATES),
        ).order_by(TemporaryAdminLease.requested_at.desc())
    ).all()
    latest_lease: dict[int, TemporaryAdminLease] = {}
    for lease in leases:
        latest_lease.setdefault(lease.zkt_device_id, lease)
    holders = lease_users(db, latest_lease.values())
    admins = dict(db.execute(
        select(DeviceUser.zkt_device_id, func.count(DeviceUser.id)).where(
            DeviceUser.zkt_device_id.in_(list(zkts) or [-1]),
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.present.is_(True),
            DeviceUser.privilege == ADMIN_PRIVILEGE,
        ).group_by(DeviceUser.zkt_device_id)
    ).all())
    rows = []
    for zkt_id, connector in zkts.items():
        zkt = connector.zkt_device
        integrity, _members, _resolutions = integrity_for(db, zkt=zkt)
        lease = latest_lease.get(zkt_id)
        holder = holders.get(lease.device_user_id) if lease is not None else None
        rows.append({
            "connector_id": connector.connector_id,
            "people": integrity["total_users"],
            "with_cnic": integrity["with_cnic"],
            "needs_fixing": integrity["missing_cnic"] + integrity["unresolved_duplicate_users"],
            "no_cnic": integrity["missing_cnic"] - integrity["cnic_format_suspect_users"],
            "same_cnic_people": integrity["unresolved_duplicate_users"],
            "cnic_typed_wrongly": integrity["cnic_format_suspect_users"],
            "admins": int(admins.get(zkt_id, 0)),
            "list_read_at": zkt.identity_snapshot_received_at,
            "admin_access": None if lease is None else {
                "user_id": holder.user_id if holder else None,
                "user_display_name": holder.display_name if holder else None,
                "expires_at": lease.expires_at,
            },
        })
    return {"rows": rows, "computed_at": utc_now()}


def search_people(db: Session, *, q: str | None, cnic_hash: str | None) -> list[dict]:
    """At most 20 people across every device by name, Employee ID or CNIC (hash)."""
    statement = active_users().join(ZKTDevice, ZKTDevice.id == DeviceUser.zkt_device_id).join(
        Connector, Connector.id == ZKTDevice.connector_id)
    if cnic_hash is not None:
        statement = statement.where(DeviceUser.cnic_lookup_hash == cnic_hash)
    else:
        text = (q or "").strip()
        like = f"%{text}%"
        statement = statement.where(or_(DeviceUser.display_name.ilike(like), DeviceUser.user_id.ilike(like)))
    users = db.scalars(statement.order_by(Connector.is_spare.asc(), DeviceUser.id.asc()).limit(200)).all()
    zkt_ids = {user.zkt_device_id for user in users}
    connectors = {
        zkt.id: zkt.connector for zkt in db.scalars(
            select(ZKTDevice).where(ZKTDevice.id.in_(list(zkt_ids) or [-1])).options(selectinload(ZKTDevice.connector))
        )
    }
    text = (q or "").strip().casefold()

    def rank(user: DeviceUser) -> tuple:
        exact = (user.user_id or "").casefold() == text or (user.display_name or "").casefold() == text
        connector = connectors[user.zkt_device_id]
        return (0 if exact else 1, bool(connector.is_spare), (user.display_name or "").casefold(),
                _natural(user.user_id), user.id)

    from zk_add.identity import suspect_cnic_name
    from zk_add.crypto import decrypt_text

    result = []
    for user in sorted(users, key=rank)[:SEARCH_LIMIT]:
        connector = connectors[user.zkt_device_id]
        naming = device_names.naming_for(connector)
        needs = []
        if not user.cnic_lookup_hash:
            needs.append("cnic_typed_wrongly" if suspect_cnic_name(decrypt_text(user.machine_name_encrypted))
                         else "no_cnic")
        elif user.identity_conflict_code is not None:
            needs.append("same_cnic")
        result.append({
            "connector_id": connector.connector_id,
            "friendly_name": naming["friendly_name"],
            "division": naming["division"],
            "is_spare": bool(connector.is_spare),
            "user_id": user.user_id,
            "display_name": user.display_name,
            "needs": needs,
        })
    return result

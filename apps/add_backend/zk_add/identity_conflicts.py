from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_cnic, mask_cnic
from zk_add.models import (
    AttendanceEvent,
    DeviceUser,
    IdentityConflictResolution,
    ZKTDevice,
)
from zk_add.time_utils import utc_now


RESOLUTION_SAME_EMPLOYEE = "SAME_EMPLOYEE_MULTIPLE_TERMINAL_RECORDS"


def _member_ids(rows: list[DeviceUser]) -> list[int]:
    return sorted(row.id for row in rows if row.id is not None)


def identity_group_token(
    zkt_device_id: int, cnic_lookup_hash: str, rows: list[DeviceUser]
) -> str:
    """Return a non-PII token bound to the exact current terminal membership."""

    members = ";".join(
        f"{row.id}:{row.uid}:{row.user_id}"
        for row in sorted(rows, key=lambda candidate: candidate.id or 0)
    )
    material = f"{zkt_device_id}:{cnic_lookup_hash}:{members}"
    return hashlib.sha256(material.encode()).hexdigest()


def active_identity_groups(session: Session, *, zkt: ZKTDevice) -> dict[str, list[DeviceUser]]:
    rows = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.lifecycle_state == "ACTIVE",
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.cnic_lookup_hash != None,  # noqa: E711
            )
        ).all()
    )
    groups: dict[str, list[DeviceUser]] = {}
    for row in rows:
        if row.cnic_lookup_hash:
            groups.setdefault(row.cnic_lookup_hash, []).append(row)
    return groups


def valid_identity_resolutions(
    session: Session,
    *,
    zkt: ZKTDevice,
    groups: dict[str, list[DeviceUser]] | None = None,
    mark_stale: bool = False,
) -> dict[str, IdentityConflictResolution]:
    """Return approvals whose exact CNIC group is still unchanged.

    A later ZKT snapshot can add, remove, or replace a terminal record.  Such a
    change invalidates the approval without deleting it, providing a complete
    audit trail and defaulting new punches back to quarantine.
    """

    groups = groups if groups is not None else active_identity_groups(session, zkt=zkt)
    resolutions = list(
        session.scalars(
            select(IdentityConflictResolution).where(
                IdentityConflictResolution.zkt_device_id == zkt.id,
                IdentityConflictResolution.status == "ACTIVE",
            )
        ).all()
    )
    valid: dict[str, IdentityConflictResolution] = {}
    now = utc_now()
    for resolution in resolutions:
        members = groups.get(resolution.cnic_lookup_hash, [])
        token = (
            identity_group_token(zkt.id, resolution.cnic_lookup_hash, members)
            if len(members) > 1
            else None
        )
        member_ids = _member_ids(members)
        is_valid = (
            token == resolution.group_token
            and member_ids == sorted(resolution.member_device_user_ids or [])
        )
        if is_valid:
            valid[resolution.cnic_lookup_hash] = resolution
            if mark_stale:
                resolution.last_validated_at = now
                resolution.updated_at = now
            continue
        if mark_stale:
            resolution.status = "STALE"
            resolution.last_validated_at = now
            resolution.updated_at = now
            append_audit(
                session,
                actor="device:snapshot-validation",
                action="IDENTITY_CONFLICT_RESOLUTION_STALE",
                target_type="identity_conflict_resolution",
                target_id=resolution.resolution_id,
                outcome="SUCCESS",
                before={
                    "status": "ACTIVE",
                    "member_device_user_ids": resolution.member_device_user_ids,
                },
                after={"status": "STALE", "current_member_device_user_ids": member_ids},
            )
    return valid


def valid_resolution_for_user(
    session: Session, *, zkt: ZKTDevice, user: DeviceUser
) -> IdentityConflictResolution | None:
    if not user.cnic_lookup_hash:
        return None
    group = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.lifecycle_state == "ACTIVE",
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.cnic_lookup_hash == user.cnic_lookup_hash,
            )
        ).all()
    )
    if len(group) < 2:
        return None
    resolution = session.scalar(
        select(IdentityConflictResolution).where(
            IdentityConflictResolution.zkt_device_id == zkt.id,
            IdentityConflictResolution.cnic_lookup_hash == user.cnic_lookup_hash,
            IdentityConflictResolution.status == "ACTIVE",
        )
    )
    if resolution is None:
        return None
    if resolution.group_token != identity_group_token(zkt.id, user.cnic_lookup_hash, group):
        return None
    if sorted(resolution.member_device_user_ids or []) != _member_ids(group):
        return None
    return resolution


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _redact_reason(value: str) -> str:
    return re.sub(r"\b\d{5}-?\d{7}-?\d\b", "[CNIC-REDACTED]", value.strip())


def classify_identity_group(rows: list[DeviceUser]) -> str:
    names = [_normalized_name(row.display_name) for row in rows]
    if names and len(set(names)) == 1:
        return "EXACT_NAME_MATCH"
    ratios = [
        SequenceMatcher(None, left, right).ratio()
        for index, left in enumerate(names)
        for right in names[index + 1 :]
    ]
    if ratios and min(ratios) < 0.4:
        return "MIXED_NAMES_HIGH_RISK"
    return "POSSIBLE_NAME_VARIANT"


def build_identity_conflict_report(session: Session, *, zkt: ZKTDevice) -> dict:
    groups = {
        lookup: rows
        for lookup, rows in active_identity_groups(session, zkt=zkt).items()
        if len(rows) > 1
    }
    resolutions = valid_identity_resolutions(session, zkt=zkt, groups=groups)
    add_attendance_count = int(
        session.scalar(
            select(func.count(AttendanceEvent.id)).where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.ords_status != "QUARANTINED_INVALID_EVENT_UID",
            )
        )
        or 0
    )
    terminal_attendance_count = int(zkt.attendance_count or 0)
    coverage_percent = (
        round(min(100.0, add_attendance_count * 100.0 / terminal_attendance_count), 2)
        if terminal_attendance_count
        else None
    )

    user_ids = [row.user_id for rows in groups.values() for row in rows]
    punch_stats: dict[str, dict] = {}
    if user_ids:
        stats = session.execute(
            select(
                AttendanceEvent.user_id,
                func.count(AttendanceEvent.id),
                func.min(AttendanceEvent.device_event_time),
                func.max(AttendanceEvent.device_event_time),
                func.sum(
                    case((AttendanceEvent.ords_status == "BLOCKED_IDENTITY", 1), else_=0)
                ),
            )
            .where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.user_id.in_(user_ids),
                AttendanceEvent.ords_status != "QUARANTINED_INVALID_EVENT_UID",
            )
            .group_by(AttendanceEvent.user_id)
        ).all()
        for user_id, count, first, last, blocked in stats:
            punch_stats[str(user_id)] = {
                "captured_count": int(count or 0),
                "first_captured_at": first,
                "last_captured_at": last,
                "blocked_identity_count": int(blocked or 0),
            }

    report_groups = []
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: min(
            (int(row.user_id) if row.user_id.isdigit() else 2**31 for row in item[1]),
            default=2**31,
        ),
    )
    for lookup, rows in ordered_groups:
        ordered = sorted(
            rows,
            key=lambda row: (int(row.user_id) if row.user_id.isdigit() else 2**31, row.user_id),
        )
        resolution = resolutions.get(lookup)
        classification = classify_identity_group(ordered)
        cnic = decrypt_cnic(ordered[0].cnic_encrypted)
        report_groups.append(
            {
                "group_token": identity_group_token(zkt.id, lookup, ordered),
                "cnic_masked": mask_cnic(cnic),
                "classification": classification,
                "status": "RESOLVED_SAME_EMPLOYEE" if resolution else "UNRESOLVED",
                "resolution_id": resolution.resolution_id if resolution else None,
                "resolution_created_at": resolution.created_at if resolution else None,
                "resolution_reason": resolution.reason if resolution else None,
                "recommended_action": (
                    "CONFIRM_SAME_EMPLOYEE"
                    if classification == "EXACT_NAME_MATCH"
                    else "HR_IDENTITY_REVIEW"
                ),
                "members": [
                    {
                        "user_key": row.user_key,
                        "uid": row.uid,
                        "user_id": row.user_id,
                        "display_name": row.display_name,
                        "row_version": row.row_version,
                        "privilege": row.privilege,
                        "observed_at": row.observed_at,
                        "punch_evidence": punch_stats.get(
                            row.user_id,
                            {
                                "captured_count": 0,
                                "first_captured_at": None,
                                "last_captured_at": None,
                                "blocked_identity_count": 0,
                            },
                        ),
                    }
                    for row in ordered
                ],
            }
        )
    return {
        "evidence_scope": {
            "snapshot_source": (
                "CURRENT_COMPLETE_ZKT_SNAPSHOT"
                if zkt.snapshot_complete
                else "PARTIAL_ZKT_SNAPSHOT"
            ),
            "terminal_attendance_count": terminal_attendance_count,
            "add_attendance_count": add_attendance_count,
            "attendance_coverage_percent": coverage_percent,
            "attendance_is_immutable": True,
            "terminal_users_are_unchanged": True,
        },
        "raw_duplicate_groups": len(report_groups),
        "resolved_groups": sum(group["status"] == "RESOLVED_SAME_EMPLOYEE" for group in report_groups),
        "unresolved_groups": sum(group["status"] == "UNRESOLVED" for group in report_groups),
        "groups": report_groups,
    }


def create_same_employee_resolution(
    session: Session,
    *,
    zkt: ZKTDevice,
    group_token: str,
    members: list[tuple[str, int]],
    reason: str,
    idempotency_key: str,
    actor: str,
    ip_address: str | None = None,
) -> IdentityConflictResolution:
    existing = session.scalar(
        select(IdentityConflictResolution).where(
            IdentityConflictResolution.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if existing.zkt_device_id != zkt.id or existing.group_token != group_token:
            raise ValueError("Idempotency key was already used for a different resolution.")
        return existing
    if not zkt.snapshot_complete:
        raise ValueError("A current complete ZKT user snapshot is required.")

    duplicate_groups = {
        lookup: rows
        for lookup, rows in active_identity_groups(session, zkt=zkt).items()
        if len(rows) > 1
    }
    match: tuple[str, list[DeviceUser]] | None = None
    for lookup, rows in duplicate_groups.items():
        if identity_group_token(zkt.id, lookup, rows) == group_token:
            match = (lookup, rows)
            break
    if match is None:
        raise ValueError("The conflict group changed; refresh the terminal snapshot and review it again.")
    lookup, rows = match
    expected_versions = dict(members)
    if len(expected_versions) != len(members):
        raise ValueError("Each conflict member must be confirmed exactly once.")
    if set(expected_versions) != {row.user_key for row in rows}:
        raise ValueError("Every current conflict member must be included; partial resolution is forbidden.")
    stale = [
        row.user_id
        for row in rows
        if expected_versions.get(row.user_key) != row.row_version
    ]
    if stale:
        raise ValueError("The terminal records changed; refresh and review the group again.")
    active = session.scalar(
        select(IdentityConflictResolution).where(
            IdentityConflictResolution.zkt_device_id == zkt.id,
            IdentityConflictResolution.cnic_lookup_hash == lookup,
            IdentityConflictResolution.status == "ACTIVE",
        )
    )
    if active is not None:
        raise ValueError("This exact-CNIC group already has an active resolution.")

    now = utc_now()
    classification = classify_identity_group(rows)
    resolution = IdentityConflictResolution(
        zkt_device_id=zkt.id,
        cnic_lookup_hash=lookup,
        group_token=group_token,
        member_device_user_ids=_member_ids(rows),
        resolution_type=RESOLUTION_SAME_EMPLOYEE,
        classification=classification,
        status="ACTIVE",
        reason=_redact_reason(reason),
        idempotency_key=idempotency_key,
        created_by=actor,
        created_at=now,
        updated_at=now,
        last_validated_at=now,
    )
    session.add(resolution)
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="IDENTITY_CONFLICT_RESOLVE_SAME_EMPLOYEE",
        target_type="identity_conflict_resolution",
        target_id=resolution.resolution_id,
        outcome="SUCCESS",
        ip_address=ip_address,
        before={
            "status": "UNRESOLVED",
            "member_device_user_ids": _member_ids(rows),
            "attendance_rows_changed": 0,
            "terminal_users_changed": 0,
        },
        after={
            "status": "ACTIVE",
            "resolution_type": RESOLUTION_SAME_EMPLOYEE,
            "classification": classification,
            "attendance_rows_changed": 0,
            "terminal_users_changed": 0,
        },
    )
    return resolution


def revoke_identity_resolution(
    session: Session,
    *,
    zkt: ZKTDevice,
    resolution_id: str,
    reason: str,
    actor: str,
    ip_address: str | None = None,
) -> IdentityConflictResolution:
    resolution = session.scalar(
        select(IdentityConflictResolution).where(
            IdentityConflictResolution.zkt_device_id == zkt.id,
            IdentityConflictResolution.resolution_id == resolution_id,
        )
    )
    if resolution is None:
        raise ValueError("Identity resolution was not found for this terminal.")
    if resolution.status != "ACTIVE":
        raise ValueError(f"Identity resolution is already {resolution.status.lower()}.")
    now = utc_now()
    resolution.status = "REVOKED"
    resolution.reason = f"{resolution.reason}\nRevoked: {_redact_reason(reason)}"
    resolution.revoked_by = actor
    resolution.revoked_at = now
    resolution.updated_at = now
    append_audit(
        session,
        actor=actor,
        action="IDENTITY_CONFLICT_RESOLUTION_REVOKE",
        target_type="identity_conflict_resolution",
        target_id=resolution.resolution_id,
        outcome="SUCCESS",
        ip_address=ip_address,
        before={"status": "ACTIVE", "attendance_rows_changed": 0, "terminal_users_changed": 0},
        after={"status": "REVOKED", "attendance_rows_changed": 0, "terminal_users_changed": 0},
    )
    return resolution

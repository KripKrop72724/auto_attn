"""Owner-approved release of a zone's older held legacy dump punches.

A legacy reconnect or startup dump row reaches ADD before the next roster
snapshot, so ingest holds it. The automatic synced-CNIC release admits such a
row on its authenticated batch proof only while the hold is recent. Older
holds are historical backlog. This module previews them per zone without
writing anything and, after the owner's password step-up and typed
confirmation, releases them in capped, audited batches with exactly the same
proof and database guard. A row whose proof fails stays held.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.attendance_identity_evidence import DUMP_RECONCILE_SOURCES
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.audit import append_audit
from zk_add.db import session_scope
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryJob as Job,
    Connector,
    DeviceUser,
)
from zk_add.service import (
    DumpIdentityRelease,
    release_synced_cnic_attendance_event,
    synced_cnic_release_proof,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

ACTION = "IDENTITY_BACKLOG_RELEASE"
POLICY = "identity-dump-backlog-v1"
# Deliberately not QUEUED/RUNNING: the generic recovery advancer owns those.
RELEASING = "RELEASING"
HELD = ("BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT")
DUMP_SOURCES = tuple(sorted(DUMP_RECONCILE_SOURCES))
AGE_BUCKETS = ("AUTOMATIC_WINDOW", "UP_TO_7_DAYS", "UP_TO_30_DAYS", "OLDER")
PREVIEW_PAGE_MAX = 1000


def confirmation_phrase(zone_id: str) -> str:
    return f"RELEASE {zone_id}"


def zone_connectors(session: Session, zone_id: str, *, lock: bool = False) -> list[Connector]:
    query = (
        select(Connector)
        .where(Connector.zone_id == zone_id, Connector.firmware_family != "hikvision")
        .order_by(Connector.id)
    )
    if lock:
        query = query.with_for_update()
    return [row for row in session.scalars(query).all() if row.zkt_device is not None]


def _held_dump_rows(connector_ids: list[int]):
    return (
        AttendanceEvent.connector_id.in_(connector_ids),
        AttendanceEvent.ords_status.in_(HELD),
        AttendanceEvent.source.in_(DUMP_SOURCES),
    )


def _window() -> timedelta:
    return timedelta(hours=settings.attendance_dump_identity_auto_release_hours)


def _bucket(received_at: datetime | None, now: datetime) -> str:
    age = now - ensure_utc(received_at) if received_at else timedelta.max
    window = _window()
    if window and age <= window:
        return "AUTOMATIC_WINDOW"
    if age <= timedelta(days=7):
        return "UP_TO_7_DAYS"
    if age <= timedelta(days=30):
        return "UP_TO_30_DAYS"
    return "OLDER"


def _bucket_expression(now: datetime):
    window = _window()
    whens = []
    if window:
        whens.append((AttendanceEvent.received_at >= now - window, "AUTOMATIC_WINDOW"))
    whens.extend([
        (AttendanceEvent.received_at >= now - timedelta(days=7), "UP_TO_7_DAYS"),
        (AttendanceEvent.received_at >= now - timedelta(days=30), "UP_TO_30_DAYS"),
    ])
    return case(*whens, else_="OLDER")


def _unique_user(session: Session, cache: dict, zkt_id: int, user_id: str) -> DeviceUser | None:
    key = (zkt_id, user_id)
    if key not in cache:
        users = session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt_id,
                DeviceUser.user_id == user_id,
                DeviceUser.lifecycle_state == "ACTIVE",
            ).limit(2)
        ).all()
        cache[key] = users[0] if len(users) == 1 else None
    return cache[key]


def _empty_groups() -> dict[tuple[str, str], dict[str, int]]:
    return {
        (source, bucket): {"held": 0, "provable": 0}
        for source in DUMP_SOURCES
        for bucket in AGE_BUCKETS
    }


def _group_rows(groups: dict) -> list[dict]:
    return [
        {"source": source, "age": bucket, **counts}
        for (source, bucket), counts in groups.items()
    ]


def preview_page(
    session: Session, *, zone_id: str, after_id: int = 0, limit: int = 500,
    now: datetime | None = None,
) -> dict:
    """Read-only: how many of a zone's held dump rows the proof would release.

    Evaluates one bounded page in event-id order. The caller sums pages until
    next_after_id is null. The first page also reports the zone's totals.
    """
    now = ensure_utc(now or utc_now())
    connectors = zone_connectors(session, zone_id)
    if not connectors:
        raise RecoveryError("This zone has no ZKT terminal in ADD.", "ZONE_NOT_FOUND")
    by_id = {row.id: row for row in connectors}
    bounded = max(1, min(int(limit), PREVIEW_PAGE_MAX))
    rows = session.scalars(
        select(AttendanceEvent)
        .where(*_held_dump_rows(list(by_id)), AttendanceEvent.id > after_id)
        .order_by(AttendanceEvent.id)
        .limit(bounded)
    ).all()
    groups = _empty_groups()
    users: dict = {}
    for row in rows:
        zkt = by_id[row.connector_id].zkt_device
        entry = groups[(row.source, _bucket(row.received_at, now))]
        entry["held"] += 1
        user = _unique_user(session, users, zkt.id, row.user_id)
        if user is not None and synced_cnic_release_proof(
            session, zkt=zkt, user=user, row=row,
            dump_release=DumpIdentityRelease(held_since=None, remaining=1),
        ):
            entry["provable"] += 1
    result = {
        "zone_id": zone_id,
        "after_id": after_id,
        "scanned": len(rows),
        "next_after_id": rows[-1].id if len(rows) == bounded else None,
        "groups": _group_rows(groups),
        "window_hours": settings.attendance_dump_identity_auto_release_hours,
        "devices": [
            {"connector_id": row.connector_id, "name": row.display_name} for row in connectors
        ],
    }
    if after_id == 0:
        bucket = _bucket_expression(now)
        totals = _empty_groups()
        for source, age, count in session.execute(
            select(AttendanceEvent.source, bucket, func.count(AttendanceEvent.id))
            .where(*_held_dump_rows(list(by_id)))
            .group_by(AttendanceEvent.source, bucket)
        ).all():
            totals[(source, age)]["held"] = count
        result["held_total"] = sum(entry["held"] for entry in totals.values())
        result["totals"] = [
            {"source": source, "age": age, "held": counts["held"]}
            for (source, age), counts in totals.items()
        ]
    return result


def create_release(
    session: Session, *, actor: str, zone_id: str, reason: str, typed_confirmation: str,
    idempotency_key: str, ip_address: str | None = None,
) -> Job:
    request = {"zone_id": zone_id, "policy": POLICY}
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is not None:
        if (
            existing.action != ACTION or existing.actor != actor
            or existing.scope.get("request") != request
        ):
            raise RecoveryError(
                "This request key belongs to a different request.", "IDEMPOTENCY_CONFLICT"
            )
        return existing
    if typed_confirmation.strip() != confirmation_phrase(zone_id):
        raise RecoveryError(
            f"Type {confirmation_phrase(zone_id)} to confirm.", "CONFIRMATION_MISMATCH"
        )
    # Locking the zone's connectors serializes competing approvals.
    connectors = zone_connectors(session, zone_id, lock=True)
    if not connectors:
        raise RecoveryError("This zone has no ZKT terminal in ADD.", "ZONE_NOT_FOUND")
    for job in session.scalars(
        select(Job).where(Job.action == ACTION, Job.status == RELEASING)
    ).all():
        if job.scope.get("request", {}).get("zone_id") == zone_id:
            raise RecoveryError(
                "A release for this zone is already running.", "RELEASE_ALREADY_RUNNING"
            )
    connector_ids = [row.id for row in connectors]
    max_event_id = session.scalar(
        select(func.max(AttendanceEvent.id)).where(
            AttendanceEvent.connector_id.in_(connector_ids)
        )
    ) or 0
    held = session.scalar(
        select(func.count(AttendanceEvent.id)).where(
            *_held_dump_rows(connector_ids), AttendanceEvent.id <= max_event_id
        )
    ) or 0
    now = utc_now()
    scope = {
        "request": request,
        "connector_ids": connector_ids,
        "connector_keys": [row.connector_id for row in connectors],
        "max_event_id": max_event_id,
        "batch_size": settings.attendance_identity_backlog_batch_size,
    }
    job = Job(
        action=ACTION,
        status=RELEASING,
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        scope=scope,
        candidate_digest=_digest(scope),
        requested_count=held,
        started_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if (
            existing is not None and existing.action == ACTION and existing.actor == actor
            and existing.scope.get("request") == request
        ):
            return existing
        raise RecoveryError(
            "This request key belongs to a different request.", "IDEMPOTENCY_CONFLICT"
        ) from None
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_APPROVED",
        target_type="zone",
        target_id=zone_id,
        outcome="ACCEPTED",
        ip_address=ip_address,
        after={
            "job_id": job.job_id,
            "policy": POLICY,
            "reason": job.reason,
            "connectors": scope["connector_keys"],
            "max_event_id": max_event_id,
            "held": held,
        },
    )
    return job


def advance_once(session: Session, *, limit: int | None = None) -> int:
    """Release one capped batch of the oldest running zone release."""
    job = session.scalar(
        select(Job)
        .where(Job.action == ACTION, Job.status == RELEASING)
        .order_by(Job.updated_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return 0
    zone_id = job.scope["request"]["zone_id"]
    connector_ids = list(job.scope["connector_ids"])
    batch = max(1, min(limit or settings.attendance_identity_backlog_batch_size, 1000))
    now = utc_now()
    rows = session.scalars(
        select(AttendanceEvent)
        .where(
            *_held_dump_rows(connector_ids),
            AttendanceEvent.id > job.cursor,
            AttendanceEvent.id <= job.scope["max_event_id"],
        )
        .order_by(AttendanceEvent.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    ).all()
    if not rows:
        job.status = "COMPLETED"
        job.completed_at = job.updated_at = now
        append_audit(
            session,
            actor=job.actor,
            action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED",
            target_type="zone",
            target_id=zone_id,
            outcome="COMPLETED",
            after={
                "job_id": job.job_id,
                "released": job.succeeded_count,
                "kept_held": job.skipped_count,
            },
            request_id=f"{job.job_id}:completed",
        )
        return 0
    connectors = {
        row.id: row
        for row in session.scalars(select(Connector).where(Connector.id.in_(connector_ids))).all()
    }
    release = DumpIdentityRelease(held_since=None, remaining=len(rows))
    users: dict = {}
    released: list[int] = []
    for row in rows:
        connector = connectors.get(row.connector_id)
        zkt = connector.zkt_device if connector else None
        user = _unique_user(session, users, zkt.id, row.user_id) if zkt else None
        if user is not None and release_synced_cnic_attendance_event(
            session, zkt=zkt, user=user, row=row, dump_release=release
        ):
            released.append(row.id)
    job.cursor = rows[-1].id
    job.succeeded_count += len(released)
    job.skipped_count += len(rows) - len(released)
    job.updated_at = now
    append_audit(
        session,
        actor=job.actor,
        action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH",
        target_type="zone",
        target_id=zone_id,
        outcome="RELEASED",
        after={
            "job_id": job.job_id,
            "policy": POLICY,
            "released": len(released),
            "kept_held": len(rows) - len(released),
            "event_ids": released,
            "cursor": job.cursor,
        },
        request_id=f"{job.job_id}:{job.cursor}",
    )
    return len(rows)


def stop(session: Session, job: Job, *, actor: str, ip_address: str | None = None) -> Job:
    if job.action != ACTION:
        raise RecoveryError("This is not a zone release.", "RUN_MISMATCH")
    locked = session.scalar(select(Job).where(Job.id == job.id).with_for_update())
    if locked.status != RELEASING:
        return locked
    now = utc_now()
    locked.status = "STOPPED"
    locked.completed_at = locked.updated_at = now
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_STOPPED",
        target_type="zone",
        target_id=locked.scope["request"]["zone_id"],
        outcome="STOPPED",
        ip_address=ip_address,
        after={
            "job_id": locked.job_id,
            "released": locked.succeeded_count,
            "kept_held": locked.skipped_count,
        },
    )
    return locked


def serialize(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "zone_id": job.scope["request"]["zone_id"],
        "status": job.status,
        "actor": job.actor,
        "reason": job.reason,
        "connectors": job.scope.get("connector_keys", []),
        "held_at_approval": job.requested_count,
        "released": job.succeeded_count,
        "kept_held": job.skipped_count,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def tick() -> None:
    with session_scope() as session:
        advance_once(session)

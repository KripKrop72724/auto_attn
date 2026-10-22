"""Owner-approved release of a zone's older held legacy dump punches.

A legacy reconnect or startup dump row reaches ADD before the next roster
snapshot, so ingest holds it. The automatic synced-CNIC release admits such a
row on its authenticated batch proof only in zones the owner enabled, and
only while the hold is recent. Older holds are historical backlog.

This module previews them per zone without writing anything. The preview is
frozen at its first page (time, highest punch ID, terminals) and signed. The
owner approves chosen groups (terminal, capture type, hold age) of that
preview and a maximum count, with a password step-up and a typed
confirmation. The worker then releases them in small, paced, audited batches
with exactly the same proof and database guard, follows each released punch
until Oracle acknowledges it, and releases it again (same proof) if delivery
re-held it because a newer roster snapshot arrived first. A punch whose proof
fails stays held.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.attendance_identity_evidence import DUMP_RECONCILE_SOURCES
from zk_add.attendance_recovery import (
    RecoveryError,
    _digest,
    sign_recovery_preview,
    verify_recovery_preview,
)
from zk_add.attendance_safe_repair import source_digest
from zk_add.audit import append_audit
from zk_add.db import session_scope
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    Connector,
    DeviceUser,
)
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_RECONCILIATION_PENDING_STATUSES,
)
from zk_add.service import (
    DUMP_RELEASE_PROOF,
    DumpIdentityRelease,
    dump_auto_release_zone_enabled,
    dump_identity_release_enabled,
    release_synced_cnic_attendance_event,
    synced_cnic_release_proof,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

ACTION = "IDENTITY_BACKLOG_RELEASE"
POLICY = "identity-dump-backlog-v2"
# Deliberately not QUEUED/RUNNING: the generic recovery advancer owns those.
RELEASING = "RELEASING"
HELD = ("BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT")
DUMP_SOURCES = tuple(sorted(DUMP_RECONCILE_SOURCES))
AGE_BUCKETS = ("AUTOMATIC_WINDOW", "UP_TO_7_DAYS", "UP_TO_30_DAYS", "OLDER")
PREVIEW_PAGE_MAX = 500
PREVIEW_VALID = timedelta(hours=12)
# One zone check page at a time across ADD, whichever process serves it.
PREVIEW_LOCK_KEY = 0x41_44_44_42_4B_4C_47
ITEM_KIND = "ATTENDANCE_EVENT"
LANE = "IDENTITY_BACKLOG"
# A released punch waits for Oracle; REHELD: delivery held it again.
WAITING = "WAITING_ORACLE"
REHELD = "REHELD"
OPEN_ITEMS = (WAITING, REHELD)
# Re-checks of a re-held punch, one per newer roster snapshot.
REHOLD_ATTEMPTS = 3


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


def _bucket(received_at: datetime | None, as_of: datetime, window_hours: int) -> str:
    """Hold age at the frozen check time; must match _bucket_expression."""
    if received_at is None:
        return "OLDER"
    received = ensure_utc(received_at)
    if window_hours and received >= as_of - timedelta(hours=window_hours):
        return "AUTOMATIC_WINDOW"
    if received >= as_of - timedelta(days=7):
        return "UP_TO_7_DAYS"
    if received >= as_of - timedelta(days=30):
        return "UP_TO_30_DAYS"
    return "OLDER"


def _bucket_expression(as_of: datetime, window_hours: int):
    whens = []
    if window_hours:
        whens.append(
            (AttendanceEvent.received_at >= as_of - timedelta(hours=window_hours),
             "AUTOMATIC_WINDOW")
        )
    whens.extend([
        (AttendanceEvent.received_at >= as_of - timedelta(days=7), "UP_TO_7_DAYS"),
        (AttendanceEvent.received_at >= as_of - timedelta(days=30), "UP_TO_30_DAYS"),
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


def _single_release() -> DumpIdentityRelease:
    """The owner's release: no age limit; caps are enforced by the job."""
    return DumpIdentityRelease(held_since=None, remaining=1, rereleases_remaining=1)


def _parse_as_of(value: str) -> datetime:
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        raise RecoveryError("The zone check time is invalid. Check the zone again.",
                            "PREVIEW_INVALID") from None


def _claim_preview_slot(session: Session) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    if not session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": PREVIEW_LOCK_KEY}
    ):
        raise RecoveryError(
            "Another zone check is running. ADD retries in a moment.", "PREVIEW_BUSY"
        )


def preview_page(
    session: Session, *, zone_id: str, actor: str, after_id: int = 0, limit: int = 200,
    as_of: str | None = None, until_id: int | None = None, window_hours: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only: how many of a zone's held dump rows the proof would release.

    The first call (no as_of/until_id) freezes the check: its time, the
    highest held punch ID and the zone's terminals, signed for the approval,
    and reports the zone's totals. Later pages pass the same values back and
    are evaluated in event-ID order until next_after_id is null.
    """
    connectors = zone_connectors(session, zone_id)
    if not connectors:
        raise RecoveryError("This zone has no ZKT terminal in ADD.", "ZONE_NOT_FOUND")
    _claim_preview_slot(session)
    by_id = {row.id: row for row in connectors}
    first = as_of is None or until_id is None
    if first:
        frozen_at = ensure_utc(now or utc_now())
        until_id = session.scalar(
            select(func.max(AttendanceEvent.id)).where(*_held_dump_rows(list(by_id)))
        ) or 0
        window_hours = settings.attendance_dump_identity_auto_release_hours
    else:
        frozen_at = _parse_as_of(as_of)
        window_hours = (
            settings.attendance_dump_identity_auto_release_hours
            if window_hours is None else int(window_hours)
        )
    bounded = max(1, min(int(limit), PREVIEW_PAGE_MAX))
    rows = session.scalars(
        select(AttendanceEvent)
        .where(
            *_held_dump_rows(list(by_id)),
            AttendanceEvent.id > after_id,
            AttendanceEvent.id <= until_id,
        )
        .order_by(AttendanceEvent.id)
        .limit(bounded + 1)
    ).all()
    more = len(rows) > bounded
    rows = rows[:bounded]
    groups: dict[tuple[str, str, str], dict[str, int]] = {}
    users: dict = {}
    for row in rows:
        connector = by_id[row.connector_id]
        zkt = connector.zkt_device
        entry = groups.setdefault(
            (connector.connector_id, row.source, _bucket(row.received_at, frozen_at, window_hours)),
            {"held": 0, "provable": 0},
        )
        entry["held"] += 1
        user = _unique_user(session, users, zkt.id, row.user_id)
        if user is not None and synced_cnic_release_proof(
            session, zkt=zkt, user=user, row=row, dump_release=_single_release(),
        ):
            entry["provable"] += 1
    result = {
        "zone_id": zone_id,
        "after_id": after_id,
        "scanned": len(rows),
        "next_after_id": rows[-1].id if more else None,
        "groups": [
            {"connector_id": key, "source": source, "age": age, **counts}
            for (key, source, age), counts in sorted(groups.items())
        ],
        "as_of": frozen_at.isoformat(),
        "until_id": until_id,
        "window_hours": window_hours,
        "release_enabled": dump_identity_release_enabled(),
        "automatic_zone_enabled": dump_auto_release_zone_enabled(zone_id),
        "devices": [
            {"connector_id": row.connector_id, "name": row.display_name} for row in connectors
        ],
    }
    if first:
        bucket = _bucket_expression(frozen_at, window_hours)
        totals = session.execute(
            select(AttendanceEvent.connector_id, AttendanceEvent.source, bucket,
                   func.count(AttendanceEvent.id))
            .where(*_held_dump_rows(list(by_id)), AttendanceEvent.id <= until_id)
            .group_by(AttendanceEvent.connector_id, AttendanceEvent.source, bucket)
        ).all()
        result["held_total"] = sum(count for *_, count in totals)
        result["totals"] = [
            {"connector_id": by_id[connector_id].connector_id, "source": source,
             "age": age, "held": count}
            for connector_id, source, age, count in sorted(totals, key=lambda value: value[:3])
        ]
        scope = {
            "zone_id": zone_id,
            "as_of": result["as_of"],
            "until_id": until_id,
            "window_hours": window_hours,
            "connectors": sorted(row.connector_id for row in connectors),
        }
        expires_at = frozen_at + PREVIEW_VALID
        try:
            signature = sign_recovery_preview(
                digest=_digest(scope), expires_at=expires_at, actor=actor, action=ACTION
            )
        except RecoveryError:
            signature = None  # Counts still show; approval needs the signing key.
        result["approval"] = {
            "scope": scope,
            "expires_at": expires_at.isoformat(),
            "signature": signature,
        }
    return result


def _group_filter(groups: list[dict], ids_by_key: dict[str, int], as_of: datetime,
                  window_hours: int):
    bucket = _bucket_expression(as_of, window_hours)
    return or_(*(
        and_(
            AttendanceEvent.connector_id == ids_by_key[group["connector_id"]],
            AttendanceEvent.source == group["source"],
            bucket == group["age"],
        )
        for group in groups
    ))


def _normalized_groups(groups, connector_keys: set[str]) -> list[dict]:
    selected = sorted({
        (group["connector_id"], group["source"], group["age"]) for group in groups
    })
    if not selected:
        raise RecoveryError("Choose at least one group to release.", "GROUPS_REQUIRED")
    for key, source, age in selected:
        if key not in connector_keys or source not in DUMP_SOURCES or age not in AGE_BUCKETS:
            raise RecoveryError(
                "A chosen group is not part of this zone check. Check the zone again.",
                "PREVIEW_INVALID",
            )
    return [{"connector_id": key, "source": source, "age": age} for key, source, age in selected]


def create_release(
    session: Session, *, actor: str, zone_id: str, reason: str, typed_confirmation: str,
    idempotency_key: str, scope: dict, expires_at: datetime, signature: str,
    groups: list[dict], approved_count: int, previewed_held: int,
    ip_address: str | None = None,
) -> Job:
    """Approve releasing at most approved_count punches of the chosen preview groups."""
    scope = dict(scope)
    scope_digest = _digest(scope)
    request = {
        "zone_id": zone_id,
        "policy": POLICY,
        "scope_digest": scope_digest,
        "groups": sorted(
            [group["connector_id"], group["source"], group["age"]] for group in groups
        ),
        "approved_count": approved_count,
    }
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
    if not dump_identity_release_enabled():
        raise RecoveryError(
            "The dump release proof is turned off on this server "
            "(ADD_ATTENDANCE_DUMP_IDENTITY_RELEASE_ENABLED).",
            "RELEASE_PROOF_DISABLED",
        )
    if scope.get("zone_id") != zone_id:
        raise RecoveryError("This zone check belongs to another zone.", "PREVIEW_INVALID")
    verify_recovery_preview(
        digest=scope_digest, expires_at=expires_at, actor=actor, action=ACTION,
        signature=signature,
    )
    as_of = _parse_as_of(scope.get("as_of"))
    until_id = int(scope.get("until_id") or 0)
    window_hours = int(scope.get("window_hours") or 0)
    # Locking the zone's connectors serializes competing approvals.
    connectors = zone_connectors(session, zone_id, lock=True)
    if not connectors:
        raise RecoveryError("This zone has no ZKT terminal in ADD.", "ZONE_NOT_FOUND")
    if sorted(row.connector_id for row in connectors) != list(scope.get("connectors") or []):
        raise RecoveryError(
            "The zone's terminals changed since the check. Check the zone again.",
            "ZONE_CHANGED",
        )
    for job in session.scalars(
        select(Job).where(Job.action == ACTION, Job.status == RELEASING)
    ).all():
        if job.scope.get("request", {}).get("zone_id") == zone_id:
            raise RecoveryError(
                "A release for this zone is already running.", "RELEASE_ALREADY_RUNNING"
            )
    ids_by_key = {row.connector_id: row.id for row in connectors}
    chosen = _normalized_groups(groups, set(ids_by_key))
    if not 1 <= approved_count <= previewed_held:
        raise RecoveryError(
            "The approved count must be between 1 and the punches held in the chosen groups.",
            "APPROVED_COUNT_INVALID",
        )
    held_now = session.scalar(
        select(func.count(AttendanceEvent.id)).where(
            *_held_dump_rows(list(ids_by_key.values())),
            AttendanceEvent.id <= until_id,
            _group_filter(chosen, ids_by_key, as_of, window_hours),
        )
    ) or 0
    if held_now > previewed_held:
        raise RecoveryError(
            "More punches are held in the chosen groups than when you checked. "
            "Check the zone again.",
            "ZONE_CHANGED",
        )
    chosen_keys = {group["connector_id"] for group in chosen}
    now = utc_now()
    job_scope = {
        "request": request,
        "as_of": scope["as_of"],
        "until_id": until_id,
        "window_hours": window_hours,
        "groups": chosen,
        "connector_ids": [ids_by_key[key] for key in sorted(chosen_keys)],
        "connector_keys": sorted(chosen_keys),
        # The terminal each connector had at approval; a moved or swapped
        # terminal takes its punches out of this release.
        "zkt_device_ids": {
            str(row.id): row.zkt_device.id for row in connectors if row.connector_id in chosen_keys
        },
        "previewed_held": previewed_held,
        "held_at_approval": held_now,
        "batch_size": settings.attendance_identity_backlog_batch_size,
        "in_flight_limit": settings.attendance_identity_backlog_in_flight_limit,
    }
    job = Job(
        action=ACTION,
        status=RELEASING,
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        scope=job_scope,
        candidate_digest=_digest(job_scope),
        preview_expires_at=ensure_utc(expires_at),
        requested_count=approved_count,
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
            "checked_at": scope["as_of"],
            "until_event_id": until_id,
            "scope_digest": scope_digest,
            "groups": chosen,
            "approved_count": approved_count,
            "previewed_held": previewed_held,
            "held_at_approval": held_now,
        },
    )
    return job


def _job_zkt(session: Session, job: Job, connector_id: int, cache: dict):
    """The connector's terminal if it is still the one approved in this zone."""
    if connector_id not in cache:
        connector = session.get(Connector, connector_id)
        zkt = connector.zkt_device if connector else None
        approved = job.scope.get("zkt_device_ids", {}).get(str(connector_id))
        cache[connector_id] = (
            zkt if (
                connector is not None
                and connector.zone_id == job.scope["request"]["zone_id"]
                and connector.firmware_family != "hikvision"
                and zkt is not None and zkt.id == approved
            ) else None
        )
    return cache[connector_id]


def _close_item(item: Item, status: str, outcome: str, now: datetime) -> None:
    item.status, item.outcome, item.completed_at = status, outcome, now


def _follow_up(session: Session, job: Job, now: datetime, zkts: dict) -> list[int]:
    """Track released punches until Oracle acknowledges them.

    Delivery re-holds a released punch when a newer roster snapshot arrives
    first. It is checked again, with the same proof, once per newer snapshot,
    and given up (kept held) after REHOLD_ATTEMPTS failed checks.
    """
    rereleased: list[int] = []
    users: dict = {}
    limit = int(job.scope.get("in_flight_limit") or 100) + int(job.scope.get("batch_size") or 25)
    items = session.scalars(
        select(Item)
        .where(Item.job_id == job.id, Item.status.in_(OPEN_ITEMS))
        .order_by(Item.id)
        .limit(limit)
    ).all()
    for item in items:
        event = session.get(AttendanceEvent, item.attendance_event_id)
        status = event.ords_status if event else None
        if status in ORDS_ACKNOWLEDGED_STATUSES:
            _close_item(item, "DELIVERED", "ORACLE_ACKNOWLEDGED", now)
            job.succeeded_count += 1
            continue
        if status in ORDS_RECONCILIATION_PENDING_STATUSES:
            item.status = WAITING
            continue
        if status not in HELD:
            _close_item(item, "NEEDS_REVIEW", status or "MISSING", now)
            job.failed_count += 1
            continue
        zkt = _job_zkt(session, job, event.connector_id, zkts)
        if zkt is None:
            _close_item(item, "KEPT_HELD", "TERMINAL_CHANGED", now)
            job.identity_held_count += 1
            continue
        result = dict(item.result or {})
        if result.get("checked_snapshot_id") == zkt.identity_snapshot_id:
            item.status = REHELD
            continue  # Wait for a newer roster snapshot.
        locked = session.scalar(
            select(AttendanceEvent)
            .where(AttendanceEvent.id == event.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if locked is None:
            continue  # Another transaction holds it; check again next tick.
        if locked.ords_status not in HELD:
            continue
        user = _unique_user(session, users, zkt.id, locked.user_id)
        result["checked_snapshot_id"] = zkt.identity_snapshot_id
        if user is not None and release_synced_cnic_attendance_event(
            session, zkt=zkt, user=user, row=locked, dump_release=_single_release(),
        ):
            result["rereleases"] = int(result.get("rereleases") or 0) + 1
            item.status = WAITING
            item.attempt_count += 1
            rereleased.append(locked.id)
        else:
            result["failed_checks"] = int(result.get("failed_checks") or 0) + 1
            item.status = REHELD
            if result["failed_checks"] >= REHOLD_ATTEMPTS:
                _close_item(item, "KEPT_HELD", "PROOF_NO_LONGER_PASSES", now)
                job.identity_held_count += 1
        item.result = result
    return rereleased


def _open_items(session: Session, job: Job) -> int:
    session.flush()  # Sessions here may not autoflush; count this tick's changes.
    return session.scalar(
        select(func.count(Item.id)).where(Item.job_id == job.id, Item.status.in_(OPEN_ITEMS))
    ) or 0


def advance_once(session: Session, *, limit: int | None = None) -> int:
    """Advance the least recently served zone release by one paced batch.

    Returns the number of new punches evaluated (0 when nothing new ran).
    """
    if not dump_identity_release_enabled():
        return 0  # Paused by the owner's switch; nothing is released.
    job = session.scalar(
        select(Job)
        .where(Job.action == ACTION, Job.status == RELEASING)
        .order_by(Job.updated_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return 0
    scope = job.scope
    zone_id = scope["request"]["zone_id"]
    until_id = int(scope["until_id"])
    now = utc_now()
    job.updated_at = now
    zkts: dict = {}
    rereleased = _follow_up(session, job, now, zkts)
    open_items = _open_items(session, job)
    batch = max(1, min(limit or int(scope.get("batch_size") or 25), 200))
    budget = min(
        batch,
        int(scope.get("in_flight_limit") or 100) - open_items,
        job.requested_count - job.eligible_count,
    )
    released: list[int] = []
    kept: list[int] = []
    evaluated = 0
    if job.cursor < until_id and job.eligible_count >= job.requested_count:
        job.cursor = until_id  # The approved count is reached.
    elif job.cursor < until_id and budget > 0:
        ids_by_key = dict(zip(scope["connector_keys"], scope["connector_ids"]))
        pending = (
            *_held_dump_rows(list(scope["connector_ids"])),
            AttendanceEvent.id > job.cursor,
            AttendanceEvent.id <= until_id,
            _group_filter(scope["groups"], ids_by_key, _parse_as_of(scope["as_of"]),
                          int(scope["window_hours"])),
        )
        candidates = session.scalars(
            select(AttendanceEvent.id).where(*pending).order_by(AttendanceEvent.id).limit(budget)
        ).all()
        if not candidates:
            job.cursor = until_id
        else:
            # A snapshot, chunk or delivery transaction may hold some rows.
            # Skip locks to avoid deadlocks, but stop before the first
            # unavailable row so the cursor never passes an unevaluated punch.
            locked = {
                row.id: row
                for row in session.scalars(
                    select(AttendanceEvent)
                    .where(*pending, AttendanceEvent.id.in_(candidates))
                    .order_by(AttendanceEvent.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                ).all()
            }
            rows = []
            for event_id in candidates:
                if event_id not in locked:
                    break
                rows.append(locked[event_id])
            users: dict = {}
            for row in rows:
                evaluated += 1
                zkt = _job_zkt(session, job, row.connector_id, zkts)
                user = _unique_user(session, users, zkt.id, row.user_id) if zkt else None
                release = _single_release()
                if user is not None and release_synced_cnic_attendance_event(
                    session, zkt=zkt, user=user, row=row, dump_release=release,
                ):
                    released.append(row.id)
                    session.add(Item(
                        job_id=job.id,
                        source_kind=ITEM_KIND,
                        source_ref=str(row.id),
                        connector_id=row.connector_id,
                        attendance_event_id=row.id,
                        expected_state_digest=source_digest(row),
                        status=WAITING,
                        lane=LANE,
                        outcome="RELEASED",
                        attempt_count=1,
                        result={
                            "proof": (
                                DUMP_RELEASE_PROOF
                                if release.released_ids or release.rereleased_ids
                                else "SYNCED_CNIC"
                            ),
                            "checked_snapshot_id": zkt.identity_snapshot_id,
                        },
                    ))
                else:
                    kept.append(row.id)
            if rows:
                job.cursor = rows[-1].id
            job.eligible_count += len(released)
            job.skipped_count += len(kept)
    if released or kept or rereleased:
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
                # Queued for Oracle; job "delivered" counts acknowledgements.
                "queued_for_oracle": len(released),
                "event_ids": released,
                "requeued_after_rehold": len(rereleased),
                "requeued_event_ids": rereleased,
                "kept_held": len(kept),
                "kept_held_event_ids": kept,
                "cursor": job.cursor,
            },
            request_id=f"{job.job_id}:{job.cursor}:{job.eligible_count}:{job.succeeded_count}",
        )
    if job.cursor >= until_id and not _open_items(session, job):
        job.status = "COMPLETED"
        job.completed_at = now
        append_audit(
            session,
            actor=job.actor,
            action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED",
            target_type="zone",
            target_id=zone_id,
            outcome="COMPLETED",
            after={"job_id": job.job_id, **_counts(session, job)},
            request_id=f"{job.job_id}:completed",
        )
    return evaluated


def _counts(session: Session, job: Job) -> dict:
    session.flush()
    by_status = dict(session.execute(
        select(Item.status, func.count(Item.id)).where(Item.job_id == job.id).group_by(Item.status)
    ).all())
    return {
        "approved": job.requested_count,
        "released": job.eligible_count,
        "delivered": by_status.get("DELIVERED", 0),
        "waiting_oracle": by_status.get(WAITING, 0) + by_status.get(REHELD, 0),
        "kept_held": job.skipped_count + by_status.get("KEPT_HELD", 0),
        "needs_review": by_status.get("NEEDS_REVIEW", 0),
    }


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
        after={"job_id": locked.job_id, **_counts(session, locked)},
    )
    return locked


def serialize(session: Session, job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "zone_id": job.scope["request"]["zone_id"],
        "status": job.status,
        "paused": job.status == RELEASING and not dump_identity_release_enabled(),
        "actor": job.actor,
        "reason": job.reason,
        "connectors": job.scope.get("connector_keys", []),
        "groups": job.scope.get("groups", []),
        "checked_at": job.scope.get("as_of"),
        **_counts(session, job),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def tick() -> None:
    with session_scope() as session:
        advance_once(session)

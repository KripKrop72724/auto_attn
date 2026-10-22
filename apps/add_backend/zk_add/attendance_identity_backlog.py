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
with exactly the same dump proof and database guard, follows each released
punch until Oracle acknowledges it, and releases it again (same proof) if
delivery re-held it because a newer roster snapshot arrived first. A punch
whose proof fails stays held.

Only the dump proof (AUTHENTICATED_RECONCILE_BATCH) is used here. A punch
another proof can release is left to the next roster snapshot, which already
applies those proofs. The scope is the zone's held dump punches never
released before: a punch a proof released and delivery held again belongs
to whatever released it, so a release never takes in punches that were on
their way to Oracle when the owner checked the zone. While a chosen
terminal's roster is incomplete, unstable or being replaced, the release
waits instead of passing punches it could not check.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, not_, or_, select, text
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
    ZKTDevice,
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
POLICY = "identity-dump-backlog-v3"
# Deliberately not QUEUED/RUNNING/STOPPING: the generic recovery advancer and
# other recovery jobs' connector checks own those.
RELEASING = "RELEASING"
# Stopped: no new punches are released, but those already released are still
# followed to Oracle (and re-checked if delivery holds them again).
STOPPED_FOLLOWING = "STOPPED_FOLLOWING"
ACTIVE = (RELEASING, STOPPED_FOLLOWING)
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


def _previously_released():
    """SQL form of service.previously_synced_cnic_release."""
    return and_(
        func.coalesce(AttendanceEvent.identity_resolution_status, "") == "RESOLVED_SYNCED_CNIC",
        func.coalesce(AttendanceEvent.identity_repair_reason, "") == "VERIFIED_SYNCED_CNIC",
    )


def _held_dump_rows(connector_ids: list[int]):
    """The backlog: held dump punches no proof has released before."""
    return (
        AttendanceEvent.connector_id.in_(connector_ids),
        AttendanceEvent.ords_status.in_(HELD),
        AttendanceEvent.source.in_(DUMP_SOURCES),
        not_(_previously_released()),
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
            ).limit(2).execution_options(populate_existing=True)
        ).all()
        cache[key] = users[0] if len(users) == 1 else None
    return cache[key]


def _single_release() -> DumpIdentityRelease:
    """The owner's release: the dump proof only, no age limit; the job caps it."""
    return DumpIdentityRelease(
        held_since=None, remaining=1, rereleases_remaining=1, only_dump_proof=True
    )


def _roster_state(zkt: ZKTDevice) -> tuple:
    return (
        zkt.identity_snapshot_id, zkt.identity_snapshot_revision,
        bool(zkt.snapshot_complete), bool(zkt.identity_snapshot_stable),
        zkt.confirmed_serial, zkt.serial,
    )


def _roster_changed(session: Session, zkt: ZKTDevice) -> bool:
    """Whether another transaction replaced the terminal's roster since it was read."""
    current = session.execute(
        select(
            ZKTDevice.identity_snapshot_id, ZKTDevice.identity_snapshot_revision,
            ZKTDevice.snapshot_complete, ZKTDevice.identity_snapshot_stable,
            ZKTDevice.confirmed_serial, ZKTDevice.serial,
        ).where(ZKTDevice.id == zkt.id)
    ).one()
    return (
        current[0], current[1], bool(current[2]), bool(current[3]), current[4], current[5]
    ) != _roster_state(zkt)


def _roster_waiting(zkt: ZKTDevice) -> str | None:
    """A terminal-wide reason no punch of it can be checked now.

    A partial or unstable roster, or a terminal awaiting serial confirmation,
    fails every punch's proof. That is not a verdict on the punch, so the
    release waits for the next complete roster instead of passing it.
    """
    if not (
        zkt.snapshot_complete and zkt.identity_snapshot_stable
        and zkt.identity_snapshot_id and zkt.identity_snapshot_observed_at
    ):
        return "WAITING_FOR_COMPLETE_ROSTER"
    if not zkt.confirmed_serial or zkt.confirmed_serial != zkt.serial:
        return "WAITING_FOR_SERIAL_CONFIRMATION"
    return None


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
    """Read-only: how many of a zone's held dump rows the dump proof would release.

    The first call (no as_of/until_id) freezes the check: its time, the
    highest held punch ID and the zone's terminals, signed for the approval,
    and reports the zone's totals. Later pages pass the same values back and
    are evaluated in event-ID order until next_after_id is null. Per group:
    held, provable (the dump proof passes now) and next_snapshot (another
    proof passes; the next roster snapshot releases it, not this action).
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
            {"held": 0, "provable": 0, "next_snapshot": 0},
        )
        entry["held"] += 1
        user = _unique_user(session, users, zkt.id, row.user_id)
        release = _single_release()
        if user is not None and synced_cnic_release_proof(
            session, zkt=zkt, user=user, row=row, dump_release=release,
        ) == DUMP_RELEASE_PROOF:
            entry["provable"] += 1
        elif release.other_proof_ids:
            entry["next_snapshot"] += 1
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
        # Released before and held again by delivery: re-checked by whatever
        # released them (a running zone release or the roster snapshot), never
        # by a new approval.
        result["reheld_outside_check"] = session.scalar(
            select(func.count(AttendanceEvent.id)).where(
                AttendanceEvent.connector_id.in_(list(by_id)),
                AttendanceEvent.ords_status.in_(HELD),
                AttendanceEvent.source.in_(DUMP_SOURCES),
                _previously_released(),
            )
        ) or 0
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
        select(Job).where(Job.action == ACTION, Job.status.in_(ACTIVE))
    ).all():
        if job.scope.get("request", {}).get("zone_id") == zone_id:
            raise RecoveryError(
                "A release for this zone is still running or following its punches to Oracle.",
                "RELEASE_ALREADY_RUNNING",
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
    """The connector's terminal, freshly read, if it is still the one approved in this zone."""
    if connector_id not in cache:
        connector = session.get(Connector, connector_id, populate_existing=True)
        zkt = session.scalar(
            select(ZKTDevice)
            .where(ZKTDevice.connector_id == connector_id)
            .execution_options(populate_existing=True)
        ) if connector is not None else None
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
    first. It is checked again, with the same proof, once per newer complete
    snapshot, and given up (kept held) after REHOLD_ATTEMPTS failed checks.
    A check the roster could not support (incomplete, or replaced while it
    ran) does not count, and neither does one another proof passes: the
    roster snapshot releases that punch.
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
        item.status = REHELD
        result = dict(item.result or {})
        if _roster_waiting(zkt) or result.get("checked_snapshot_id") == zkt.identity_snapshot_id:
            continue  # Wait for a newer complete roster snapshot.
        locked = session.scalar(
            select(AttendanceEvent)
            .where(AttendanceEvent.id == event.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if locked is None or locked.ords_status not in HELD:
            continue  # Another transaction holds or released it; look again next tick.
        user = _unique_user(session, users, zkt.id, locked.user_id)
        if user is not None and user.snapshot_revision != zkt.identity_snapshot_revision:
            continue  # The roster is being replaced; check against the new one.
        release = _single_release()
        if user is not None and release_synced_cnic_attendance_event(
            session, zkt=zkt, user=user, row=locked, dump_release=release,
        ):
            result["rereleases"] = int(result.get("rereleases") or 0) + 1
            item.status = WAITING
            item.attempt_count += 1
            rereleased.append(locked.id)
        elif release.other_proof_ids:
            result["left_to_snapshot"] = True
        elif _roster_changed(session, zkt):
            continue
        else:
            result["failed_checks"] = int(result.get("failed_checks") or 0) + 1
            if result["failed_checks"] >= REHOLD_ATTEMPTS:
                _close_item(item, "KEPT_HELD", "PROOF_NO_LONGER_PASSES", now)
                job.identity_held_count += 1
        result["checked_snapshot_id"] = zkt.identity_snapshot_id
        item.result = result
    return rereleased


def _open_items(session: Session, job: Job) -> int:
    session.flush()  # Sessions here may not autoflush; count this tick's changes.
    return session.scalar(
        select(func.count(Item.id)).where(Item.job_id == job.id, Item.status.in_(OPEN_ITEMS))
    ) or 0


def _release_batch(session: Session, job: Job, zkts: dict, limit: int | None):
    """One paced batch of the approved scan. Returns (evaluated, released, kept, left, waiting).

    The cursor only passes punches whose proof was checked against a
    complete, stable roster that did not change while it ran. Otherwise the
    batch stops before the punch and the next tick checks it again, so a
    short-lived roster problem never leaves an approved punch held.
    """
    scope = job.scope
    until_id = int(scope["until_id"])
    released: list[int] = []
    kept: list[int] = []
    left: list[int] = []
    waiting = None
    evaluated = 0
    if job.cursor >= until_id:
        return evaluated, released, kept, left, waiting
    if job.eligible_count >= job.requested_count:
        job.cursor = until_id  # The approved count is reached.
        return evaluated, released, kept, left, waiting
    batch = max(1, min(limit or int(scope.get("batch_size") or 25), 200))
    budget = min(
        batch,
        int(scope.get("in_flight_limit") or 100) - _open_items(session, job),
        job.requested_count - job.eligible_count,
    )
    if budget <= 0:
        return evaluated, released, kept, left, waiting
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
        return evaluated, released, kept, left, waiting
    # A snapshot, chunk or delivery transaction may hold some rows. Skip
    # locks to avoid deadlocks, but stop before the first unavailable row so
    # the cursor never passes an unevaluated punch.
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
    users: dict = {}
    checked = None
    for event_id in candidates:
        row = locked.get(event_id)
        if row is None:
            break
        zkt = _job_zkt(session, job, row.connector_id, zkts)
        user = None
        if zkt is not None:
            waiting = _roster_waiting(zkt)
            if waiting:
                break
            user = _unique_user(session, users, zkt.id, row.user_id)
            if user is not None and user.snapshot_revision != zkt.identity_snapshot_revision:
                waiting = "WAITING_FOR_ROSTER_UPDATE"
                break
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
                    "proof": DUMP_RELEASE_PROOF,
                    "checked_snapshot_id": zkt.identity_snapshot_id,
                },
            ))
        elif release.other_proof_ids:
            left.append(row.id)
        elif zkt is not None and _roster_changed(session, zkt):
            # Another transaction replaced the roster while this punch was
            # checked; the verdict is not reliable. Check it again.
            waiting = "WAITING_FOR_ROSTER_UPDATE"
            break
        else:
            kept.append(row.id)
        evaluated += 1
        checked = row.id
    if checked is not None:
        job.cursor = checked
    job.eligible_count += len(released)
    job.skipped_count += len(kept)
    job.excluded_count += len(left)
    return evaluated, released, kept, left, waiting


def advance_once(session: Session, *, limit: int | None = None) -> int:
    """Advance the least recently served zone release by one paced batch.

    Returns the number of new punches evaluated (0 when nothing new ran).
    A stopped release only follows the punches it already released.
    """
    if not dump_identity_release_enabled():
        return 0  # Paused by the owner's switch; nothing is released.
    job = session.scalar(
        select(Job)
        .where(Job.action == ACTION, Job.status.in_(ACTIVE))
        .order_by(Job.updated_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return 0
    # Pending changes reach the database before terminals are read again.
    session.flush()
    zone_id = job.scope["request"]["zone_id"]
    now = utc_now()
    job.updated_at = now
    zkts: dict = {}
    rereleased = _follow_up(session, job, now, zkts)
    evaluated, released, kept, left, waiting = 0, [], [], [], None
    if job.status == RELEASING:
        evaluated, released, kept, left, waiting = _release_batch(session, job, zkts, limit)
    job.last_error = waiting
    if released or kept or left or rereleased:
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
                "proof": DUMP_RELEASE_PROOF,
                # Queued for Oracle; job "delivered" counts acknowledgements.
                "queued_for_oracle": len(released),
                "event_ids": released,
                "requeued_after_rehold": len(rereleased),
                "requeued_event_ids": rereleased,
                "kept_held": len(kept),
                "kept_held_event_ids": kept,
                # Another proof passes: the next roster snapshot releases them.
                "left_to_snapshot": len(left),
                "left_to_snapshot_event_ids": left,
                "cursor": job.cursor,
            },
            request_id=f"{job.job_id}:{job.cursor}:{job.eligible_count}:{job.succeeded_count}",
        )
    finished = (
        job.status == STOPPED_FOLLOWING
        or job.cursor >= int(job.scope["until_id"])
    ) and not _open_items(session, job)
    if finished:
        stopped = job.status == STOPPED_FOLLOWING
        job.status = "STOPPED" if stopped else "COMPLETED"
        job.completed_at = now
        job.last_error = None
        append_audit(
            session,
            actor=job.actor,
            action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED",
            target_type="zone",
            target_id=zone_id,
            outcome=job.status,
            after={"job_id": job.job_id, "stopped": stopped, **_counts(session, job)},
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
        "left_to_snapshot": job.excluded_count,
        "needs_review": by_status.get("NEEDS_REVIEW", 0),
    }


def stop(session: Session, job: Job, *, actor: str, ip_address: str | None = None) -> Job:
    """Release no more punches. Those already released are still followed to Oracle."""
    if job.action != ACTION:
        raise RecoveryError("This is not a zone release.", "RUN_MISMATCH")
    locked = session.scalar(select(Job).where(Job.id == job.id).with_for_update())
    if locked.status != RELEASING:
        return locked
    now = utc_now()
    following = _open_items(session, locked)
    locked.status = STOPPED_FOLLOWING if following else "STOPPED"
    locked.updated_at = now
    locked.last_error = None
    if not following:
        locked.completed_at = now
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_IDENTITY_BACKLOG_RELEASE_STOPPED",
        target_type="zone",
        target_id=locked.scope["request"]["zone_id"],
        outcome="STOPPED",
        ip_address=ip_address,
        after={"job_id": locked.job_id, "still_following": following, **_counts(session, locked)},
    )
    return locked


def serialize(session: Session, job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "zone_id": job.scope["request"]["zone_id"],
        "status": job.status,
        "paused": job.status in ACTIVE and not dump_identity_release_enabled(),
        # Why the last batch could not check its next punch, if it could not.
        "waiting_reason": job.last_error if job.status == RELEASING else None,
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

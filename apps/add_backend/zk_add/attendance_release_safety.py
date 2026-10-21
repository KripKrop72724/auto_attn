"""Read-only checks behind an administrator's manual Oracle send.

A manual send overrides ADD's delivery holds, so it must never be the step
that delivers an impossible time or silently attributes an older punch to
whoever holds the terminal user ID today. Nothing here writes attendance or
changes the automatic identity proof; it only reads and reuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_cnic, decrypt_text, normalize_cnic
from zk_add.models import AttendanceIdentityHistory, Connector, DeviceUser
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

CLOCK_INVALID = "CLOCK_INVALID"
TIME_IMPOSSIBLE = "TIME_IMPOSSIBLE"
TIME_REFUSALS = frozenset({CLOCK_INVALID, TIME_IMPOSSIBLE})

# Which CNIC the terminal held for this user ID when the punch happened.
MATCHES_CURRENT = "MATCHES_CURRENT"  # every retained interval has today's CNIC
DIFFERS = "DIFFERS"  # one retained CNIC, different from today's (Toheed, 19 Oct)
AMBIGUOUS = "AMBIGUOUS"  # several CNICs, revoked or unreadable intervals
NONE = "NONE"  # no interval covers the punch (pre-history, new hire, no CNIC)

HELD_FOR_SNAPSHOT = frozenset({"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"})
MAX_INTERVALS = 100


def time_refusal(event) -> str | None:
    """A punch whose time cannot be true must never reach Oracle by hand.

    24 Sep: a manual send delivered rows dated 2027-10-11 and 2074-08-24.
    """
    from zk_add.service import MAX_DEVICE_CLOCK_LEAD, attendance_device_time_is_plausible

    if event.clock_quality == "INVALID":
        return CLOCK_INVALID
    if event.device_event_time is None or event.captured_at is None:
        return TIME_IMPOSSIBLE
    when = ensure_utc(event.device_event_time)
    if (
        not attendance_device_time_is_plausible(when, event.captured_at)
        or when > utc_now() + MAX_DEVICE_CLOCK_LEAD
        or (
            event.received_at is not None
            and when > ensure_utc(event.received_at) + MAX_DEVICE_CLOCK_LEAD
        )
    ):
        return TIME_IMPOSSIBLE
    return None


@dataclass(frozen=True)
class PunchTimeIdentity:
    state: str
    cnic: str | None = None  # the single retained punch-time CNIC (DIFFERS only)
    cnic_hash: str | None = None
    display_name: str | None = None
    history_ids: tuple[int, ...] = ()


def _covers(history: AttendanceIdentityHistory, when, tolerance: timedelta) -> bool:
    until = ensure_utc(history.observed_until)
    return ensure_utc(history.observed_from) <= when and (
        until >= when or (not history.closed and until >= when - tolerance)
    )


def _readable_cnic(history: AttendanceIdentityHistory) -> str | None:
    from zk_add.attendance_identity_evidence import valid_cnic

    if not valid_cnic(history.cnic_encrypted, history.cnic_lookup_hash):
        return None
    return normalize_cnic(decrypt_cnic(history.cnic_encrypted))


def classify_punch_time(
    event, intervals, current_cnic_hash: str | None, *, verify: bool = True,
) -> PunchTimeIdentity:
    """Compare the CNIC(s) retained for the punch time with today's CNIC.

    Intervals are matched by terminal, serial and user ID. A revoked interval
    or one for another UID still counts as evidence that the identity changed,
    but only a retained interval for the punch's own UID can supply a CNIC.
    ``verify=False`` compares lookup hashes only, for bounded ledger hints.
    """
    if event.device_event_time is None:
        return PunchTimeIdentity(NONE)
    when = ensure_utc(event.device_event_time)
    tolerance = timedelta(seconds=settings.identity_snapshot_capture_tolerance_seconds)
    covering = sorted(
        (row for row in intervals if _covers(row, when, tolerance)), key=lambda row: row.id,
    )
    if not covering:
        return PunchTimeIdentity(NONE)
    ids = tuple(row.id for row in covering)
    if len(covering) > MAX_INTERVALS or any(
        verify and _readable_cnic(row) is None for row in covering
    ):
        return PunchTimeIdentity(AMBIGUOUS, history_ids=ids)
    hashes = {row.cnic_lookup_hash for row in covering}
    if current_cnic_hash and hashes == {current_cnic_hash}:
        return PunchTimeIdentity(MATCHES_CURRENT, history_ids=ids)
    retained = [
        row for row in covering
        if not row.revoked and (not event.uid or row.uid == event.uid)
    ]
    if len(hashes) != 1 or not retained:
        return PunchTimeIdentity(AMBIGUOUS, history_ids=ids)
    chosen = retained[-1]
    return PunchTimeIdentity(
        DIFFERS,
        cnic=_readable_cnic(chosen) if verify else None,
        cnic_hash=chosen.cnic_lookup_hash,
        display_name=decrypt_text(chosen.display_name_encrypted) if verify else None,
        history_ids=ids,
    )


def _interval_query(keys):
    return select(AttendanceIdentityHistory).where(
        tuple_(
            AttendanceIdentityHistory.zkt_device_id,
            AttendanceIdentityHistory.terminal_serial,
            AttendanceIdentityHistory.user_id,
        ).in_(keys)
    )


def punch_time_identity(session: Session, event, current_cnic_hash: str | None) -> PunchTimeIdentity:
    if not (event.zkt_device_id and event.device_serial and event.user_id):
        return PunchTimeIdentity(NONE)
    intervals = session.scalars(
        _interval_query([(event.zkt_device_id, event.device_serial, event.user_id)])
        .order_by(AttendanceIdentityHistory.id).limit(5 * MAX_INTERVALS)
    ).all()
    return classify_punch_time(event, intervals, current_cnic_hash)


def intervals_for_page(session: Session, events) -> dict[tuple, list[AttendanceIdentityHistory]]:
    """Retained intervals for a ledger page in bounded, chunked queries."""
    keys = sorted({
        (row.zkt_device_id, row.device_serial, row.user_id)
        for row in events
        if row.zkt_device_id and row.device_serial and row.user_id
    })
    found: dict[tuple, list[AttendanceIdentityHistory]] = {}
    for start in range(0, len(keys), 200):
        for row in session.scalars(_interval_query(keys[start:start + 200])).all():
            found.setdefault((row.zkt_device_id, row.terminal_serial, row.user_id), []).append(row)
    return found


def database_guard_state(session: Session, *, event, zkt, user) -> str:
    """Ask the installed PostgreSQL release guard, without changing anything.

    The guard check is a SELECT over a proposed copy of the row. It runs in a
    savepoint so an unexpected database error cannot spoil the caller's
    transaction.
    """
    from zk_add.attendance_auto_release_guard import database_synced_cnic_release_proven

    if session.get_bind().dialect.name != "postgresql":
        return "NOT_APPLICABLE"
    try:
        with session.begin_nested():
            accepted = database_synced_cnic_release_proven(
                session, row=event, zkt=zkt, user=user,
            )
    except Exception:
        return "UNAVAILABLE"
    return "ACCEPTED" if accepted else "REJECTED"


def auto_release_preview(session: Session, event, *, include_guard: bool = True) -> dict:
    """Would ADD's own proof release this held punch without an administrator?

    ``at_next_snapshot``: the automatic proof (and the database guard) passes
    now, so the next user snapshot releases it. ``awaiting_snapshot``: the
    punch arrived after the latest snapshot and ADD checks it again at the
    next one (normally within 15 minutes).
    """
    from zk_add.attendance_identity_evidence import source_evidence
    from zk_add.service import synced_cnic_identity_proven

    result = {
        "held": event.ords_status in HELD_FOR_SNAPSHOT,
        "proof": False,
        "database_guard": "NOT_CHECKED",
        "at_next_snapshot": False,
        "awaiting_snapshot": False,
    }
    if not result["held"] or time_refusal(event):
        return result
    connector = session.get(Connector, event.connector_id)
    zkt = connector.zkt_device if connector else None
    if zkt is None:
        return result
    users = session.scalars(select(DeviceUser).where(
        DeviceUser.zkt_device_id == zkt.id,
        DeviceUser.user_id == event.user_id,
        DeviceUser.lifecycle_state == "ACTIVE",
    ).limit(2)).all()
    if len(users) != 1:
        return result
    user = users[0]
    result["proof"] = bool(synced_cnic_identity_proven(session, connector, zkt, user, event))
    if result["proof"]:
        result["database_guard"] = (
            database_guard_state(session, event=event, zkt=zkt, user=user)
            if include_guard else "NOT_CHECKED"
        )
        result["at_next_snapshot"] = result["database_guard"] in {
            "ACCEPTED", "NOT_APPLICABLE", "NOT_CHECKED",
        }
        return result
    snapshot_at = zkt.identity_snapshot_observed_at
    result["awaiting_snapshot"] = bool(
        user.present and user.cnic_lookup_hash and not user.identity_conflict_code
        and zkt.snapshot_complete and zkt.identity_snapshot_stable
        and event.clock_quality == "OK"
        and (snapshot_at is None or ensure_utc(event.captured_at) > ensure_utc(snapshot_at))
        and (
            event.source in {"LIVE", "LIVE_POLL"}
            or source_evidence(session, event, connector) is not None
        )
    )
    return result


def refresh_cnic_format_alert(session: Session, connector: Connector, zkt) -> int:
    """Raise USER_CNIC_FORMAT_SUSPECT while an active user's name hides a CNIC typo.

    Such a user has no CNIC in ADD, so every punch is held. The alert names
    terminal user IDs only; the suspect digits are never parsed or stored.
    """
    from zk_add.identity import suspect_cnic_name
    from zk_add.service import resolve_alert, upsert_alert

    suspect_user_ids = sorted(
        user.user_id
        for user in session.scalars(select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.present.is_(True),
            DeviceUser.cnic_lookup_hash.is_(None),
        )).all()
        if suspect_cnic_name(decrypt_text(user.machine_name_encrypted))
    )
    if suspect_user_ids:
        upsert_alert(
            session,
            connector,
            code="USER_CNIC_FORMAT_SUSPECT",
            severity="WARNING",
            message=(
                "A terminal user name contains a CNIC in a format ADD cannot use "
                "(for example a letter before the 13 digits, or dashes). Their punches "
                "stay held. Confirm the CNIC with HR and set it in ADD Users."
            ),
            details={"affected_users": len(suspect_user_ids), "user_ids": suspect_user_ids[:50]},
        )
    else:
        resolve_alert(session, connector, code="USER_CNIC_FORMAT_SUSPECT")
    return len(suspect_user_ids)


def suspect_user_count(users) -> int:
    from zk_add.identity import suspect_cnic_name

    return sum(
        1 for user in users
        if not user.cnic_lookup_hash
        and suspect_cnic_name(decrypt_text(user.machine_name_encrypted))
    )

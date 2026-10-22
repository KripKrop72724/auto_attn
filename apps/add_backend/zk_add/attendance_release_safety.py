"""Read-only checks behind an administrator's manual Oracle send.

A manual send overrides ADD's delivery holds, so it must never be the step
that delivers an impossible time or silently attributes an older punch to
whoever holds the terminal user ID today. Nothing here writes attendance or
changes the automatic identity proof; it only reads and reuses it.

Who held a terminal user ID when a punch happened is judged from everything
ADD retained about that user ID, not only from the current roster:

* retained identity intervals covering the punch time (including revoked ones
  and ones recorded for another terminal UID),
* the CNIC hash the terminal itself captured with the punch,
* deleted identities (tombstones) for the same user ID or UID removed at or
  after the punch time,
* the terminal user the punch was linked to at ingest,
* the name saved with the punch, and
* when nothing covers the punch, the nearest retained interval before and
  after it (and whether the punch precedes ADD's first record of the user ID).

Any other CNIC (or another person with an unknown CNIC) in that evidence is a
conflict. Only evidence that names exactly one CNIC at punch time can supply
that CNIC; today's CNIC is used without asking only when ADD's own records at
punch time show nothing else. A punch in a break between two records of the
same terminal user with the same other CNIC takes that CNIC, as if covered.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from zk_add.crypto import decrypt_cnic, decrypt_text, mask_cnic, normalize_cnic
from zk_add.models import (
    AttendanceIdentityHistory as History,
    Connector,
    DeviceUser,
    IdentityTombstone,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

log = logging.getLogger(__name__)

CLOCK_INVALID = "CLOCK_INVALID"
TIME_IMPOSSIBLE = "TIME_IMPOSSIBLE"
TIME_REFUSALS = frozenset({CLOCK_INVALID, TIME_IMPOSSIBLE})

# Which CNIC the terminal held for this user ID when the punch happened.
MATCHES_CURRENT = "MATCHES_CURRENT"  # ADD's records at punch time show only today's CNIC
DIFFERS = "DIFFERS"  # they name exactly one other CNIC (Toheed: edited on the terminal 19 Oct)
CONFLICT = "CONFLICT"  # another CNIC or person, but not one usable CNIC: never sent by hand
CHANGED_NEAR_PUNCH = "CHANGED_NEAR_PUNCH"  # no record at punch time; the nearest one holds another CNIC
AMBIGUOUS = "AMBIGUOUS"  # records exist but do not prove today's CNIC (revoked, other UID, open conflict)
NONE = "NONE"  # no record at all (pre-history, a new hire's first punches, no CNIC then)

# Evidence notes shown to the administrator. They never carry a CNIC.
REVOKED_RECORD = "REVOKED_RECORD"
OTHER_UID_RECORD = "OTHER_UID_RECORD"
CAPTURED_CNIC_DIFFERS = "CAPTURED_CNIC_DIFFERS"
CAPTURED_CNIC_MATCHES = "CAPTURED_CNIC_MATCHES"
PREVIOUS_HOLDER_DELETED = "PREVIOUS_HOLDER_DELETED"
PUNCH_LINKED_TO_OTHER_USER = "PUNCH_LINKED_TO_OTHER_USER"
UID_DIFFERS = "UID_DIFFERS"
IDENTITY_CONFLICT_UNRESOLVED = "IDENTITY_CONFLICT_UNRESOLVED"
CNIC_CHANGED_NEAR_PUNCH = "CNIC_CHANGED_NEAR_PUNCH"
RECORDS_TRUNCATED = "RECORDS_TRUNCATED"
PUNCH_TIME_CNIC_HELD_BY_OTHER = "PUNCH_TIME_CNIC_HELD_BY_OTHER"
# ADD's first record of this terminal user ID starts after the punch: a user
# ID reused before ADD's history began cannot be detected.
PUNCH_BEFORE_FIRST_OBSERVATION = "PUNCH_BEFORE_FIRST_OBSERVATION"
# No record covers the punch, but the records just before and after it are
# the same terminal user with the same other CNIC (a break, not a change).
GAP_BRIDGED_SAME_CNIC = "GAP_BRIDGED_SAME_CNIC"
# The name saved with the punch is not today's user's name.
NAME_DIFFERS = "NAME_DIFFERS"
# Signals that stop "matches today's CNIC" without naming another CNIC.
SOFT_SIGNALS = frozenset({
    REVOKED_RECORD, OTHER_UID_RECORD, PREVIOUS_HOLDER_DELETED, PUNCH_LINKED_TO_OTHER_USER,
    UID_DIFFERS, IDENTITY_CONFLICT_UNRESOLVED,
})

# Where a punch that no record covers sits in the user ID's retained history.
NO_RECORDS = "NO_RECORDS"
BEFORE_FIRST_RECORD = "BEFORE_FIRST_RECORD"
BETWEEN_RECORDS = "BETWEEN_RECORDS"
AFTER_LAST_RECORD = "AFTER_LAST_RECORD"

# How a single other punch-time CNIC is known.
COVERING_RECORD = "COVERING_RECORD"
BRIDGED_GAP = "BRIDGED_GAP"
NEAREST_RECORD = "NEAREST_RECORD"

# Saved with an approval that sends a punch-time CNIC another active terminal
# user holds today; a later approval check stops if this appears unaccepted.
HELD_BY_OTHER_TOKEN = "HELD_BY_OTHER"

HELD_FOR_SNAPSHOT = frozenset({"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"})
KEY_CHUNK = 50
CHUNK_ROW_LIMIT = 5000
PER_KEY_LIMIT = 500
MAX_COVERING = 100


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


def safe_cnic(encrypted: str | None) -> str | None:
    """Decrypt a CNIC without ever failing the caller's transaction."""
    try:
        return normalize_cnic(decrypt_cnic(encrypted))
    except Exception:
        return None


def safe_text(encrypted: str | None) -> str | None:
    try:
        return decrypt_text(encrypted)
    except Exception:
        return None


@dataclass
class PunchEvidence:
    """Everything ADD retained about who held a terminal user ID at one punch."""

    covering: list = field(default_factory=list)
    before: History | None = None
    after: History | None = None
    tombstones: list = field(default_factory=list)
    prior_user: DeviceUser | SimpleNamespace | None = None
    truncated: bool = False
    conflict_resolved: bool = False


@dataclass(frozen=True)
class PunchTimeIdentity:
    state: str
    cnic_hash: str | None = None  # the single punch-time CNIC hash (DIFFERS)
    interval: History | None = None  # the retained interval that names it (DIFFERS)
    evidence_hashes: frozenset = frozenset()  # every CNIC hash seen at or near punch time
    unknown_holder: bool = False  # another person whose CNIC ADD never had
    signals: frozenset = frozenset()
    continuity: bool = False  # DIFFERS: the same terminal user record, only its CNIC changed
    history_ids: tuple[int, ...] = ()
    good_intervals: tuple = ()  # non-revoked intervals for the punch's UID at punch time
    sources: tuple = ()  # (cnic hash, object with cnic_encrypted) for masked display
    uid_matches_current: bool = True
    position: str | None = None  # where an uncovered punch sits among the retained records
    first_observed_at: object = None  # BEFORE_FIRST_RECORD: when ADD first recorded the user ID
    basis: str | None = None  # how ``cnic_hash`` is known (COVERING_RECORD, BRIDGED_GAP, NEAREST_RECORD)
    current_nearby: bool = False  # CHANGED_NEAR_PUNCH: the record on the other side holds today's CNIC

    @property
    def cnic(self) -> str | None:
        """The single other CNIC at or near punch time; None when it cannot be read."""
        if self.interval is None:
            return None
        value = safe_cnic(self.interval.cnic_encrypted)
        from zk_add.crypto import cnic_lookup

        return value if value and cnic_lookup(value) == self.cnic_hash else None

    @property
    def display_name(self) -> str | None:
        return safe_text(self.interval.display_name_encrypted) if self.interval is not None else None


def _covers(history: History, when, tolerance: timedelta) -> bool:
    until = ensure_utc(history.observed_until)
    return ensure_utc(history.observed_from) <= when and (
        until >= when or (not history.closed and until >= when - tolerance)
    )


def _tolerance() -> timedelta:
    return timedelta(seconds=settings.identity_snapshot_capture_tolerance_seconds)


def _name_key(value: str | None) -> str:
    """A name compared case-, punctuation- and CNIC-suffix-insensitively."""
    from zk_add.identity import clean_display_name

    cleaned = clean_display_name(value or "")
    return " ".join(re.sub(r"[\W_]+", " ", cleaned.casefold()).split())


def names_differ(saved: str | None, current: str | None) -> bool:
    saved_key, current_key = _name_key(saved), _name_key(current)
    return bool(saved_key and current_key and saved_key != current_key)


def _bridges(before: History, after: History, uid_ok) -> bool:
    """The same terminal user and UID, holding the same CNIC, on both sides of a break."""
    return bool(
        before.cnic_lookup_hash
        and before.cnic_lookup_hash == after.cnic_lookup_hash
        and before.device_user_id == after.device_user_id
        and before.uid == after.uid
        and not before.revoked and not after.revoked
        and uid_ok(before) and uid_ok(after)
    )


def _event_key(event):
    if not (event.zkt_device_id and event.device_serial and event.user_id and event.device_event_time):
        return None
    return (event.zkt_device_id, event.device_serial, event.user_id)


def _key_match(key):
    return and_(
        History.zkt_device_id == key[0],
        History.terminal_serial == key[1],
        History.user_id == key[2],
    )


def _neighbours(session: Session, windows: dict, *, before: bool) -> dict:
    """The nearest interval ending before (or starting after) each key's window."""
    tolerance = _tolerance()
    partition = (History.zkt_device_id, History.terminal_serial, History.user_id)
    if before:
        order = (History.observed_until.desc(), History.id.desc())
        bounds = [
            and_(_key_match(key), History.observed_until < low - tolerance)
            for key, (low, _high) in windows.items()
        ]
    else:
        order = (History.observed_from.asc(), History.id.asc())
        bounds = [
            and_(_key_match(key), History.observed_from > high)
            for key, (_low, high) in windows.items()
        ]
    ranked = select(
        History, func.row_number().over(partition_by=partition, order_by=order).label("rank")
    ).where(or_(*bounds)).subquery()
    nearest = aliased(History, ranked)
    return {
        (row.zkt_device_id, row.terminal_serial, row.user_id): row
        for row in session.scalars(select(nearest).where(ranked.c.rank == 1)).all()
    }


def _intervals(session: Session, windows: dict) -> tuple[dict, set]:
    """Intervals overlapping each key's own time window, bounded per key."""
    tolerance = _tolerance()
    found: dict = defaultdict(list)
    truncated: set = set()

    def window(key, low, high):
        return and_(
            _key_match(key), History.observed_from <= high,
            History.observed_until >= low - tolerance,
        )

    ordering = (
        History.zkt_device_id, History.terminal_serial, History.user_id,
        History.observed_from, History.id,
    )
    rows = session.scalars(
        select(History).where(or_(*[window(key, *bounds) for key, bounds in windows.items()]))
        .order_by(*ordering).limit(CHUNK_ROW_LIMIT + 1)
    ).all()
    if len(rows) <= CHUNK_ROW_LIMIT:
        for row in rows:
            found[(row.zkt_device_id, row.terminal_serial, row.user_id)].append(row)
        return found, truncated
    # Unusually many intervals: read each key on its own, and treat a key
    # that still overflows as unprovable rather than reading a partial list.
    for key, bounds in windows.items():
        rows = session.scalars(
            select(History).where(window(key, *bounds)).order_by(*ordering).limit(PER_KEY_LIMIT + 1)
        ).all()
        if len(rows) > PER_KEY_LIMIT:
            truncated.add(key)
        found[key] = rows[:PER_KEY_LIMIT]
    return found, truncated


def load_punch_evidence(session: Session, events, users: dict) -> dict[int, PunchEvidence]:
    """Bounded, batched evidence for many punches.

    ``users`` maps an event id to the single current active terminal user
    (or None). Queries are chunked by terminal user ID, each with its own
    time window, so a wide selection never reads a truncated interval list.
    """
    evidence = {event.id: PunchEvidence() for event in events}
    windows: dict = {}
    for event in events:
        key = _event_key(event)
        if key is None:
            continue
        when = ensure_utc(event.device_event_time)
        low, high = windows.get(key, (when, when))
        windows[key] = (min(low, when), max(high, when))
    keys = sorted(windows)
    intervals: dict = {}
    before: dict = {}
    after: dict = {}
    truncated: set = set()
    for start in range(0, len(keys), KEY_CHUNK):
        chunk = {key: windows[key] for key in keys[start:start + KEY_CHUNK]}
        found, overflow = _intervals(session, chunk)
        intervals.update(found)
        truncated |= overflow
        before.update(_neighbours(session, chunk, before=True))
        after.update(_neighbours(session, chunk, before=False))

    # Deleted identities that held the same user ID or UID.
    tombstone_keys = set()
    for event in events:
        if not event.zkt_device_id:
            continue
        tombstone_keys.add((event.zkt_device_id, "user_id", event.user_id))
        user = users.get(event.id)
        for uid in {event.uid, user.uid if user else None} - {None, ""}:
            tombstone_keys.add((event.zkt_device_id, "uid", uid))
    tombstones: dict = defaultdict(list)
    by_user_id = sorted((zkt, value) for zkt, kind, value in tombstone_keys if kind == "user_id" and value)
    by_uid = sorted((zkt, value) for zkt, kind, value in tombstone_keys if kind == "uid")
    for start in range(0, max(len(by_user_id), len(by_uid)), 200):
        conditions = []
        if by_user_id[start:start + 200]:
            conditions.append(tuple_(IdentityTombstone.zkt_device_id, IdentityTombstone.user_id).in_(
                by_user_id[start:start + 200]))
        if by_uid[start:start + 200]:
            conditions.append(tuple_(IdentityTombstone.zkt_device_id, IdentityTombstone.uid).in_(
                by_uid[start:start + 200]))
        for row in session.scalars(
            select(IdentityTombstone).where(or_(*conditions)).order_by(IdentityTombstone.id)
        ).all():
            tombstones[row.zkt_device_id].append(row)

    prior_ids = sorted({
        event.device_user_id for event in events
        if event.device_user_id and users.get(event.id) is not None
        and event.device_user_id != users[event.id].id
    })
    prior = {
        row.id: row for start in range(0, len(prior_ids), 500)
        for row in session.scalars(
            select(DeviceUser).where(DeviceUser.id.in_(prior_ids[start:start + 500]))
        ).all()
    }

    resolved: dict = {}
    tolerance = _tolerance()
    for event in events:
        item = evidence[event.id]
        user = users.get(event.id)
        key = _event_key(event)
        if key is not None:
            when = ensure_utc(event.device_event_time)
            rows = intervals.get(key, [])
            item.covering = sorted((row for row in rows if _covers(row, when, tolerance)), key=lambda row: row.id)
            item.truncated = key in truncated or len(item.covering) > MAX_COVERING
            earlier = [row for row in rows if ensure_utc(row.observed_until) < when]
            later = [row for row in rows if ensure_utc(row.observed_from) > when]
            if key in before:
                earlier.append(before[key])
            if key in after:
                later.append(after[key])
            item.before = max(earlier, key=lambda row: (ensure_utc(row.observed_until), row.id), default=None)
            item.after = min(later, key=lambda row: (ensure_utc(row.observed_from), row.id), default=None)
        if event.device_event_time is not None:
            when = ensure_utc(event.device_event_time)
            uids = {event.uid, user.uid if user else None} - {None, ""}
            item.tombstones = [
                row for row in tombstones.get(event.zkt_device_id, [])
                if (row.user_id == event.user_id or row.uid in uids)
                and row.device_serial in {event.device_serial, None, ""}
                and (user is None or row.device_user_id != user.id)
                and row.created_at is not None and ensure_utc(row.created_at) >= when
            ]
        if user is not None and event.device_user_id and event.device_user_id != user.id:
            item.prior_user = prior.get(event.device_user_id)
            if item.prior_user is None:
                # The linked terminal user row is gone: another person, CNIC unknown.
                item.prior_user = SimpleNamespace(
                    id=event.device_user_id, cnic_lookup_hash=None, cnic_encrypted=None,
                )
        if user is not None and user.identity_conflict_code:
            if user.id not in resolved:
                resolved[user.id] = _conflict_resolved(session, user)
            item.conflict_resolved = resolved[user.id]
    return evidence


def _conflict_resolved(session: Session, user: DeviceUser) -> bool:
    from zk_add.identity_conflicts import RESOLUTION_SAME_EMPLOYEE, valid_resolution_for_user
    from zk_add.models import ZKTDevice

    zkt = session.get(ZKTDevice, user.zkt_device_id)
    if zkt is None:
        return False
    try:
        resolution = valid_resolution_for_user(session, zkt=zkt, user=user)
    except Exception:
        log.exception("Identity conflict resolution could not be read for user %s", user.id)
        return False
    return bool(resolution and resolution.resolution_type == RESOLUTION_SAME_EMPLOYEE)


def classify(event, user, current_hash: str | None, evidence: PunchEvidence) -> PunchTimeIdentity:
    """Name the CNIC ADD's records show for the punch time, or why it cannot."""
    covering = evidence.covering
    signals: set = set()
    hashes: set = set()
    unknown_holder = False
    sources: list = []
    uid_matches = bool(user is None or not event.uid or not user.uid or event.uid == user.uid)
    if not uid_matches:
        signals.add(UID_DIFFERS)
    if user is not None and user.identity_conflict_code and not evidence.conflict_resolved:
        signals.add(IDENTITY_CONFLICT_UNRESOLVED)
    if evidence.truncated:
        signals.add(RECORDS_TRUNCATED)

    def uid_ok(row) -> bool:
        return not event.uid or row.uid == event.uid

    for row in covering:
        hashes.add(row.cnic_lookup_hash)
        sources.append((row.cnic_lookup_hash, row))
        if row.revoked:
            signals.add(REVOKED_RECORD)
        if not uid_ok(row):
            signals.add(OTHER_UID_RECORD)
    captured = event.captured_cnic_lookup_hash
    if captured:
        hashes.add(captured)
        signals.add(CAPTURED_CNIC_MATCHES if captured == current_hash else CAPTURED_CNIC_DIFFERS)
    for row in evidence.tombstones:
        signals.add(PREVIOUS_HOLDER_DELETED)
        if row.cnic_lookup_hash:
            hashes.add(row.cnic_lookup_hash)
            sources.append((row.cnic_lookup_hash, row))
        else:
            unknown_holder = True
    prior = evidence.prior_user
    if prior is not None:
        signals.add(PUNCH_LINKED_TO_OTHER_USER)
        if prior.cnic_lookup_hash:
            hashes.add(prior.cnic_lookup_hash)
            sources.append((prior.cnic_lookup_hash, prior))
        else:
            unknown_holder = True
    if user is not None and names_differ(event.display_name, user.display_name):
        # Soft evidence: the name saved with the punch is someone else's name.
        signals.add(NAME_DIFFERS)
    position = first_observed_at = None
    bridged: tuple = ()
    nearest = None  # the one record near the punch naming another CNIC
    current_nearby = False
    if not covering and not captured:
        before, after = evidence.before, evidence.after
        position = (
            BETWEEN_RECORDS if before is not None and after is not None
            else BEFORE_FIRST_RECORD if after is not None
            else AFTER_LAST_RECORD if before is not None
            else NO_RECORDS
        )
        if position == BEFORE_FIRST_RECORD and event.device_event_time is not None:
            first_observed_at = ensure_utc(after.observed_from)
            if first_observed_at - ensure_utc(event.device_event_time) > _tolerance():
                signals.add(PUNCH_BEFORE_FIRST_OBSERVATION)
        near = [row for row in (before, after) if row is not None and row.cnic_lookup_hash != current_hash]
        current_nearby = any(
            row is not None and row.cnic_lookup_hash == current_hash for row in (before, after)
        )
        for row in near:
            hashes.add(row.cnic_lookup_hash)
            sources.append((row.cnic_lookup_hash, row))
            signals.add(CNIC_CHANGED_NEAR_PUNCH)
        if position == BETWEEN_RECORDS and before.cnic_lookup_hash != current_hash and _bridges(
            before, after, uid_ok,
        ):
            bridged = (before, after)
            signals.add(GAP_BRIDGED_SAME_CNIC)
            signals.discard(CNIC_CHANGED_NEAR_PUNCH)
        elif len({row.cnic_lookup_hash for row in near}) == 1:
            usable = [row for row in near if not row.revoked and uid_ok(row)]
            nearest = usable[0] if len(usable) == len(near) else None
    ids = tuple(row.id for row in covering) + tuple(row.id for row in bridged)
    good = [row for row in covering if not row.revoked and uid_ok(row)] + list(bridged)
    common = dict(
        evidence_hashes=frozenset(hashes), unknown_holder=unknown_holder,
        history_ids=ids, good_intervals=tuple(good), sources=tuple(sources),
        uid_matches_current=uid_matches, position=position, first_observed_at=first_observed_at,
    )
    if RECORDS_TRUNCATED in signals:
        return PunchTimeIdentity(CONFLICT, signals=frozenset(signals), **common)

    good_hashes = {row.cnic_lookup_hash for row in good}
    if len(good_hashes) == 1 and next(iter(good_hashes)) != current_hash:
        punch_hash = next(iter(good_hashes))
        if hashes == {punch_hash} and not unknown_holder:
            chosen = max(good, key=lambda row: row.id)
            continuity = bool(
                user is not None and uid_matches and prior is None and not evidence.tombstones
                and all(row.device_user_id == user.id and row.uid == user.uid for row in good)
            )
            return PunchTimeIdentity(
                DIFFERS, cnic_hash=punch_hash, interval=chosen,
                signals=frozenset(signals), continuity=continuity,
                basis=BRIDGED_GAP if bridged else COVERING_RECORD, **common,
            )
        return PunchTimeIdentity(CONFLICT, signals=frozenset(signals), **common)
    if unknown_holder or hashes - {current_hash}:
        # One other CNIC on the nearest record and nothing else: the CNIC may
        # have changed around the punch. Two other CNICs near the punch, or
        # any other holder, is a conflict: today's CNIC is not near it at all.
        # Today's CNIC is offered only when the record on the other side of
        # the punch holds it (the records disagree); the other CNIC only from a
        # usable record. With neither, nothing can be chosen: a conflict.
        if (
            CNIC_CHANGED_NEAR_PUNCH in signals and len(hashes - {current_hash}) == 1
            and not unknown_holder
            and not signals & {CAPTURED_CNIC_DIFFERS, PREVIOUS_HOLDER_DELETED, PUNCH_LINKED_TO_OTHER_USER}
            and (current_nearby or nearest is not None)
        ):
            return PunchTimeIdentity(
                CHANGED_NEAR_PUNCH, signals=frozenset(signals),
                cnic_hash=nearest.cnic_lookup_hash if nearest is not None else None,
                interval=nearest, basis=NEAREST_RECORD if nearest is not None else None,
                current_nearby=current_nearby, **common,
            )
        return PunchTimeIdentity(CONFLICT, signals=frozenset(signals), **common)
    proven = bool(good) or captured == current_hash
    if proven and not signals & SOFT_SIGNALS:
        return PunchTimeIdentity(MATCHES_CURRENT, signals=frozenset(signals), **common)
    if covering or signals & SOFT_SIGNALS:
        return PunchTimeIdentity(AMBIGUOUS, signals=frozenset(signals), **common)
    return PunchTimeIdentity(NONE, signals=frozenset(signals), **common)


def approval_tokens(identity: PunchTimeIdentity, sent_hash: str | None, *, held_by_other: bool = False) -> frozenset:
    """``contradiction_tokens`` plus another active user holding the sent CNIC today."""
    tokens = set(contradiction_tokens(identity, sent_hash))
    if held_by_other:
        tokens.add(HELD_BY_OTHER_TOKEN)
    return frozenset(tokens)


def contradiction_tokens(identity: PunchTimeIdentity, sent_hash: str | None) -> frozenset:
    """Evidence against sending ``sent_hash``; an approval stores what it accepted.

    A revoked or other-UID record with the same CNIC is not a contradiction:
    it does not name anyone else, and a resolved duplicate-CNIC restore
    depends on that.
    """
    tokens = {f"CNIC:{value}" for value in identity.evidence_hashes if value and value != sent_hash}
    if identity.unknown_holder:
        tokens.add("UNKNOWN_HOLDER")
    for signal in (IDENTITY_CONFLICT_UNRESOLVED, RECORDS_TRUNCATED):
        if signal in identity.signals:
            tokens.add(signal)
    return frozenset(tokens)


def other_cnics_masked(identity: PunchTimeIdentity, current_hash: str | None) -> list[str]:
    """Masked CNICs (never full) of every other identity in the punch-time evidence."""
    from zk_add.crypto import cnic_lookup

    shown: dict[str, str] = {}
    for value, row in identity.sources:
        if not value or value == current_hash or value in shown:
            continue
        cnic = safe_cnic(row.cnic_encrypted)
        shown[value] = (mask_cnic(cnic) if cnic and cnic_lookup(cnic) == value else None) or "unreadable"
    return sorted(shown.values())


def punch_cnics_held_elsewhere(session: Session, pairs) -> set[tuple[str, int]]:
    """(hash, user id) pairs whose punch-time CNIC another active terminal user holds today."""
    hashes = sorted({value for value, _user_id in pairs if value})
    holders: dict = defaultdict(set)
    for start in range(0, len(hashes), 500):
        for value, user_id in session.execute(
            select(DeviceUser.cnic_lookup_hash, DeviceUser.id).where(
                DeviceUser.cnic_lookup_hash.in_(hashes[start:start + 500]),
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all():
            holders[value].add(user_id)
    return {(value, user_id) for value, user_id in pairs if holders.get(value, set()) - {user_id}}


def single_event_identity(session: Session, event, user, current_hash: str | None) -> PunchTimeIdentity:
    evidence = load_punch_evidence(session, [event], {event.id: user})[event.id]
    return classify(event, user, current_hash, evidence)


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


def auto_release_preview(
    session: Session, event, *, user=None, identity: PunchTimeIdentity | None = None,
    include_guard: bool = True,
) -> dict:
    """Would ADD's own proof release this held punch without an administrator?

    ``at_next_snapshot``: the automatic proof passes now and the database
    guard does not reject it (an unreadable guard counts as pending), so the
    next user snapshot releases it. ``awaiting_snapshot``: the punch arrived
    after the latest snapshot and the next one can still prove it (normally
    within 15 minutes). A punch whose punch-time records contradict today's
    CNIC is never "awaiting": ADD cannot release it automatically.
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
    if user is None:
        users = session.scalars(select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.lifecycle_state == "ACTIVE",
        ).limit(2)).all()
        if len(users) != 1:
            return result
        user = users[0]
    try:
        result["proof"] = bool(synced_cnic_identity_proven(session, connector, zkt, user, event))
    except Exception:
        # Unreadable retained data: the snapshot's own release fails the same
        # way, so nothing is released automatically.
        log.exception("Automatic identity proof could not be evaluated for event %s", event.id)
    if result["proof"]:
        result["database_guard"] = (
            database_guard_state(session, event=event, zkt=zkt, user=user)
            if include_guard else "NOT_CHECKED"
        )
        result["at_next_snapshot"] = result["database_guard"] != "REJECTED"
        return result
    live = event.source in {"LIVE", "LIVE_POLL"}
    provable = (
        not event.captured_cnic_lookup_hash
        or event.captured_cnic_lookup_hash == user.cnic_lookup_hash
    )
    if not live and identity is not None:
        provable = provable and identity.state in {MATCHES_CURRENT, NONE} and not contradiction_tokens(
            identity, user.cnic_lookup_hash,
        )
    snapshot_at = zkt.identity_snapshot_observed_at
    result["awaiting_snapshot"] = bool(
        provable
        and user.present and user.cnic_lookup_hash and not user.identity_conflict_code
        and zkt.snapshot_complete and zkt.identity_snapshot_stable
        and event.clock_quality == "OK"
        and (snapshot_at is None or ensure_utc(event.captured_at) > ensure_utc(snapshot_at))
        and (live or source_evidence(session, event, connector) is not None)
    )
    return result


CNIC_FORMAT_ALERT = "USER_CNIC_FORMAT_SUSPECT"


def refresh_cnic_format_alert(session: Session, connector: Connector, zkt) -> int:
    """Raise USER_CNIC_FORMAT_SUSPECT while an active user's name hides a CNIC typo.

    Such a user has no CNIC in ADD, so every punch is held. The alert names
    terminal user IDs only; the suspect digits are never parsed or stored.
    It is an INFO data-quality note with no health effect, and an unchanged
    set of users does not refresh it, so it never appears as a new warning
    inside a firmware HIL observation window.
    """
    from zk_add.identity import suspect_cnic_name
    from zk_add.service import open_alert_row, resolve_alert, upsert_alert

    suspect_user_ids = sorted(
        user.user_id
        for user in session.scalars(select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.present.is_(True),
            DeviceUser.cnic_lookup_hash.is_(None),
        )).all()
        if suspect_cnic_name(safe_text(user.machine_name_encrypted))
    )
    if suspect_user_ids:
        details = {"affected_users": len(suspect_user_ids), "user_ids": suspect_user_ids[:50]}
        current = open_alert_row(session, connector, CNIC_FORMAT_ALERT)
        if current is None or (current.details or {}).get("user_ids") != details["user_ids"] or (
            (current.details or {}).get("affected_users") != details["affected_users"]
        ):
            upsert_alert(
                session,
                connector,
                code="USER_CNIC_FORMAT_SUSPECT",  # literal: the alert policy test reads it
                severity="INFO",
                message=(
                    "A terminal user name contains a CNIC in a format ADD cannot use "
                    "(for example a letter before the 13 digits, or dashes). Their punches "
                    "stay held. Confirm the CNIC with HR and set it in ADD Users."
                ),
                details=details,
            )
    else:
        resolve_alert(session, connector, code=CNIC_FORMAT_ALERT, touch_last_seen=False)
    return len(suspect_user_ids)


def suspect_user_count(users) -> int:
    from zk_add.identity import suspect_cnic_name

    return sum(
        1 for user in users
        if not user.cnic_lookup_hash
        and suspect_cnic_name(safe_text(user.machine_name_encrypted))
    )

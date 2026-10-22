"""Account for retained attendance that has no durable ORDS outbox row.

An outbox row is ADD's delivery work item, not custody evidence. ZKT terminal
ingestion deliberately creates none for a punch that is held for identity or
quarantined for review: ADD's policy is that such a punch is not queued for
Oracle until a verified release (synced-CNIC proof, Force release or Direct
ORDS) creates the row. Hikvision and journal ingestion instead keep a
placeholder row in the held status, and so does a ZKT punch whose firmware
receipt arrived with it. Either way the punch is held by design, so it is never
shown as a delivery gap. Only a deliverable punch without an outbox row is
missing, and so is a held punch that already has a Force release or Direct
ORDS decision: both create the outbox in the decision's own transaction, so a
decided punch without one is a lost work item, not a hold.

These numbers describe ADD's own queue, not what Oracle holds. A legacy ZKT
terminal can post a punch to ORDS itself and report a firmware receipt. When
the receipt arrives with a punch that has no CNIC, ingestion writes a
FIRMWARE_RECEIPT_UNVERIFIED outbox that the worker then blocks with the event
(a held placeholder). When it arrives after the punch is already held, ADD keeps
the receipt as evidence and creates no outbox. So a held punch, with or without
an outbox row, may already be in Oracle under whatever identity the terminal
sent.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.attendance_manual_guard import HELD as MANUAL_APPROVAL_HELD_STATUSES
from zk_add.models import AttendanceEvent, AttendanceForceReleaseDecision, OrdsOutbox
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
    classify_ords_assurance_status,
    normalize_ords_status,
)

# Identity holds as the custody boundary sees them: the manual-approval guard,
# the historical sweep and Force release all treat WAITING_FOR_SNAPSHOT as held
# alongside BLOCKED_IDENTITY and QUARANTINED_IDENTITY_REUSE. The recovery
# summary's Identity held tile uses the same set.
IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES = frozenset(ORDS_IDENTITY_HELD_STATUSES) | frozenset(
    MANUAL_APPROVAL_HELD_STATUSES
)


def outbox_coverage_bucket(status: str | None, *, release_decided: bool = False) -> str:
    """Name the bucket an outbox-less, unacknowledged event belongs to.

    ``release_decided`` means a Force release or Direct ORDS decision exists
    for the event. Both approvals create the outbox in the same transaction, so
    a decided event without one is missing even if its status is held.
    """

    key = normalize_ords_status(status)
    if key in IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES:
        return "missing" if release_decided else "held"
    if classify_ords_assurance_status(key) == "PENDING":
        return "missing"
    # Non-identity quarantines (invalid time, invalid event UID, missing
    # terminal provenance, ORDS rejection, source conflict) and any state ADD
    # does not recognise. Fail-closed: never folded into held or missing.
    return "review"


def ords_outbox_coverage(session: Session) -> dict:
    """Split unacknowledged events without an outbox row by why they have none.

    One grouped anti-join replaces the former single count, so the overview
    pays for this check once. The decision table is small and indexed by
    event, so the extra outer join is a hash lookup per outbox-less row.
    """

    decided = (
        select(AttendanceForceReleaseDecision.attendance_event_id.label("event_id"))
        .distinct()
        .subquery()
    )
    has_decision = decided.c.event_id.is_not(None)
    rows = session.execute(
        select(AttendanceEvent.ords_status, has_decision, func.count(AttendanceEvent.id))
        .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
        .outerjoin(decided, decided.c.event_id == AttendanceEvent.id)
        .where(
            OrdsOutbox.id.is_(None),
            ~AttendanceEvent.ords_status.in_(tuple(ORDS_ACKNOWLEDGED_STATUSES)),
        )
        .group_by(AttendanceEvent.ords_status, has_decision)
    ).all()
    by_status: dict[str, int] = {}
    buckets = {"missing": 0, "held": 0, "review": 0}
    decided_held = 0
    for status, release_decided, count in rows:
        key = normalize_ords_status(status)
        count = int(count)
        by_status[key] = by_status.get(key, 0) + count
        bucket = outbox_coverage_bucket(key, release_decided=bool(release_decided))
        buckets[bucket] += count
        if bucket == "missing" and key in IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES:
            decided_held += count
    return {
        # Deliverable punches that ADD never queued, plus held punches that
        # already have a release decision: a real delivery gap.
        "missing_outbox": buckets["missing"],
        # Identity holds (BLOCKED_IDENTITY, QUARANTINED_IDENTITY_REUSE,
        # WAITING_FOR_SNAPSHOT) with no release decision: ADD queues nothing
        # until a verified release.
        "held_without_outbox": buckets["held"],
        # Non-identity quarantines or unknown states: preserved for review and
        # never queued.
        "review_without_outbox": buckets["review"],
        # The part of missing_outbox whose stored status is still held but
        # which has a Force release or Direct ORDS decision. Expected 0.
        "decided_held_without_outbox": decided_held,
        "without_outbox_total": sum(buckets.values()),
        "without_outbox_by_status": dict(sorted(by_status.items())),
    }

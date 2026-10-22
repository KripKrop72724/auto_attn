"""Account for retained attendance that has no durable ORDS outbox row.

An outbox row is ADD's delivery work item, not custody evidence. ZKT terminal
ingestion deliberately creates none for a punch that is held for identity or
quarantined for review: ADD's policy is that such a punch is not queued for
Oracle until a verified release (synced-CNIC proof, Force release or Direct
ORDS) creates the row. Hikvision and journal ingestion instead keep a
placeholder row in the held status. Either way the punch is held by design, so
it is never shown as a delivery gap. Only a deliverable punch without an outbox
row is missing.

These numbers describe ADD's own queue, not what Oracle holds. A legacy ZKT
terminal can post a punch to ORDS itself; when its firmware receipt reaches ADD
for a held punch without a release decision, ADD keeps the receipt as evidence
but still creates no outbox and leaves the punch held. So a held punch without
an outbox may already be in Oracle under whatever identity the terminal sent.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.attendance_manual_guard import HELD as MANUAL_APPROVAL_HELD_STATUSES
from zk_add.models import AttendanceEvent, OrdsOutbox
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
    classify_ords_assurance_status,
    normalize_ords_status,
)

# Identity holds as the custody boundary sees them: the manual-approval guard,
# the historical sweep and Force release all treat WAITING_FOR_SNAPSHOT as held
# alongside BLOCKED_IDENTITY and QUARANTINED_IDENTITY_REUSE.
IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES = frozenset(ORDS_IDENTITY_HELD_STATUSES) | frozenset(
    MANUAL_APPROVAL_HELD_STATUSES
)


def outbox_coverage_bucket(status: str | None) -> str:
    """Name the bucket an outbox-less, unacknowledged event belongs to."""

    key = normalize_ords_status(status)
    if key in IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES:
        return "held"
    if classify_ords_assurance_status(key) == "PENDING":
        return "missing"
    # Non-identity quarantines (invalid time, invalid event UID, missing
    # terminal provenance, ORDS rejection, source conflict) and any state ADD
    # does not recognise. Fail-closed: never folded into held or missing.
    return "review"


def ords_outbox_coverage(session: Session) -> dict:
    """Split unacknowledged events without an outbox row by why they have none.

    One grouped anti-join replaces the former single count, so the overview
    pays for this check once.
    """

    rows = session.execute(
        select(AttendanceEvent.ords_status, func.count(AttendanceEvent.id))
        .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
        .where(
            OrdsOutbox.id.is_(None),
            ~AttendanceEvent.ords_status.in_(tuple(ORDS_ACKNOWLEDGED_STATUSES)),
        )
        .group_by(AttendanceEvent.ords_status)
    ).all()
    by_status: dict[str, int] = {}
    for status, count in rows:
        key = normalize_ords_status(status)
        by_status[key] = by_status.get(key, 0) + int(count)
    buckets = {"missing": 0, "held": 0, "review": 0}
    for status, count in by_status.items():
        buckets[outbox_coverage_bucket(status)] += count
    return {
        # Deliverable punches that ADD never queued: a real delivery gap.
        "missing_outbox": buckets["missing"],
        # Identity holds (BLOCKED_IDENTITY, QUARANTINED_IDENTITY_REUSE,
        # WAITING_FOR_SNAPSHOT): ADD queues nothing until a verified release.
        "held_without_outbox": buckets["held"],
        # Non-identity quarantines or unknown states: preserved for review and
        # never queued.
        "review_without_outbox": buckets["review"],
        "without_outbox_total": sum(buckets.values()),
        "without_outbox_by_status": dict(sorted(by_status.items())),
    }

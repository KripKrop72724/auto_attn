"""Account for retained attendance that has no durable ORDS outbox row.

An outbox row is the delivery work item, not custody evidence. ZKT terminal
ingestion deliberately creates none for a punch that is held for identity or
quarantined for review, because nothing may reach Oracle until a verified
release (synced-CNIC proof, Force release or Direct ORDS) creates it.
Hikvision and journal ingestion instead keep a placeholder row in the held
status. Either way such a punch is held by design, so it must never be shown as
a delivery gap. Only a deliverable punch without an outbox row is missing.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.models import AttendanceEvent, OrdsOutbox
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    classify_ords_assurance_status,
    normalize_ords_status,
)


def ords_outbox_coverage(session: Session) -> dict:
    """Split unacknowledged events without an outbox row by why they have none.

    One grouped anti-join replaces the former single count, so the overview
    pays for this check once. Unknown states are fail-closed as review rather
    than hidden in either the held or the missing number.
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
    missing = held = review = 0
    for status, count in by_status.items():
        outcome = classify_ords_assurance_status(status)
        if outcome == "PENDING":
            missing += count
        elif outcome == "IDENTITY_HELD":
            held += count
        else:
            review += count
    return {
        # Deliverable punches that ADD never queued: a real delivery gap.
        "missing_outbox": missing,
        # Identity-held punches: no Oracle work item until a verified release.
        "held_without_outbox": held,
        # Quarantined or unknown states: preserved for review, never sent.
        "review_without_outbox": review,
        "without_outbox_total": missing + held + review,
        "without_outbox_by_status": dict(sorted(by_status.items())),
    }

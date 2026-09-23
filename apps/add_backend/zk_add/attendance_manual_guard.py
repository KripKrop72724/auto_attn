"""A sticky custody boundary for attendance that has ever needed identity approval."""

from sqlalchemy import event as sa_event, inspect, select
from sqlalchemy.orm import Session

HELD = {"BLOCKED_IDENTITY", "QUARANTINED_IDENTITY_REUSE", "WAITING_FOR_SNAPSHOT"}


def requires_approval(row) -> bool:
    return bool(row.manual_release_required or row.ords_status in HELD)


def decision_for(session: Session, row):
    from zk_add.models import AttendanceForceReleaseDecision

    pending = [
        value
        for value in session.new
        if isinstance(value, AttendanceForceReleaseDecision) and value.attendance_event_id == row.id
    ]
    if pending:
        return pending[-1]

    return (
        session.scalar(
            select(AttendanceForceReleaseDecision)
            .where(AttendanceForceReleaseDecision.attendance_event_id == row.id)
            .order_by(AttendanceForceReleaseDecision.id.desc())
            .limit(1)
        )
        if row.id
        else None
    )


def delivery_authorized(session: Session, row) -> bool:
    if not requires_approval(row):
        return True
    if decision_for(session, row) is not None:
        return True  # The force-delivery validator also checks its immutable proof.
    # Preserve explicit approvals already in flight before the policy upgrade.
    from zk_add.models import AttendanceSafeRepairDecision

    if any(
        isinstance(value, AttendanceSafeRepairDecision)
        and value.attendance_event_id == row.id
        and not value.actor.startswith("system:")
        for value in session.new
    ):
        return True

    prior = (
        session.scalar(
            select(AttendanceSafeRepairDecision)
            .where(
                AttendanceSafeRepairDecision.attendance_event_id == row.id,
                ~AttendanceSafeRepairDecision.actor.like("system:%"),
            )
            .limit(1)
        )
        if row.id
        else None
    )
    if prior is not None:
        return True
    from zk_add.models import AttendanceRepairItem, AttendanceRepairJob

    return (
        session.scalar(
            select(AttendanceRepairItem.id)
            .join(AttendanceRepairJob)
            .where(
                AttendanceRepairItem.attendance_event_id == row.id,
                AttendanceRepairJob.approved_at.is_not(None),
            )
            .limit(1)
        )
        is not None
    )


def generic_confirmation_allowed(session: Session, row) -> bool:
    # A UID-only firmware receipt or membership check cannot establish which
    # employee Oracle accepted for a manual identity override.
    if not requires_approval(row):
        return True
    return delivery_authorized(session, row) and decision_for(session, row) is None


@sa_event.listens_for(Session, "before_flush")
def retain_manual_approval_requirement(session, _context, _instances):
    from zk_add.models import AttendanceEvent, OrdsOutbox

    for row in session.new.union(session.dirty):
        if not isinstance(row, AttendanceEvent):
            continue
        state = inspect(row)
        prior_status = state.attrs.ords_status.history.deleted
        prior_required = state.attrs.manual_release_required.history.deleted
        if (
            row.ords_status in HELD
            or any(s in HELD for s in prior_status)
            or True in prior_required
        ):
            row.manual_release_required = True
        if row.manual_release_required and row.ords_status in {
            "PENDING",
            "IN_FLIGHT",
            "FAILED_RETRYABLE",
            "RETRYING",
            "ACKED",
            "ACKED_CHECK",
        }:
            if not delivery_authorized(session, row):
                row.ords_status = "BLOCKED_IDENTITY"
                row.oracle_confirmed_at = row.oracle_confirmation_path = None
    for row in session.new.union(session.dirty):
        if not isinstance(row, OrdsOutbox) or row.status not in {
            "PENDING",
            "IN_FLIGHT",
            "FAILED_RETRYABLE",
            "RETRYING",
            "ACKED",
            "ACKED_CHECK",
        }:
            continue
        attendance = (
            session.get(AttendanceEvent, row.attendance_event_id)
            if row.attendance_event_id
            else None
        )
        if attendance and not delivery_authorized(session, attendance):
            row.status, row.last_error = "BLOCKED_IDENTITY", "MANUAL_APPROVAL_REQUIRED"
            row.next_attempt_at = row.acknowledged_at = None

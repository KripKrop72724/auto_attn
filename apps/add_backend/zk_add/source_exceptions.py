from __future__ import annotations

import base64

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_text
from zk_add.models import (
    AuditEvent,
    Connector,
    ReconciliationCoverage,
    ReconciliationJob,
    TerminalRecordManifest,
    TerminalRecordReview,
)
from zk_add.time_utils import utc_now


EXCEPTION_DISPOSITIONS = {"INVALID_TIME", "MALFORMED"}


def exception_or_404_row(session: Session, exception_id: int) -> TerminalRecordManifest | None:
    return session.scalar(
        select(TerminalRecordManifest).where(
            TerminalRecordManifest.id == exception_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
            TerminalRecordManifest.disposition.in_(EXCEPTION_DISPOSITIONS),
        )
    )


def _latest_reviews(session: Session, manifest_ids: list[int]) -> dict[int, TerminalRecordReview]:
    if not manifest_ids:
        return {}
    rows = session.scalars(
        select(TerminalRecordReview)
        .where(TerminalRecordReview.manifest_id.in_(manifest_ids))
        .order_by(TerminalRecordReview.id.asc())
    ).all()
    return {row.manifest_id: row for row in rows}


def _coverage_by_terminal(
    session: Session, terminal_ids: list[int]
) -> dict[tuple[int, int], ReconciliationCoverage]:
    if not terminal_ids:
        return {}
    rows = session.scalars(
        select(ReconciliationCoverage)
        .where(ReconciliationCoverage.zkt_device_id.in_(terminal_ids))
        .order_by(ReconciliationCoverage.id.asc())
    ).all()
    return {(row.zkt_device_id, row.terminal_generation): row for row in rows}


def _serialize(
    row: TerminalRecordManifest,
    connector: Connector | None,
    review: TerminalRecordReview | None,
    coverage: ReconciliationCoverage | None,
) -> dict:
    committed_cursor = coverage.source_committed_cursor if coverage else 0
    return {
        "id": row.id,
        "connector_id": connector.connector_id if connector else None,
        "device_id": connector.device_id if connector else None,
        "display_name": connector.display_name if connector else None,
        "zone_id": connector.zone_id if connector else None,
        "terminal_serial": row.terminal_serial,
        "terminal_generation": row.generation,
        "ordinal": row.ordinal,
        "source_kind": row.source_kind,
        "record_size": row.record_size,
        "disposition": row.disposition,
        "error_code": row.error_code,
        "raw_timestamp": row.raw_timestamp,
        "observed_uid": row.observed_uid,
        "observed_user_id": row.observed_user_id,
        "raw_record_digest": row.raw_record_digest,
        "evidence_available": bool(row.protected_raw_record),
        "terminal_record_key": row.terminal_record_key,
        "attendance_event_id": row.attendance_event_id,
        "observed_at": row.created_at,
        "review_state": review.state if review else "OPEN",
        "reviewed_at": review.created_at if review else None,
        "reviewed_by": review.actor if review else None,
        "review_reason": review.reason if review else None,
        "source_committed_cursor": committed_cursor,
        "cursor_advanced": committed_cursor > row.ordinal,
        "oracle_action": "EXCLUDED_FAIL_CLOSED",
    }


def list_source_exceptions(
    session: Session,
    *,
    job: ReconciliationJob | None = None,
    connector_id: int | None = None,
    disposition: str | None = None,
    error_code: str | None = None,
    review_state: str | None = None,
    ordinal: int | None = None,
    cursor: int | None = None,
    limit: int = 100,
) -> dict:
    statement = select(TerminalRecordManifest).where(
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.disposition.in_(EXCEPTION_DISPOSITIONS)
    )
    if job is not None:
        statement = statement.where(
            TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
            TerminalRecordManifest.generation == job.terminal_generation,
            TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
            TerminalRecordManifest.ordinal < (job.cutoff_count or 0),
        )
    if connector_id is not None:
        statement = statement.where(TerminalRecordManifest.connector_id == connector_id)
    if disposition:
        statement = statement.where(TerminalRecordManifest.disposition == disposition)
    if error_code:
        statement = statement.where(TerminalRecordManifest.error_code == error_code)
    if ordinal is not None:
        statement = statement.where(TerminalRecordManifest.ordinal == ordinal)
    if review_state == "OPEN":
        statement = statement.where(
            ~TerminalRecordManifest.id.in_(select(TerminalRecordReview.manifest_id))
        )
    elif review_state == "REVIEWED":
        statement = statement.where(
            TerminalRecordManifest.id.in_(select(TerminalRecordReview.manifest_id))
        )
    filtered_total = session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ) or 0
    if cursor:
        statement = statement.where(TerminalRecordManifest.id < cursor)
    rows = session.scalars(
        statement.order_by(TerminalRecordManifest.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    reviews = _latest_reviews(session, [row.id for row in page])
    connectors = {
        row.id: row
        for row in session.scalars(
            select(Connector).where(
                Connector.id.in_({item.connector_id for item in page})
            )
        ).all()
    } if page else {}
    coverage = _coverage_by_terminal(session, list({row.zkt_device_id for row in page}))

    totals_statement = select(
        func.count(TerminalRecordManifest.id),
        func.sum(case((TerminalRecordManifest.disposition == "INVALID_TIME", 1), else_=0)),
        func.sum(case((TerminalRecordManifest.disposition == "MALFORMED", 1), else_=0)),
        func.count(func.distinct(TerminalRecordManifest.zkt_device_id)),
    ).where(
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.disposition.in_(EXCEPTION_DISPOSITIONS),
    )
    total, invalid_time, malformed, affected_terminals = session.execute(totals_statement).one()
    reviewed = session.scalar(
        select(func.count(func.distinct(TerminalRecordReview.manifest_id)))
    ) or 0
    return {
        "totals": {
            "all": int(total or 0),
            "open": max(0, int(total or 0) - int(reviewed)),
            "reviewed": int(reviewed),
            "invalid_time": int(invalid_time or 0),
            "malformed": int(malformed or 0),
            "affected_terminals": int(affected_terminals or 0),
        },
        "rows": [
            _serialize(
                row,
                connectors.get(row.connector_id),
                reviews.get(row.id),
                coverage.get((row.zkt_device_id, row.generation)),
            )
            for row in page
        ],
        "next_cursor": page[-1].id if len(rows) > limit and page else None,
        "filtered_total": int(filtered_total),
    }


def source_exception_detail(session: Session, row: TerminalRecordManifest) -> dict:
    connector = session.get(Connector, row.connector_id)
    reviews = session.scalars(
        select(TerminalRecordReview)
        .where(TerminalRecordReview.manifest_id == row.id)
        .order_by(TerminalRecordReview.id.asc())
    ).all()
    coverage = _coverage_by_terminal(session, [row.zkt_device_id]).get(
        (row.zkt_device_id, row.generation)
    )
    result = _serialize(row, connector, reviews[-1] if reviews else None, coverage)
    result["reviews"] = [
        {
            "review_id": review.review_id,
            "state": review.state,
            "reason": review.reason,
            "actor": review.actor,
            "created_at": review.created_at,
        }
        for review in reviews
    ]
    return result


def review_source_exception(
    session: Session,
    *,
    row: TerminalRecordManifest,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> TerminalRecordReview:
    existing = session.scalar(
        select(TerminalRecordReview).where(
            TerminalRecordReview.manifest_id == row.id,
            TerminalRecordReview.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    review = TerminalRecordReview(
        manifest_id=row.id,
        state="REVIEWED",
        reason=reason.strip(),
        actor=actor,
        idempotency_key=idempotency_key,
    )
    session.add(review)
    append_audit(
        session,
        actor=actor,
        action="TERMINAL_SOURCE_EXCEPTION_REVIEWED",
        target_type="terminal_source_record",
        target_id=str(row.id),
        outcome="SUCCESS",
        after={"state": "REVIEWED", "reason": reason.strip()},
        request_id=idempotency_key,
    )
    return review


def reveal_source_exception(
    session: Session,
    *,
    row: TerminalRecordManifest,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> dict:
    raw_b64 = decrypt_text(row.protected_raw_record)
    if not raw_b64:
        raise ValueError("Protected raw evidence is unavailable.")
    raw = base64.b64decode(raw_b64, validate=True)
    existing_audit = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "TERMINAL_SOURCE_EXCEPTION_REVEALED",
            AuditEvent.target_id == str(row.id),
            AuditEvent.request_id == idempotency_key,
        )
    )
    if existing_audit is None:
        append_audit(
            session,
            actor=actor,
            action="TERMINAL_SOURCE_EXCEPTION_REVEALED",
            target_type="terminal_source_record",
            target_id=str(row.id),
            outcome="SUCCESS",
            after={"reason": reason.strip(), "revealed_at": utc_now().isoformat()},
            request_id=idempotency_key,
        )
    return {
        "id": row.id,
        "raw_record_b64": raw_b64,
        "raw_record_hex": raw.hex(),
        "raw_record_digest": row.raw_record_digest,
        "record_size": row.record_size,
    }

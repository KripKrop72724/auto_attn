from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.models import AuditChainHead, AuditEvent
from zk_add.time_utils import utc_now


def append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str | None,
    outcome: str,
    ip_address: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    # The singleton head is the serialization point.  PostgreSQL locks it for
    # the transaction, preventing concurrent requests from producing two rows
    # with the same previous_hash.  The migration seeds it from the legacy
    # tail; create_all databases are initialized lazily here.
    head = session.scalar(select(AuditChainHead).where(AuditChainHead.id == 1).with_for_update())
    if head is None:
        previous = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
        head = AuditChainHead(
            id=1,
            last_audit_event_id=previous.id if previous else None,
            last_hash=previous.row_hash if previous else None,
            updated_at=utc_now(),
        )
        session.add(head)
        session.flush()
    previous_hash = head.last_hash
    created_at = utc_now()
    request_id = request_id or str(uuid4())
    material = json.dumps(
        {
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "outcome": outcome,
            "before": before or {},
            "after": after or {},
            "request_id": request_id,
            "previous_hash": previous_hash,
            "created_at": created_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    row = AuditEvent(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
        ip_address=ip_address,
        before=before or {},
        after=after or {},
        outcome=outcome,
        previous_hash=previous_hash,
        row_hash=hashlib.sha256(material.encode()).hexdigest(),
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    head.last_audit_event_id = row.id
    head.last_hash = row.row_hash
    head.updated_at = created_at
    return row

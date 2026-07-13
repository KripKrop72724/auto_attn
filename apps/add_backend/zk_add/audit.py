from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.models import AuditEvent
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
    previous = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    previous_hash = previous.row_hash if previous else None
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
    return row

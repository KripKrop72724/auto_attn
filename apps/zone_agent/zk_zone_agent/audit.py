from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.hashing import audit_row_hash, payload_hash
from zk_zone_agent.db import AuditLedger


class AuditLedgerWriter:
    def append(self, session: Session, record_type: str, record_id: str | int, payload: object) -> AuditLedger:
        previous = session.scalar(select(AuditLedger).order_by(AuditLedger.id.desc()).limit(1))
        previous_hash = previous.row_hash if previous else None
        row = AuditLedger(
            record_type=record_type,
            record_id=str(record_id),
            payload_hash=payload_hash(payload),
            previous_hash=previous_hash,
            row_hash=audit_row_hash(previous_hash, record_type, record_id, payload),
        )
        session.add(row)
        session.flush()
        return row


audit_ledger = AuditLedgerWriter()

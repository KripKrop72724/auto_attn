"""Bounded, audited access to retained source evidence for protocol diagnosis.

This does not reinterpret, review, correct or release any attendance. The
current model label is explicitly distinct from a model observed at capture.
"""
from __future__ import annotations

import base64
import hashlib
import json

from sqlalchemy import select

from zk_add.time_utils import ensure_utc
from zk_add.audit import append_audit
from zk_add.crypto import decrypt_text
from zk_add.models import AttendanceEvent, AuditEvent, TerminalRecordManifest, TerminalSourceEpoch


def _scope(connector):
    if connector.firmware_family != "zkt" or connector.zkt_device is None:
        raise ValueError("ZKT_SOURCE_REQUIRED")
    return (TerminalRecordManifest.connector_id == connector.id,
            TerminalRecordManifest.zkt_device_id == connector.zkt_device.id)


def list_evidence(session, connector, *, before=None, limit=20, disposition=None):
    maximum = max(1, min(limit, 50))
    query = select(TerminalRecordManifest).where(*_scope(connector))
    if before is not None:
        query = query.where(TerminalRecordManifest.id < before)
    if disposition is not None:
        query = query.where(TerminalRecordManifest.disposition == disposition)
    rows = session.scalars(query.order_by(TerminalRecordManifest.id.desc()).limit(maximum + 1)).all()
    page = rows[:maximum]
    return {"connector_id": connector.connector_id,
        "current_model": connector.zkt_device.model, "model_at_capture": "NOT_RECORDED",
        "qualification": "NOT_ASSERTED",
        "rows": [{"id": row.id, "ordinal": row.ordinal, "generation": row.generation,
            "source_epoch_id": row.source_epoch_id, "source_kind": row.source_kind,
            "canonical_source": row.canonical_source, "record_size": row.record_size,
            "raw_record_digest": row.raw_record_digest, "disposition": row.disposition,
            "declared_disposition": row.declared_disposition,
            "interpretation_version": row.interpretation_version,
            "source_claim_available": bool(row.protected_source_claim),
            "error_code": row.error_code, "evidence_available": bool(row.protected_raw_record),
            "created_at": ensure_utc(row.created_at)} for row in page],
        "next_cursor": page[-1].id if len(rows) > maximum and page else None}


def reveal_evidence(session, connector, manifest_id, *, actor, reason, idempotency_key):
    row = session.scalar(select(TerminalRecordManifest).where(*_scope(connector),
                                                            TerminalRecordManifest.id == manifest_id))
    if row is None:
        return None
    try:
        encoded = decrypt_text(row.protected_raw_record)
        if not encoded or len(encoded) > 684:
            raise ValueError("SOURCE_EVIDENCE_UNAVAILABLE")
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("SOURCE_EVIDENCE_UNAVAILABLE") from exc
    if not 1 <= len(raw) <= 512 or len(raw) != row.record_size or hashlib.sha256(raw).hexdigest() != row.raw_record_digest:
        raise ValueError("SOURCE_EVIDENCE_INTEGRITY")
    claim = None
    if row.protected_source_claim:
        try:
            claim = json.loads(decrypt_text(row.protected_source_claim))
            record = claim["record"]
            if (claim["schema_version"] != 1
                    or claim["interpretation_version"] != row.interpretation_version
                    or record["raw_record_digest"] != row.raw_record_digest
                    or record["ordinal"] != row.ordinal
                    or record["terminal_record_key"] != row.terminal_record_key
                    or record["disposition"] != row.declared_disposition):
                raise ValueError("SOURCE_CLAIM_INTEGRITY")
        except Exception as exc:
            raise ValueError("SOURCE_CLAIM_INTEGRITY") from exc
    epoch = session.get(TerminalSourceEpoch, row.source_epoch_id) if row.source_epoch_id else None
    event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
    bound = bool(event and event.connector_id == connector.id
                 and event.zkt_device_id == row.zkt_device_id and event.device_serial == row.terminal_serial)
    audit = session.scalar(select(AuditEvent.id).where(
        AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED", AuditEvent.target_id == str(row.id),
        AuditEvent.actor == actor, AuditEvent.request_id == idempotency_key).limit(1))
    if audit is None:
        append_audit(session, actor=actor, action="ZKT_SOURCE_EVIDENCE_REVEALED",
            target_type="terminal_source_record", target_id=str(row.id), outcome="SUCCESS",
            request_id=idempotency_key, after={"reason": reason.strip()})
    return {"schema_version": 1, "id": row.id, "connector_id": connector.connector_id,
        "terminal_serial": row.terminal_serial, "current_model": connector.zkt_device.model,
        "model_at_capture": "NOT_RECORDED", "qualification": "NOT_ASSERTED",
        "source_epoch": epoch.epoch_id if epoch else None, "generation": row.generation,
        "ordinal": row.ordinal, "canonical_source": row.canonical_source,
        "source_kind": row.source_kind, "record_size": row.record_size,
        "raw_record_b64": encoded, "raw_record_digest": row.raw_record_digest,
        "original_disposition": row.declared_disposition or row.disposition,
        "original_error_code": claim["record"].get("error_code") if claim else row.error_code,
        "custody_disposition": row.disposition, "custody_error_code": row.error_code,
        "interpretation_version": row.interpretation_version,
        "submitted_interpretation": claim,
        "original_encoded_time": row.raw_timestamp, "created_at": ensure_utc(row.created_at),
        "associated_event": {"id": event.id, "event_uid": event.event_uid,
            "user_id": event.user_id, "device_event_time": ensure_utc(event.device_event_time),
            "captured_at": ensure_utc(event.captured_at), "status": event.status, "punch": event.punch,
            "ords_status": event.ords_status, "oracle_confirmed_at": ensure_utc(event.oracle_confirmed_at) if event.oracle_confirmed_at else None,
            "evidence_role": "PRIOR_INTERPRETATION_NOT_INDEPENDENT_GROUND_TRUTH"} if bound else None}

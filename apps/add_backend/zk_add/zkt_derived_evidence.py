"""Bounded, append-only interpretation of already authenticated custody bytes.

These are decoder proposals, never profile, employee or Oracle authority. A
new implementation version appends a new chain; it cannot rewrite a receipt,
manifest, earlier interpretation, attendance UID or delivery intent. Callers
hold connector then work locks and commit the step with its scheduling state.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import struct

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.models import ZktCustodyWork, ZktDerivedEvidence
from zk_add.settings import settings
from zk_add.zkt_decode import (DECODER_VERSION, LIVE_SIZES, SOURCE_SIZES, DecodeError,
                               decode_live_record, decode_source)
from zk_add.zkt_packet import PACKET_MAX

INTERPRETATION_VERSION = f"{DECODER_VERSION}/derived-1"
RECORDS_PER_STEP = 128
MAX_STEPS = 2 + sum((PACKET_MAX - 8) // size for size in LIVE_SIZES) // RECORDS_PER_STEP
MAX_PLAINTEXT = 128 * 1024
MAX_CIPHERTEXT = 180 * 1024
SUPPORTED_KINDS = frozenset({"SOURCE_LEDGER", "SOURCE_RECORD", "LIVE_PACKET", "LIVE_FRAME", "PACKET_FRAGMENT"})


class DerivedEvidenceInvalid(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def input_fingerprint(work: ZktCustodyWork) -> str:
    return _digest(["zkt-derived-input-v1", work.id, work.work_key, work.connector_id,
        work.kind, work.source_manifest_id, work.terminal_serial, work.capture_epoch,
        work.decoder_profile, work.decoder_version, work.expected_bytes, work.expected_digest,
        work.evidence_revision])


def _plan(raw: bytes, kind: str) -> dict:
    source = kind in {"SOURCE_LEDGER", "SOURCE_RECORD"}
    result = {"source": source, "header": None, "framing_error": None, "layouts": []}
    if source:
        sizes, length = (sorted(SOURCE_SIZES & {len(raw)})), len(raw)
        if not sizes:
            result["framing_error"] = "SOURCE_RECORD_BOUNDARY"
    elif kind == "LIVE_FRAME":
        # The old reserved format never specified whether a header was retained.
        # Inferring framing from plausible bytes would invent missing evidence.
        result["framing_error"] = "LIVE_FRAME_CONTRACT_UNSPECIFIED"
        return result
    elif not 8 < len(raw) <= PACKET_MAX:
        result["framing_error"] = "LIVE_PACKET_BOUNDARY"
        return result
    else:
        command, checksum, session, reply = struct.unpack_from("<HHHH", raw)
        result["header"] = dict(command=command, checksum=checksum, session_id=session,
                                 reply_id=reply, checksum_verified=False, session_binding_verified=False)
        if command != 500 or not session:
            result["framing_error"] = "LIVE_PACKET_SESSION_OR_COMMAND"
            return result
        length = len(raw) - 8
        sizes = [size for size in sorted(LIVE_SIZES) if length % size == 0]
        if not sizes:
            result["framing_error"] = "LIVE_RECORD_BOUNDARY"
    result["layouts"] = [dict(size=size, total=length // size, processed=0, errors=0) for size in sizes]
    return result


def _chain_rows(session: Session, work: ZktCustodyWork, fingerprint: str,
                version: str) -> list[ZktDerivedEvidence]:
    # Only metadata and the final ciphertext are needed for continuation. Avoid
    # loading every prior encrypted batch for each step of a large packet.
    from sqlalchemy.orm import defer
    rows = session.scalars(select(ZktDerivedEvidence).options(defer(ZktDerivedEvidence.protected_evidence))
        .where(ZktDerivedEvidence.work_id == work.id,
            ZktDerivedEvidence.interpretation_version == version,
            ZktDerivedEvidence.input_fingerprint == fingerprint)
        .order_by(ZktDerivedEvidence.step_index).limit(MAX_STEPS + 1)).all()
    if len(rows) > MAX_STEPS:
        raise DerivedEvidenceInvalid("DERIVED_EVIDENCE_BOUNDS")
    previous = None
    for index, row in enumerate(rows):
        if (row.step_index != index or row.previous_digest != previous
                or (index < len(rows) - 1 and row.result != "PENDING")
                or not 0 <= row.error_count <= row.record_count <= RECORDS_PER_STEP):
            raise DerivedEvidenceInvalid("DERIVED_CHAIN_CHANGED")
        previous = row.evidence_digest
    return rows


def _unseal(row: ZktDerivedEvidence, work: ZktCustodyWork, plan: dict) -> dict:
    if not settings.pii_fernet_key:
        raise RuntimeError("CUSTODY_KEY_UNAVAILABLE")
    if len(row.protected_evidence) > MAX_CIPHERTEXT:
        raise DerivedEvidenceInvalid("DERIVED_EVIDENCE_BOUNDS")
    try:
        value = decrypt_json(row.protected_evidence)
    except (InvalidToken, ValueError) as exc:
        raise DerivedEvidenceInvalid("DERIVED_CIPHERTEXT_CHANGED") from exc
    if (_digest(value) != row.evidence_digest or value.get("schema_version") != 1
            or value.get("authority") != "UNQUALIFIED" or value.get("work_key") != work.work_key
            or value.get("input_fingerprint") != row.input_fingerprint
            or value.get("interpretation_version") != row.interpretation_version
            or value.get("step_index") != row.step_index or value.get("previous_digest") != row.previous_digest
            or value.get("result") != row.result or value.get("record_count") != row.record_count
            or value.get("error_count") != row.error_count):
        raise DerivedEvidenceInvalid("DERIVED_EVIDENCE_CHANGED")
    progress = value.get("plan")
    if (not isinstance(progress, dict) or progress.get("source") != plan["source"]
            or progress.get("header") != plan["header"] or progress.get("framing_error") != plan["framing_error"]
            or not isinstance(progress.get("layouts"), list) or len(progress["layouts"]) != len(plan["layouts"])):
        raise DerivedEvidenceInvalid("DERIVED_PLAN_CHANGED")
    for stored, expected in zip(progress["layouts"], plan["layouts"]):
        if (not isinstance(stored, dict) or stored.get("size") != expected["size"]
                or stored.get("total") != expected["total"]
                or type(stored.get("processed")) is not int or type(stored.get("errors")) is not int
                or not 0 <= stored["errors"] <= stored["processed"] <= expected["total"]):
            raise DerivedEvidenceInvalid("DERIVED_PROGRESS_CHANGED")
    return value


def derive_step(session: Session, work: ZktCustodyWork, raw: bytes, *, flush: bool = True) -> ZktDerivedEvidence:
    """Interpret at most 128 records across explicit hypothetical layouts.

    The caller has checked original ciphertext, framing and custody bindings.
    All size-compatible layouts are tried independently; a plausible layout is
    still unqualified and two plausible layouts remain ambiguous. A bad record
    retains its offset and byte digest without discarding later valid records.
    A batch owner may defer the flush only when it visits each work group once
    and flushes the evidence and work states together before reporting progress.
    """
    if (work.kind not in SUPPORTED_KINDS or len(raw) != work.expected_bytes or len(raw) > PACKET_MAX
            or hashlib.sha256(raw).hexdigest() != work.expected_digest):
        raise DerivedEvidenceInvalid("DERIVED_INPUT_CHANGED")
    fingerprint = input_fingerprint(work)
    plan = _plan(raw, work.kind)
    rows = _chain_rows(session, work, fingerprint, INTERPRETATION_VERSION)
    prior_interpretation = None
    if rows:
        prior = _unseal(rows[-1], work, plan)
        if rows[-1].result != "PENDING":
            return rows[-1]
        plan = prior["plan"]
        prior_interpretation = prior["prior_interpretation"]
    else:
        earlier = session.execute(select(ZktDerivedEvidence.id, ZktDerivedEvidence.interpretation_version,
            ZktDerivedEvidence.evidence_digest).where(ZktDerivedEvidence.work_id == work.id,
                ZktDerivedEvidence.input_fingerprint == fingerprint,
                ZktDerivedEvidence.interpretation_version != INTERPRETATION_VERSION,
                ZktDerivedEvidence.result != "PENDING").order_by(ZktDerivedEvidence.id.desc()).limit(1)).first()
        if earlier:
            prior_interpretation = dict(id=earlier.id, version=earlier.interpretation_version,
                                        evidence_digest=earlier.evidence_digest)
    if len(rows) >= MAX_STEPS:
        raise DerivedEvidenceInvalid("DERIVED_PROGRESS_BOUNDS")
    records = []
    for layout in plan["layouts"]:
        stop = min(layout["total"], layout["processed"] + RECORDS_PER_STEP - len(records))
        for index in range(layout["processed"], stop):
            size = layout["size"]
            offset = index * size + (0 if plan["source"] else 8)
            part = raw[offset:offset + size]
            evidence = dict(offset=offset, length=size, raw_digest=hashlib.sha256(part).hexdigest(),
                            error_code=None, facts=None)
            try:
                decoder = decode_source if plan["source"] else decode_live_record
                facts = asdict(decoder(part, record_size=size, offset=offset))
                for field in ("local_time", "utc_time"):
                    facts[field] = facts[field].isoformat()
                evidence["facts"] = facts
            except DecodeError as exc:
                evidence["error_code"] = str(exc)
                layout["errors"] += 1
            records.append(evidence)
            layout["processed"] += 1
        if len(records) == RECORDS_PER_STEP:
            break
    complete = all(layout["processed"] == layout["total"] for layout in plan["layouts"])
    plausible = [layout["size"] for layout in plan["layouts"] if not layout["errors"] and complete]
    result = ("PENDING" if not complete else "AMBIGUOUS_LAYOUT" if len(plausible) > 1
              else "UNQUALIFIED_FACTS" if plausible else "DECODE_REJECTED")
    value = dict(schema_version=1, authority="UNQUALIFIED", work_key=work.work_key,
        input_fingerprint=fingerprint, interpretation_version=INTERPRETATION_VERSION,
        decoder_version=DECODER_VERSION, reported_profile=work.decoder_profile,
        reported_decoder_version=work.decoder_version, source_manifest_id=work.source_manifest_id,
        input_digest=work.expected_digest, input_bytes=work.expected_bytes,
        evidence_revision=work.evidence_revision, step_index=len(rows),
        prior_interpretation=prior_interpretation,
        previous_digest=rows[-1].evidence_digest if rows else None,
        result=result, record_count=len(records), error_count=sum(bool(row["error_code"]) for row in records),
        plan=plan, plausible_layouts=plausible, records=records)
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_PLAINTEXT:
        raise DerivedEvidenceInvalid("DERIVED_EVIDENCE_BOUNDS")
    row = ZktDerivedEvidence(work_id=work.id, interpretation_version=INTERPRETATION_VERSION,
        input_fingerprint=fingerprint, step_index=value["step_index"], result=result,
        record_count=value["record_count"], error_count=value["error_count"],
        previous_digest=value["previous_digest"], evidence_digest=_digest(value),
        protected_evidence=encrypt_json(value))
    session.add(row)
    if flush:
        session.flush()
    return row


def verified_steps(session: Session, work: ZktCustodyWork, raw: bytes, *,
                   version: str = INTERPRETATION_VERSION):
    """Stream a validated chain for an internal, authorized evidence consumer.

    This is not a qualification API. Each batch stays bounded and the reader
    rechecks every protected step, not merely the metadata status projection.
    Consumers must finish this iterator before trusting chain completeness.
    """
    if len(raw) != work.expected_bytes or hashlib.sha256(raw).hexdigest() != work.expected_digest:
        raise DerivedEvidenceInvalid("DERIVED_INPUT_CHANGED")
    plan = _plan(raw, work.kind)
    rows = _chain_rows(session, work, input_fingerprint(work), version)
    for row in rows:
        yield _unseal(row, work, plan)

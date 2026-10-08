"""Bounded, read-only attendance evidence for one stored writer observation.

This is not a HIL verdict, source-chain certificate, identity qualification, or
release gate. It preserves the distinction between Oracle UID membership and
content/day verification. Only database-owned run scope is accepted; no caller
can supply receipts or booleans asserting success. Call from a clean session in
the finalizer's consistent read transaction. No protected payload leaves here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import base64
import hashlib

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from zk_add.attendance_repair import _protected_digest
from zk_add.crypto import decrypt_json, decrypt_text
from zk_add.hil_scope import HilTarget, target_matches
from zk_add.models import (
    Connector, OrdsOutbox, ReconciliationCoverage, ReconciliationJob,
    SourceTailChunk, TerminalRecordManifest, TerminalSourceEpoch,
    ZktObservationLink, ZktObservationReceipt, ZktOccurrenceAlias,
    ZktOracleContentReceipt, ZktOracleIntent, ZktOracleMembershipReceipt,
    ZktSourceAttendance,
)
from zk_add.ota import FirmwareDeployment, FirmwareHilRun, FirmwareRelease
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_custody import occurrence_id
from zk_add.zkt_oracle_delivery import MEMBERSHIP_SCOPE, TOKEN, VERIFICATION_SCOPE, verification_check
from zk_add.zkt_source_attendance import binding_event

DEFAULT_LIMIT = 128
MAX_OCCURRENCES = 512
MAX_LINKS = 16
MAX_RECEIPTS = 16
MAX_INTENT_CIPHERTEXT = 32768


def _digest(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _inside(value, start, end):
    return value is not None and start <= ensure_utc(value) <= end


def _scope(session, run, now):
    """Existing runs without a pinned epoch cannot acquire one retrospectively."""
    baseline = run.baseline or {}
    if baseline.get("profile") != "FULL_REMOTE_HIL_V1":
        return None, "FULL_WRITER_OBSERVATION_REQUIRED"
    start, end = ensure_utc(run.started_at), ensure_utc(run.ends_at)
    if end - start != timedelta(minutes=15) or now < end:
        return None, "OBSERVATION_WINDOW_INCOMPLETE_OR_INVALID"
    if (type(baseline.get("source_epoch_id")) is not int or baseline["source_epoch_id"] <= 0
            or not isinstance(baseline.get("source_epoch"), str) or not baseline["source_epoch"]):
        return None, "RUN_SOURCE_EPOCH_UNPINNED"
    connector = session.get(Connector, run.connector_id)
    deployment = session.get(FirmwareDeployment, run.deployment_id)
    release = session.get(FirmwareRelease, run.release_id)
    try:
        target = HilTarget.model_validate(run.target)
    except (ValueError, TypeError):
        return None, "RUN_TARGET_INVALID"
    from zk_add.hil_runs import _release_identity
    try:
        identity = _release_identity(release).model_dump(mode="json") if release else None
    except (ValueError, TypeError):
        identity = None
    if (connector is None or connector.firmware_family != "zkt" or not target_matches(target, connector)
            or deployment is None or deployment.connector_id != connector.id
            or deployment.release_id != run.release_id or release is None or release.version != "2.7.0"
            or release.manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or identity is None or identity != run.release_identity):
        return None, "RUN_DEVICE_OR_RELEASE_BINDING_CHANGED"
    coverage = session.scalar(select(ReconciliationCoverage).where(
        ReconciliationCoverage.coverage_id == baseline.get("coverage_id")))
    epoch = session.get(TerminalSourceEpoch, baseline["source_epoch_id"])
    job = session.get(ReconciliationJob, coverage.job_id) if coverage else None
    first = baseline.get("source_cursor")
    if (coverage is None or job is None or epoch is None or not coverage.active or epoch.state != "ACTIVE"
            or job.job_id != baseline.get("job_id") or job.connector_id != connector.id
            or coverage.zkt_device_id != connector.zkt_device.id
            or epoch.zkt_device_id != connector.zkt_device.id
            or coverage.source_epoch_id != epoch.id or job.source_epoch_id != epoch.id
            or epoch.epoch_id != baseline["source_epoch"]
            or coverage.terminal_serial != target.terminal_serial
            or coverage.terminal_generation != baseline.get("source_generation")
            or epoch.terminal_generation != baseline.get("source_generation")
            or type(first) is not int or first < 0 or not _digest(baseline.get("source_chain"))):
        return None, "RUN_SOURCE_BINDING_CHANGED"
    return (connector, coverage, epoch, start, end, first), None


def _source_custody(session, row, alias, coverage, start, end, result):
    issues = result["reasons"]
    try:
        if not row.protected_raw_record or len(row.protected_raw_record) > 2048:
            raise ValueError()
        encoded = decrypt_text(row.protected_raw_record)
        if not encoded or len(encoded) > 684:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != row.record_size or hashlib.sha256(raw).hexdigest() != row.raw_record_digest:
            raise ValueError()
    except (ValueError, TypeError, InvalidToken, RuntimeError):
        issues.append("SOURCE_RAW_CUSTODY_UNVERIFIED")
        return
    # New ordinals after the run's certified baseline are owned by tail
    # receipts. Full chain continuity remains the separate source collector's
    # responsibility; this verifies the record's committed receipt binding.
    chunks = list(session.scalars(select(SourceTailChunk).where(
        SourceTailChunk.coverage_id == coverage.id,
        SourceTailChunk.start_ordinal <= row.ordinal, SourceTailChunk.end_ordinal > row.ordinal,
    ).order_by(SourceTailChunk.id).limit(2)))
    if len(chunks) != 1:
        issues.append("SOURCE_TAIL_RECEIPT_MISSING_OR_AMBIGUOUS")
        return
    chunk = chunks[0]
    if (chunk.connector_id != row.connector_id or chunk.zkt_device_id != row.zkt_device_id
            or chunk.generation != row.generation or not _inside(chunk.committed_at, start, end)
            or chunk.record_count != chunk.end_ordinal - chunk.start_ordinal
            or not all(_digest(value) for value in (
                chunk.chunk_digest, chunk.previous_chain_digest, chunk.resulting_chain_digest))):
        issues.append("SOURCE_TAIL_RECEIPT_BINDING_CHANGED")
        return
    result["custody"] = {"kind": "SOURCE_MANIFEST_AND_TAIL", "manifest_id": row.id,
        "tail_chunk_id": chunk.id, "raw_digest": row.raw_record_digest,
        "committed_at": ensure_utc(chunk.committed_at).isoformat(), "observation_receipts": []}
    links = list(session.execute(select(ZktObservationLink, ZktObservationReceipt)
        .outerjoin(ZktObservationReceipt, ZktObservationReceipt.id == ZktObservationLink.receipt_id)
        .where(ZktObservationLink.occurrence_alias_id == alias.id)
        .order_by(ZktObservationLink.id).limit(MAX_LINKS + 1)))
    if len(links) > MAX_LINKS:
        issues.append("OBSERVATION_LINK_LIMIT")
    for link, receipt in links[:MAX_LINKS]:
        if (receipt is None or receipt.connector_id != row.connector_id
                or receipt.terminal_serial != row.terminal_serial or receipt.raw_digest != row.raw_record_digest
                or not _inside(receipt.committed_at, start, end)
                or not _digest(receipt.payload_digest) or link.proof_kind != "EXACT_CANONICAL_SOURCE_BYTES"):
            issues.append("OBSERVATION_RECEIPT_BINDING_CHANGED")
        else:
            result["custody"]["observation_receipts"].append({"id": receipt.id,
                "receipt_id": receipt.receipt_id, "payload_digest": receipt.payload_digest})


def _oracle(session, connector, alias, event, start, end, result):
    issues = result["reasons"]
    intents = list(session.scalars(select(ZktOracleIntent).where(
        ZktOracleIntent.attendance_event_id == event.id).limit(2)))
    if len(intents) != 1:
        issues.append("ORACLE_INTENT_MISSING_OR_AMBIGUOUS")
        return
    intent = intents[0]
    outbox = session.get(OrdsOutbox, intent.outbox_id)
    if (intent.connector_id != connector.id or intent.occurrence_alias_id != alias.id
            or outbox is None or outbox.attendance_event_id != event.id
            or intent.verification_scope not in {MEMBERSHIP_SCOPE, VERIFICATION_SCOPE}
            or not _inside(intent.created_at, start, end)):
        issues.append("ORACLE_INTENT_BINDING_CHANGED")
        return
    if (not _inside(intent.prepared_at, start, end) or not _digest(intent.payload_digest)
            or not intent.protected_payload or not intent.protected_check):
        issues.append("ORACLE_INTENT_NOT_PREPARED")
        return
    try:
        if max(len(intent.protected_payload), len(intent.protected_check)) > MAX_INTENT_CIPHERTEXT:
            raise ValueError()
        payload, check = decrypt_json(intent.protected_payload), decrypt_json(intent.protected_check)
        stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")) if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or not isinstance(check, dict)
                or _protected_digest(payload) != intent.payload_digest
                or check != verification_check(payload, intent.verification_scope)
                or payload.get("event_uid") != event.event_uid
                or payload.get("device_serial") != event.device_serial
                or payload.get("zone_id") != connector.zone_id or payload.get("device_id") != connector.device_id
                or payload.get("user_id") != event.user_id
                or stamp is None or stamp.tzinfo is None
                or ensure_utc(stamp) != ensure_utc(event.device_event_time)
                or payload.get("status") != event.status or payload.get("punch") != event.punch
                or payload.get("raw_punch") != ("T" if event.raw_punch else "F")):
            raise ValueError()
    except (AttributeError, KeyError, ValueError, TypeError, InvalidToken, RuntimeError):
        issues.append("ORACLE_FROZEN_PAYLOAD_UNVERIFIED")
        return
    result["intent"] = {"id": intent.id, "outbox_id": outbox.id,
        "verification_scope": intent.verification_scope, "payload_digest": intent.payload_digest}
    expected_model = ZktOracleMembershipReceipt if intent.verification_scope == MEMBERSHIP_SCOPE else ZktOracleContentReceipt
    for model in (ZktOracleMembershipReceipt, ZktOracleContentReceipt):
        receipts = list(session.scalars(select(model).where(model.intent_id == intent.id)
            .order_by(model.id).limit(MAX_RECEIPTS + 1)))
        if len(receipts) > MAX_RECEIPTS:
            issues.append("ORACLE_RECEIPT_LIMIT")
        for receipt in receipts[:MAX_RECEIPTS]:
            request_digest = check["request_digest"] if model is ZktOracleMembershipReceipt else _protected_digest(check)
            token = receipt.response_digest if model is ZktOracleMembershipReceipt else receipt.content_token
            if (model is not expected_model or receipt.verification_scope != intent.verification_scope
                    or receipt.event_uid != event.event_uid or receipt.payload_digest != intent.payload_digest
                    or receipt.request_digest != request_digest or not _digest(token)
                    or type(receipt.claim_attempt) is not int or receipt.claim_attempt < 1
                    or not _inside(receipt.verified_at, start, end)
                    or ensure_utc(receipt.verified_at) < ensure_utc(intent.prepared_at)):
                issues.append("ORACLE_RECEIPT_BINDING_CHANGED")
                continue
            membership = model is ZktOracleMembershipReceipt
            result["oracle_receipts"].append({"table": model.__tablename__, "id": receipt.id,
                "receipt_id": receipt.receipt_id, "intent_id": intent.id, "event_id": event.id,
                "verification_scope": receipt.verification_scope, "payload_digest": receipt.payload_digest,
                "request_digest": receipt.request_digest,
                "verified_at": ensure_utc(receipt.verified_at).isoformat(),
                "oracle_uid_membership": "RECORDED",
                "oracle_raw_content_and_day_times": "NOT_ASSERTED" if membership else "RECORDED",
                "response_digest" if membership else "content_token": token})
    if not result["oracle_receipts"]:
        issues.append("TYPED_ORACLE_RECEIPT_MISSING")
    elif len(result["oracle_receipts"]) != 1:
        issues.append("TYPED_ORACLE_RECEIPT_AMBIGUOUS")


def _occurrence(session, connector, coverage, epoch, start, end, first, row):
    result = {"manifest_id": row.id, "ordinal": row.ordinal, "occurrence_id": None,
        "observed_at": ensure_utc(row.created_at).isoformat(), "custody": None,
        "attendance": None, "intent": None, "oracle_receipts": [], "reasons": []}
    issues = result["reasons"]
    if (row.zkt_device_id != connector.zkt_device.id or row.terminal_serial != coverage.terminal_serial
            or row.generation != epoch.terminal_generation or row.ordinal < first
            or not _digest(row.raw_record_digest)):
        issues.append("SOURCE_OCCURRENCE_BINDING_CHANGED")
        return result
    aliases = list(session.scalars(select(ZktOccurrenceAlias).where(
        ZktOccurrenceAlias.manifest_id == row.id).limit(2)))
    if len(aliases) != 1:
        issues.append("OCCURRENCE_ALIAS_MISSING_OR_AMBIGUOUS")
        return result
    alias = aliases[0]
    if ((alias.zkt_device_id, alias.source_epoch_id, alias.ordinal, alias.raw_digest) != (
            row.zkt_device_id, epoch.id, row.ordinal, row.raw_record_digest)
            or alias.occurrence_id != occurrence_id(row.terminal_serial, epoch.epoch_id, row.ordinal, row.raw_record_digest)):
        issues.append("OCCURRENCE_ALIAS_BINDING_CHANGED")
        return result
    result["occurrence_id"] = alias.occurrence_id
    _source_custody(session, row, alias, coverage, start, end, result)
    links = list(session.scalars(select(ZktSourceAttendance).where(
        ZktSourceAttendance.occurrence_alias_id == alias.id).limit(2)))
    if len(links) != 1:
        issues.append("LOGICAL_ATTENDANCE_MISSING_OR_AMBIGUOUS")
        return result
    if links[0].canonical_alias_id != alias.id:
        issues.append("RECOVERY_ALIAS_IS_NOT_NEW_OBSERVED_ATTENDANCE")
        return result
    try:
        event = binding_event(session, connector, alias)
    except (ValueError, TypeError, InvalidToken, RuntimeError):
        event = None
    if event is None or not _inside(event.captured_at, start, end):
        issues.append("LOGICAL_ATTENDANCE_BINDING_UNVERIFIED")
        return result
    result["attendance"] = {"event_id": event.id, "event_uid": event.event_uid,
        "source_attendance_id": links[0].id, "captured_at": ensure_utc(event.captured_at).isoformat(),
        "profile_qualification": "NOT_ASSERTED", "identity_qualification": "NOT_ASSERTED"}
    _oracle(session, connector, alias, event, start, end, result)
    result["reasons"] = sorted(set(issues))
    return result


def collect_writer_attendance(session, run_id: str, *, now: datetime | None = None,
                              limit: int = DEFAULT_LIMIT) -> dict:
    """Return inspectable binding evidence; never mutate or finalize the run.

    `observed_at` is ADD source-custody receipt time, not a terminal-clock claim.
    Rows with no attendance or Oracle result are retained as explicit gaps.
    An overflow returns a bounded prefix with an explicit limit reason; it is
    never a complete inventory. The caller must also collect journal inventory,
    source-chain coverage, recovery controls, telemetry, and alert history.
    """
    sampled = now or utc_now()
    if sampled.tzinfo is None or type(limit) is not int or not 1 <= limit <= MAX_OCCURRENCES:
        raise ValueError("WRITER_EVIDENCE_TIME_OR_LIMIT_INVALID")
    result = {"schema_version": 1, "run_id": run_id, "collected_at": sampled.isoformat(),
        "scope": None, "occurrences": [], "truncated": False, "reasons": [],
        "limits": {"occurrences": limit, "observation_links_per_occurrence": MAX_LINKS,
                   "receipts_per_kind_per_occurrence": MAX_RECEIPTS},
        "hil_verdict": "NOT_EVALUATED", "source_chain": "NOT_ASSERTED",
        "journal_inventory": "NOT_ASSERTED", "physical_qualification": "NOT_PERFORMED"}
    # Autoflush would turn a read helper into a mutation. Also reject dirty
    # identity-map objects instead of trusting caller-edited, uncommitted facts.
    if session.new or session.dirty or session.deleted:
        result["reasons"] = ["UNCOMMITTED_SESSION_STATE"]
        return result
    with session.no_autoflush:
        run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id))
        if run is None:
            result["reasons"] = ["HIL_RUN_NOT_FOUND"]
            return result
        scoped, reason = _scope(session, run, sampled)
        if reason:
            result["reasons"] = [reason]
            return result
        connector, coverage, epoch, start, end, first = scoped
        result["scope"] = {"connector_id": connector.connector_id, "target": dict(run.target),
            "release_identity": dict(run.release_identity), "coverage_id": coverage.coverage_id,
            "source_epoch_id": epoch.id, "source_epoch": epoch.epoch_id, "first_ordinal": first,
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "time_basis": "ADD_SOURCE_CUSTODY_RECEIPT"}
        rows = list(session.scalars(select(TerminalRecordManifest).where(
            TerminalRecordManifest.connector_id == connector.id,
            TerminalRecordManifest.source_epoch_id == epoch.id,
            TerminalRecordManifest.canonical_source.is_(True),
            TerminalRecordManifest.created_at >= start, TerminalRecordManifest.created_at <= end,
        ).order_by(TerminalRecordManifest.ordinal, TerminalRecordManifest.id).limit(limit + 1)))
        result["truncated"] = len(rows) > limit
        if result["truncated"]:
            result["reasons"].append("SOURCE_OCCURRENCE_LIMIT")
        if not rows:
            result["reasons"].append("NO_SOURCE_OCCURRENCES_IN_WINDOW")
        for row in rows[:limit]:
            item = _occurrence(session, connector, coverage, epoch, start, end, first, row)
            result["occurrences"].append(item)
            result["reasons"].extend(item["reasons"])
        result["reasons"] = sorted(set(result["reasons"]))
        return result

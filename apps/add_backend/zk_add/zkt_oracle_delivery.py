"""ADD-owned delivery with a frozen, explicit verification scope.

Registration is internal to guarded canonical occurrence creation. No device
payload, version string or connector toggle registers an intent. Existing
event UIDs/Oracle keys are never migrated or changed by this module.
The versioned reader checks every transmitted field stored by the Oracle raw
table, and independently accounts for the daily punch-time projection. Business
status, leave and payroll calculations are outside this delivery proof. The
experimental membership contract uses the installed Oracle interface and
records only UID presence in a separate receipt. It never downgrades a v2 intent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

import httpx
from sqlalchemy import select

from zk_add.attendance_repair import _ords_request, _protected_digest
from zk_add.crypto import decrypt_cnic, decrypt_json, encrypt_json
from zk_add.db import session_scope
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, ZktOccurrenceAlias,
                           ZktOracleContentReceipt, ZktOracleMembershipReceipt, ZktOracleIntent,
                           TerminalSourceEpoch, TerminalRecordManifest)
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import source_occurrence_delivery_hold, occurrence_id, occurrence_attendance_id
from zk_add.identity_states import VERIFIED_IDENTITY_RESOLUTION_STATUSES

TOKEN = re.compile(r"^[a-f0-9]{64}$")
CONFLICTS = frozenset({"MISMATCH", "IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION", "CHANGED",
                       "IDENTITY_HOLD", "DOWNSTREAM_HOLD"})
VERIFICATION_SCOPE = "ORACLE_RAW_DAY_TIMES_V2"
MEMBERSHIP_SCOPE = "ORACLE_UID_MEMBERSHIP_V1"
PROJECTION_FIELDS = ("event_uid", "zone_id", "device_id", "device_serial", "user_id",
                     "employee_name", "cnic", "timestamp", "raw_punch", "capturetype", "trust_status")


def projection_check(payload):
    """Declare Oracle's timestamp/NUMBER(10,3) representation explicitly.

    The original payload (including fields Oracle does not store) remains frozen
    independently. Null clock difference is distinct from a measured zero.
    """
    projection = {key: payload[key] for key in PROJECTION_FIELDS}
    try:
        stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
        projection["timestamp"] = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        drift = payload["clockdiff"]
        if drift is None:
            projection["clockdiff"] = None
        else:
            if isinstance(drift, bool):
                raise ValueError("boolean clock")
            decimal = Decimal(str(drift))
            if not decimal.is_finite():
                raise ValueError("nonfinite clock")
            decimal = decimal.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            if abs(decimal) >= Decimal("10000000"):
                raise ValueError("clock overflow")
            projection["clockdiff"] = format(decimal, ".3f")
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise EvidenceChanged("ZKT_ORACLE_PROJECTION_UNREPRESENTABLE") from exc
    return {"contract_version": "2", "verification_scope": VERIFICATION_SCOPE,
            "request_digest": _protected_digest(payload), "projection": projection}


class EvidenceChanged(ValueError):
    pass


@dataclass(frozen=True)
class MembershipProof:
    event_uid: str
    payload_digest: str
    request_digest: str
    response_digest: str


def verification_check(payload, scope):
    if scope == VERIFICATION_SCOPE:
        return projection_check(payload)
    if scope == MEMBERSHIP_SCOPE:
        return {"contract_version": "1", "verification_scope": MEMBERSHIP_SCOPE,
                "request_digest": _protected_digest(payload), "event_uids": [payload["event_uid"]]}
    raise EvidenceChanged("ZKT_ORACLE_VERIFICATION_SCOPE_UNKNOWN")


def _occurrence_bound(session, connector, alias):
    if source_occurrence_delivery_hold(session, connector, alias.occurrence_id):
        return False
    epoch = session.get(TerminalSourceEpoch, alias.source_epoch_id)
    manifest = session.get(TerminalRecordManifest, alias.manifest_id)
    return bool(epoch and manifest and epoch.zkt_device_id == alias.zkt_device_id
        and epoch.terminal_generation == manifest.generation
        and alias.occurrence_id == occurrence_id(manifest.terminal_serial, epoch.epoch_id,
                                                alias.ordinal, alias.raw_digest))


def register_intent(session, *, connector, event, outbox, alias, verification_scope=VERIFICATION_SCOPE):
    """Register only a new occurrence UID, inside its attendance transaction.

    The caller still owns authorized source decoding and identity evidence. An
    association is not such proof. Registration alone cannot resolve identity
    or permit an Oracle send; claim-time policy is independently revalidated.
    """
    if verification_scope not in {VERIFICATION_SCOPE, MEMBERSHIP_SCOPE}:
        raise ValueError("ZKT_ORACLE_VERIFICATION_SCOPE_UNKNOWN")
    if (not connector.zkt_custody_enabled or connector.firmware_family != "zkt"
            or event.connector_id != connector.id or outbox.attendance_event_id != event.id
            or occurrence_attendance_id(session, connector, alias) != event.id or event.event_uid != alias.occurrence_id
            or not _occurrence_bound(session, connector, alias)):
        raise ValueError("ZKT_ORACLE_OCCURRENCE_BINDING")
    prior = session.scalar(select(ZktOracleIntent).where(ZktOracleIntent.attendance_event_id == event.id))
    if prior:
        if (prior.outbox_id, prior.occurrence_alias_id, prior.connector_id, prior.verification_scope) != (
                outbox.id, alias.id, connector.id, verification_scope):
            raise ValueError("ZKT_ORACLE_INTENT_CONFLICT")
        return prior
    intent = ZktOracleIntent(outbox_id=outbox.id, attendance_event_id=event.id,
        occurrence_alias_id=alias.id, connector_id=connector.id, verification_scope=verification_scope)
    session.add(intent)
    session.flush()
    return intent


def _current(session, intent, row):
    from zk_add.attendance_manual_guard import decision_for, delivery_authorized
    from zk_add.attendance_recovery import _terminal_provenance_verified
    from zk_add.attendance_safe_repair import delivery_proof_valid
    from zk_add.service import oracle_payload, attendance_device_time_is_plausible

    if not settings.pii_fernet_key or not settings.pii_lookup_key:
        raise RuntimeError("ZKT_ORACLE_KEYS_UNAVAILABLE")

    event = session.get(AttendanceEvent, intent.attendance_event_id)
    connector = session.get(Connector, intent.connector_id)
    alias = session.get(ZktOccurrenceAlias, intent.occurrence_alias_id)
    if (not event or not connector or not alias or connector.firmware_family != "zkt"
            or row.id != intent.outbox_id or row.attendance_event_id != event.id
            or event.connector_id != connector.id or occurrence_attendance_id(session, connector, alias) != event.id
            or event.event_uid != alias.occurrence_id or not connector.zkt_device
            or not _occurrence_bound(session, connector, alias)
            or not _terminal_provenance_verified(event, connector)
            or not delivery_authorized(session, event) or decision_for(session, event) is not None
            or event.identity_resolution_status not in VERIFIED_IDENTITY_RESOLUTION_STATUSES
            or not delivery_proof_valid(session, event, connector)
            or event.clock_quality == "INVALID"
            or not attendance_device_time_is_plausible(event.device_event_time, event.captured_at)):
        raise EvidenceChanged("ZKT_ORACLE_CURRENT_EVIDENCE_CHANGED")
    cnic = decrypt_cnic(event.cnic_encrypted)
    if not cnic:
        raise EvidenceChanged("ZKT_ORACLE_IDENTITY_UNAVAILABLE")
    payload = oracle_payload(connector, connector.zkt_device, event, cnic)
    check = verification_check(payload, intent.verification_scope)
    return event, payload, check


def _hold(row, event, reason):
    row.status = event.ords_status = "QUARANTINED_IDENTITY_CONFLICT"
    row.next_attempt_at = None
    row.last_error = reason
    row.acknowledged_at = event.oracle_confirmed_at = event.oracle_confirmation_path = None


def _retry(row, event, reason="ZKT_ORACLE_CONTENT_VERIFICATION_PENDING"):
    row.status = event.ords_status = "FAILED_RETRYABLE"
    row.last_error = reason
    row.next_attempt_at = utc_now() + timedelta(seconds=min(600, 2 ** min(row.attempt_count, 9)))
    row.acknowledged_at = event.oracle_confirmed_at = event.oracle_confirmation_path = None


def split_claims(claims):
    """A retained intent always owns its route, even after a feature rollback."""
    ordinary, content = [], []
    if not claims:
        return ordinary, content
    with session_scope() as session:
        intents = {row.outbox_id: row for row in session.scalars(select(ZktOracleIntent).where(
            ZktOracleIntent.outbox_id.in_([claim[0] for claim in claims]))).all()}
        for claim in claims:
            intent = intents.get(claim[0])
            if intent is None:
                ordinary.append(claim)
                continue
            row = session.scalar(select(OrdsOutbox).where(OrdsOutbox.id == intent.outbox_id).with_for_update())
            if row is None or row.status != "IN_FLIGHT":
                continue
            event = session.get(AttendanceEvent, intent.attendance_event_id)
            try:
                event, payload, check = _current(session, intent, row)
                if payload != claim[1] or claim[2] != intent.connector_id:
                    raise EvidenceChanged("ZKT_ORACLE_CLAIM_CHANGED")
                if intent.payload_digest is None:
                    if intent.protected_payload is not None or intent.protected_check is not None:
                        raise EvidenceChanged("ZKT_ORACLE_PARTIAL_INTENT")
                    payload_digest = _protected_digest(payload)
                    protected_payload, protected_check = encrypt_json(payload), encrypt_json(check)
                    # No partial frozen intent survives failed encryption.
                    intent.payload_digest = payload_digest
                    intent.protected_payload, intent.protected_check = protected_payload, protected_check
                    intent.prepared_at = utc_now()
                elif (intent.payload_digest != _protected_digest(payload)
                        or decrypt_json(intent.protected_payload) != payload
                        or decrypt_json(intent.protected_check) != check):
                    raise EvidenceChanged("ZKT_ORACLE_FROZEN_EVIDENCE_CHANGED")
            except EvidenceChanged:
                # Never fall back to ordinary UID-only delivery on a key,
                # ownership, identity or immutable-payload failure.
                if event is not None:
                    _hold(row, event, "ZKT_ORACLE_EVIDENCE_UNAVAILABLE_OR_CHANGED")
                else:
                    row.status, row.next_attempt_at, row.last_error = "QUARANTINED_IDENTITY_CONFLICT", None, "ZKT_ORACLE_EVENT_MISSING"
                continue
            except Exception:
                if event is not None:
                    _retry(row, event)
                continue
            content.append({"row_id": row.id, "intent_id": intent.id, "attempt": row.attempt_count,
                            "payload_digest": intent.payload_digest, "payload": payload, "check": check})
    return ordinary, content


def _owned(session, claim):
    row = session.scalar(select(OrdsOutbox).where(OrdsOutbox.id == claim["row_id"]).with_for_update())
    intent = session.get(ZktOracleIntent, claim["intent_id"])
    if (row is None or intent is None or row.status != "IN_FLIGHT"
            or row.attempt_count != claim["attempt"] or row.id != intent.outbox_id
            or intent.payload_digest != claim["payload_digest"]):
        return None
    return row, intent


def reserve_post(claim):
    """Persist send intent and revalidate before I/O; never hold a lock over it."""
    with session_scope() as session:
        owned = _owned(session, claim)
        if owned is None:
            return False
        row, intent = owned
        try:
            event, payload, check = _current(session, intent, row)
            if (payload != claim["payload"] or check != claim["check"]
                    or decrypt_json(intent.protected_payload) != payload
                    or decrypt_json(intent.protected_check) != check):
                return False
        except EvidenceChanged:
            return False
        intent.post_attempts += 1
        intent.last_post_at = utc_now()
    return True


async def verify(claim):
    scope = claim["check"].get("verification_scope")
    if scope == MEMBERSHIP_SCOPE:
        return await verify_membership(claim)
    if scope != VERIFICATION_SCOPE:
        return "UNKNOWN", None
    response = await _ords_request("raw-captures/delivery-v2/check", payload=claim["check"])
    if not isinstance(response, dict):
        return "UNKNOWN", None
    rows = response.get("results")
    if (response.get("success") is not True or response.get("contract_version") != "2"
            or response.get("verification_scope") != VERIFICATION_SCOPE
            or response.get("request_digest") != claim["check"]["request_digest"]
            or not isinstance(rows, list) or len(rows) != 1):
        return "UNKNOWN", None
    row = rows[0]
    if not isinstance(row, dict) or row.get("event_uid") != claim["payload"]["event_uid"]:
        return "UNKNOWN", None
    classification, token = row.get("classification"), row.get("current_content_token")
    if not isinstance(classification, str) or not isinstance(token, str) or not TOKEN.fullmatch(token):
        return "UNKNOWN", None
    raw, downstream = row.get("raw_projection_verified"), row.get("downstream_status")
    if classification == "MATCH":
        required = "RAW_ONLY" if claim["payload"]["raw_punch"] == "T" else "MATCH"
        if raw is not True or downstream != required:
            return "UNKNOWN", None
    elif classification in {"MISSING", "MISMATCH", "CROSS_DEVICE_UID_COLLISION"}:
        if raw is not False or downstream != "NOT_VERIFIED":
            return "UNKNOWN", None
    elif classification in {"DOWNSTREAM_PENDING", "IDENTITY_HOLD", "DOWNSTREAM_HOLD"}:
        if raw is not True or downstream != "NOT_VERIFIED":
            return "UNKNOWN", None
    else:
        return "UNKNOWN", None
    return classification, token


async def _membership_request(uid):
    # This is the installed ordinary attendance reader, not the administrator
    # repair service. Experimental delivery requires no repair credentials.
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.ords_timeout_seconds, headers={
                "X-API-Username": settings.ords_username, "X-API-Password": settings.ords_password,
        }) as client:
            response = await client.post(settings.ords_base_url.rstrip("/") + "/raw-captures/check",
                                         json={"event_uids": [uid]})
        return response.json() if response.status_code == 200 else None
    except (httpx.RequestError, ValueError):
        return None


async def verify_membership(claim):
    """Use only the existing read-only membership route, with no content claim.

    This path is chosen when the immutable intent is registered, never as a
    response to v2 failure. A successful POST alone cannot settle the record.
    """
    from zk_add.worker import ords_membership_missing
    uid = claim["payload"]["event_uid"]
    check = claim["check"]
    if check != verification_check(claim["payload"], MEMBERSHIP_SCOPE):
        return "UNKNOWN", None
    response = await _membership_request(uid)
    if (not isinstance(response, dict) or any(type(response.get(field)) is not int
            for field in ("received_count", "missing_count", "existing_count"))):
        return "UNKNOWN", None
    missing = ords_membership_missing(200, response, {uid})
    if missing is None:
        return "UNKNOWN", None
    if uid in missing:
        return "MISSING", None
    normalized = {field: response[field] for field in (
        "success", "received_count", "existing_count", "missing_count", "missing_event_uids")}
    proof = MembershipProof(uid, claim["payload_digest"], check["request_digest"], _protected_digest(normalized))
    return "UID_PRESENT", proof


def persist_result(claim, classification, token=None):
    with session_scope() as session:
        owned = _owned(session, claim)
        if owned is None:
            return
        row, intent = owned
        event = session.get(AttendanceEvent, intent.attendance_event_id)
        if event is None:
            return
        try:
            event, payload, check = _current(session, intent, row)
            if (payload != claim["payload"] or check != claim["check"]
                    or decrypt_json(intent.protected_payload) != payload
                    or decrypt_json(intent.protected_check) != check):
                classification = "CHANGED"
        except EvidenceChanged:
            classification = "CHANGED"
        except Exception:
            classification = "UNKNOWN"
        if (classification == "UID_PRESENT" and intent.verification_scope == MEMBERSHIP_SCOPE
                and isinstance(token, MembershipProof) and token.event_uid == event.event_uid
                and token.payload_digest == intent.payload_digest
                and token.request_digest == claim["check"]["request_digest"]
                and isinstance(token.response_digest, str)
                and TOKEN.fullmatch(token.response_digest)):
            receipt = session.scalar(select(ZktOracleMembershipReceipt).where(
                ZktOracleMembershipReceipt.intent_id == intent.id,
                ZktOracleMembershipReceipt.payload_digest == intent.payload_digest))
            if receipt is None:
                session.add(ZktOracleMembershipReceipt(intent_id=intent.id, event_uid=event.event_uid,
                    payload_digest=intent.payload_digest, request_digest=token.request_digest,
                    response_digest=token.response_digest, verification_scope=MEMBERSHIP_SCOPE,
                    claim_attempt=claim["attempt"]))
            now = utc_now()
            row.status = event.ords_status = "ACKED_CHECK"
            row.acknowledged_at = event.oracle_confirmed_at = now
            event.oracle_confirmation_path = "ADD_ZKT_UID_ONLY_V1"
            row.next_attempt_at = row.last_error = None
            row.last_http_status = 200
            # Content/day verification fields are deliberately untouched.
            session.flush()
        elif (classification == "MATCH" and intent.verification_scope == VERIFICATION_SCOPE
                and isinstance(token, str) and TOKEN.fullmatch(token)):
            receipt = session.scalar(select(ZktOracleContentReceipt).where(
                ZktOracleContentReceipt.intent_id == intent.id,
                ZktOracleContentReceipt.payload_digest == intent.payload_digest,
                ZktOracleContentReceipt.content_token == token))
            if receipt is None:
                receipt = ZktOracleContentReceipt(intent_id=intent.id, event_uid=event.event_uid,
                    payload_digest=intent.payload_digest, request_digest=_protected_digest(claim["check"]),
                    verification_scope=VERIFICATION_SCOPE,
                    content_token=token, claim_attempt=claim["attempt"])
                session.add(receipt)
            now = utc_now()
            row.status = event.ords_status = "ACKED_CHECK"
            row.acknowledged_at = event.oracle_confirmed_at = now
            event.oracle_confirmation_path = "ADD_ZKT_PROJECTION_V2"
            row.next_attempt_at = row.last_error = None
            row.last_http_status = 200
            # The receipt and completion state commit together, or not at all.
            session.flush()
        elif classification in {"IDENTITY_HOLD", "DOWNSTREAM_HOLD"}:
            _hold(row, event, "ZKT_ORACLE_" + classification)
        elif classification in CONFLICTS:
            _hold(row, event, "ZKT_ORACLE_CONTENT_CONFLICT" if classification != "CHANGED" else "ZKT_ORACLE_EVIDENCE_CHANGED")
        elif classification == "DOWNSTREAM_PENDING":
            _retry(row, event, "ZKT_ORACLE_DOWNSTREAM_VERIFICATION_PENDING")
        elif classification == "REJECTED":
            _hold(row, event, "ZKT_ORACLE_POST_REJECTED")
        else:
            _retry(row, event)


async def deliver(claims, *, concurrency):
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 16)))

    async def one(claim):
        classification, token = "UNKNOWN", None
        async with semaphore:
            try:
                classification, token = await verify(claim)
                if classification == "MISSING":
                    if not await asyncio.to_thread(reserve_post, claim):
                        classification = "CHANGED"
                    else:
                        post_status = None
                        async with httpx.AsyncClient(timeout=settings.ords_timeout_seconds, headers={
                            "X-API-Username": settings.ords_username, "X-API-Password": settings.ords_password,
                        }) as client:
                            try:
                                response = await client.post(settings.ords_base_url.rstrip("/") + "/raw-captures", json=claim["payload"])
                                post_status = response.status_code
                            except httpx.RequestError:
                                pass  # A lost reply does not prove the transaction failed.
                        classification, token = await verify(claim)
                        if classification == "MISSING" and post_status in {400, 404, 405, 410, 422}:
                            classification = "REJECTED"
            except Exception:
                classification, token = "UNKNOWN", None
            await asyncio.to_thread(persist_result, claim, classification, token)

    await asyncio.gather(*(one(claim) for claim in claims))

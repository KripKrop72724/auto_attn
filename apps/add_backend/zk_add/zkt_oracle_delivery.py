"""ADD-owned, immutable-payload delivery with separate Oracle core-field proof.

Registration is internal to future qualified occurrence creation. No device
payload, version string or connector toggle registers an intent. Existing
event UIDs/Oracle keys are never migrated or changed by this module.
The existing Oracle checker verifies UID, terminal, user reference, timestamp,
raw-punch flag, name and CNIC. Its receipt does not certify unexamined fields,
downstream daily processing, or the full 2.7.0 release contract.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import re

import httpx
from sqlalchemy import select

from zk_add.attendance_repair import _identity_digest, _immutable_facts, _ords_request, _protected_digest
from zk_add.crypto import decrypt_cnic, decrypt_json, encrypt_json
from zk_add.db import session_scope
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, ZktOccurrenceAlias,
                           ZktOracleContentReceipt, ZktOracleIntent, TerminalSourceEpoch, TerminalRecordManifest)
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import source_occurrence_delivery_hold, occurrence_id
from zk_add.identity_states import VERIFIED_IDENTITY_RESOLUTION_STATUSES

TOKEN = re.compile(r"^[a-f0-9]{64}$")
CONFLICTS = frozenset({"MISMATCH", "IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION", "CHANGED"})
VERIFICATION_SCOPE = "ORACLE_RAW_CORE_V1"


class EvidenceChanged(ValueError):
    pass


def _occurrence_bound(session, connector, alias):
    if source_occurrence_delivery_hold(session, connector, alias.occurrence_id):
        return False
    epoch = session.get(TerminalSourceEpoch, alias.source_epoch_id)
    manifest = session.get(TerminalRecordManifest, alias.manifest_id)
    return bool(epoch and manifest and epoch.zkt_device_id == alias.zkt_device_id
        and epoch.terminal_generation == manifest.generation
        and alias.occurrence_id == occurrence_id(manifest.terminal_serial, epoch.epoch_id,
                                                alias.ordinal, alias.raw_digest))


def register_intent(session, *, connector, event, outbox, alias):
    """Register only a new occurrence UID, inside its attendance transaction.

    The caller still owns qualified source decoding and identity evidence. An
    association is not such proof. Registration alone cannot resolve identity
    or permit an Oracle send; claim-time policy is independently revalidated.
    """
    if (not connector.zkt_custody_enabled or connector.firmware_family != "zkt"
            or event.connector_id != connector.id or outbox.attendance_event_id != event.id
            or alias.attendance_event_id != event.id or event.event_uid != alias.occurrence_id
            or not _occurrence_bound(session, connector, alias)):
        raise ValueError("ZKT_ORACLE_OCCURRENCE_BINDING")
    prior = session.scalar(select(ZktOracleIntent).where(ZktOracleIntent.attendance_event_id == event.id))
    if prior:
        if (prior.outbox_id, prior.occurrence_alias_id, prior.connector_id) != (outbox.id, alias.id, connector.id):
            raise ValueError("ZKT_ORACLE_INTENT_CONFLICT")
        return prior
    intent = ZktOracleIntent(outbox_id=outbox.id, attendance_event_id=event.id,
                             occurrence_alias_id=alias.id, connector_id=connector.id)
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
            or event.connector_id != connector.id or alias.attendance_event_id != event.id
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
    facts = _immutable_facts(event)
    check = {"contract_version": "1", "connector_id": connector.connector_id,
             "terminal_serial": event.device_serial, "items": [{"event_uid": event.event_uid,
                 "immutable_facts": facts, "immutable_facts_digest": _protected_digest(facts),
                 "desired_identity": {"employee_name": payload["employee_name"], "cnic": cnic,
                     "identity_digest": _identity_digest(payload["employee_name"], cnic)}}]}
    return event, payload, check


def _hold(row, event, reason):
    row.status = event.ords_status = "QUARANTINED_IDENTITY_CONFLICT"
    row.next_attempt_at = None
    row.last_error = reason
    row.acknowledged_at = event.oracle_confirmed_at = event.oracle_confirmation_path = None


def _retry(row, event):
    row.status = event.ords_status = "FAILED_RETRYABLE"
    row.last_error = "ZKT_ORACLE_CONTENT_VERIFICATION_PENDING"
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
    response = await _ords_request("raw-captures/identity-repairs/check", payload=claim["check"])
    if not isinstance(response, dict):
        return "UNKNOWN", None
    rows = response.get("results")
    if response.get("success") is not True or not isinstance(rows, list) or len(rows) != 1:
        return "UNKNOWN", None
    row = rows[0]
    if not isinstance(row, dict) or row.get("event_uid") != claim["payload"]["event_uid"]:
        return "UNKNOWN", None
    classification, token = row.get("classification"), row.get("current_content_token")
    if not isinstance(classification, str):
        return "UNKNOWN", None
    if classification in {"MATCH", "MISSING", "MISMATCH"} and not (
        isinstance(token, str) and TOKEN.fullmatch(token)
    ):
        return "UNKNOWN", None
    if classification not in {"MATCH", "MISSING", "MISMATCH", "IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION"}:
        return "UNKNOWN", None
    return classification, token


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
        if classification == "MATCH" and isinstance(token, str) and TOKEN.fullmatch(token):
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
            event.oracle_confirmation_path = "ADD_ZKT_CORE_CHECK"
            row.next_attempt_at = row.last_error = None
            row.last_http_status = 200
            # The receipt and completion state commit together, or not at all.
            session.flush()
        elif classification in CONFLICTS:
            _hold(row, event, "ZKT_ORACLE_CONTENT_CONFLICT" if classification != "CHANGED" else "ZKT_ORACLE_EVIDENCE_CHANGED")
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

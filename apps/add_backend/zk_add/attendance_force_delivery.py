"""Content-verified ORDS delivery for explicitly approved identity overrides.

Database sessions end before any network wait. Lost replies replay the exact
encrypted approval payload; neither HTTP success nor UID membership can ACK it.
"""

import asyncio
from datetime import timedelta
import re

import httpx
from sqlalchemy import select

from zk_add.attendance_force_release import delivery_payload, explain
from zk_add.attendance_legacy_uid import matches_original, potentially_recoverable
from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_repair import _identity_digest, _ords_request, _protected_digest
from zk_add.crypto import decrypt_json
from zk_add.db import session_scope
from zk_add.models import AttendanceEvent, AttendanceRecoveryItem, Connector, OrdsOutbox
from zk_add.settings import settings
from zk_add.time_utils import utc_now


def split_claims(claims):
    ordinary, forced = [], []
    with session_scope() as session:
        for claim in claims:
            row = session.get(OrdsOutbox, claim[0])
            event = session.get(AttendanceEvent, row.attendance_event_id) if row else None
            decision = decision_for(session, event) if event else None
            if decision is None:
                ordinary.append(claim)
                continue
            connector = session.get(Connector, event.connector_id)
            payload = delivery_payload(session, event, connector)
            if payload is None or payload != claim[1]:
                row.status = event.ords_status = "BLOCKED_IDENTITY"
                row.last_error = "The saved approval no longer matches the attendance."
                continue
            facts = decision.proof["immutable_facts"]
            forced.append(
                {
                    "row_id": row.id,
                    "decision_id": decision.id,
                    "attempt": row.attempt_count,
                    "direct": decision.proof.get("policy") == "manual-direct-ords-v1",
                    "payload": payload,
                    "check": {
                        "contract_version": "1",
                        "connector_id": connector.connector_id,
                        "terminal_serial": event.device_serial,
                        "items": [
                            {
                                "event_uid": event.event_uid,
                                "immutable_facts": facts,
                                "immutable_facts_digest": _protected_digest(facts),
                                "desired_identity": {
                                    "employee_name": payload["employee_name"],
                                    "cnic": payload["cnic"],
                                    "identity_digest": _identity_digest(
                                        payload["employee_name"], payload["cnic"]
                                    ),
                                },
                            }
                        ],
                    },
                }
            )
    return ordinary, forced


def reserve_direct_post(claim, classification):
    """Persist send intent before network I/O so an uncertain reply is verified first."""
    from zk_add.worker import event_uid_is_valid

    with session_scope() as session:
        row = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.id == claim["row_id"]).with_for_update()
        )
        if not row or row.status != "IN_FLIGHT" or row.attempt_count != claim["attempt"]:
            return False
        event = session.get(AttendanceEvent, row.attendance_event_id)
        decision = decision_for(session, event)
        if not decision or decision.id != claim["decision_id"]:
            return False
        item = session.get(AttendanceRecoveryItem, decision.item_id)
        prior = item.result.get("direct_post_attempts", 0)
        if prior and (
            classification != "MISSING" or not event_uid_is_valid(event.event_uid)
        ):
            return False
        item.result = {**item.result, "direct_post_attempts": prior + 1}
        return True


async def verify(claim):
    response = await _ords_request("raw-captures/identity-repairs/check", payload=claim["check"])
    rows = response.get("results")
    if response.get("success") is not True or not isinstance(rows, list) or len(rows) != 1:
        return "UNKNOWN", None
    row = rows[0]
    if not isinstance(row, dict) or row.get("event_uid") != claim["payload"]["event_uid"]:
        return "UNKNOWN", None
    classification, token = row.get("classification"), row.get("current_content_token")
    if classification in {"MATCH", "MISSING", "MISMATCH"} and not re.fullmatch(
        r"[0-9a-f]{64}", token or ""
    ):
        return "UNKNOWN", None
    if classification not in {
        "MATCH",
        "MISSING",
        "MISMATCH",
        "IMMUTABLE_MISMATCH",
        "CROSS_DEVICE_UID_COLLISION",
    }:
        return "UNKNOWN", None
    return classification, token


async def verify_legacy(claim):
    """Find an already stored original; never authorize a new Oracle insert."""
    response = await _ords_request("raw-captures/identity-repairs/check", payload=claim["check"])
    rows = response.get("results")
    if response.get("success") is not True or not isinstance(rows, list) or len(rows) != 1:
        return "UNKNOWN", None, None
    row = rows[0]
    if not isinstance(row, dict) or row.get("event_uid") != claim["payload"]["event_uid"]:
        return "UNKNOWN", None, None
    classification = row.get("classification")
    if classification == "LEGACY_SOURCE_MATCH":
        token, original = row.get("current_content_token"), row.get("matched_event_uid")
        if not re.fullmatch(r"[0-9a-f]{64}", token or "") or not matches_original(
            claim["payload"]["event_uid"], original
        ):
            return "UNKNOWN", None, None
        return classification, token, original
    if classification in {"LEGACY_SOURCE_MISSING", "LEGACY_SOURCE_CONFLICT", "LEGACY_SOURCE_AMBIGUOUS"}:
        return classification, None, None
    return "UNKNOWN", None, None


def still_authorized(claim):
    with session_scope() as session:
        row = session.get(OrdsOutbox, claim["row_id"])
        if not row or row.status != "IN_FLIGHT" or row.attempt_count != claim["attempt"]:
            return False
        event = session.get(AttendanceEvent, row.attendance_event_id)
        decision = decision_for(session, event)
        return bool(
            decision
            and decision.id == claim["decision_id"]
            and delivery_payload(session, event, session.get(Connector, event.connector_id))
            == claim["payload"]
        )


def persist_result(claim, classification, token=None, error_code=None, matched_event_uid=None):
    with session_scope() as session:
        row = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.id == claim["row_id"]).with_for_update()
        )
        if not row or row.status != "IN_FLIGHT" or row.attempt_count != claim["attempt"]:
            return  # A later claim owns this row; its content check accounts for the reply.
        event = session.get(AttendanceEvent, row.attendance_event_id)
        decision = decision_for(session, event)
        if (
            not decision
            or decision.id != claim["decision_id"]
            or decrypt_json(decision.payload_encrypted) != claim["payload"]
        ):
            return
        item = session.get(AttendanceRecoveryItem, decision.item_id)
        if classification == "LEGACY_SOURCE_MATCH" and not (
            claim["direct"] and matches_original(event.event_uid, matched_event_uid)
            and re.fullmatch(r"[0-9a-f]{64}", token or "")
        ):
            classification = "UNKNOWN"
        if classification in {"MATCH", "LEGACY_SOURCE_MATCH"}:
            now = utc_now()
            row.status = event.ords_status = "ACKED_CHECK"
            row.acknowledged_at = event.oracle_confirmed_at = now
            event.oracle_confirmation_path = (
                "ADD_FORCE_LEGACY_SOURCE_CHECK" if classification == "LEGACY_SOURCE_MATCH"
                else "ADD_FORCE_CONTENT_CHECK"
            )
            row.next_attempt_at = row.last_error = None
            row.last_http_status = 200
            item.result = {
                **item.result,
                "oracle_verified_payload_digest": decision.payload_digest,
                "oracle_content_token": token,
                "oracle_verified_at": now.isoformat(),
                "reason": (
                    "Oracle confirmed the matching original punch already stored there."
                    if classification == "LEGACY_SOURCE_MATCH" else "Oracle confirmed the approved attendance."
                ),
                "needs_attention": False,
            }
            if classification == "LEGACY_SOURCE_MATCH":
                item.result = {**item.result, "matched_oracle_event_uid": matched_event_uid}
            item.error_code = None
            item.status, item.completed_at = "CONFIRMED", now
        elif classification in {
            "MISMATCH",
            "IMMUTABLE_MISMATCH",
            "CROSS_DEVICE_UID_COLLISION",
            "CHANGED",
        }:
            code = "EVIDENCE_CHANGED" if classification == "CHANGED" else "ORACLE_CONFLICT"
            row.status = event.ords_status = "QUARANTINED_IDENTITY_CONFLICT"
            row.next_attempt_at, row.last_error = None, code
            item.status, item.error_code = "NEEDS_REVIEW", code
            item.result = {**item.result, "reason": explain(code)}
        elif classification in {"LEGACY_SOURCE_MISSING", "LEGACY_SOURCE_CONFLICT", "LEGACY_SOURCE_AMBIGUOUS"}:
            row.status = event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
            row.next_attempt_at, row.last_error = None, classification
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", classification, utc_now()
            item.result = {
                **item.result,
                "reason": (
                    "No matching original punch was found in Oracle. This damaged ID was not sent."
                    if classification == "LEGACY_SOURCE_MISSING" else
                    "Oracle has a different or ambiguous punch. Nothing was sent; review is required."
                ),
                "needs_attention": True,
            }
        elif classification == "INVALID_EVENT_UID":
            now = utc_now()
            row.status = event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
            row.next_attempt_at, row.last_error = None, "INVALID_EVENT_UID"
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", "INVALID_EVENT_UID", now
            item.result = {
                **item.result,
                "reason": "Oracle cannot accept this older punch ID. The punch and approval remain saved for ID repair.",
                "needs_attention": True,
            }
        elif classification == "REJECTED":
            row.status = event.ords_status = "QUARANTINED_ORDS_REJECTED"
            row.next_attempt_at, row.last_error = None, error_code or "ORACLE_REJECTED"
            item.status, item.error_code = "NEEDS_REVIEW", "ORACLE_REJECTED"
            item.result = {
                **item.result,
                "reason": "Oracle rejected this attendance. An administrator must review it.",
            }
        else:
            # Credential/contract failures remain saved and visible; backoff is
            # capped. A later operator fix does not require another release.
            row.status = event.ords_status = "FAILED_RETRYABLE"
            row.last_error = error_code or "ORACLE_CONTENT_VERIFICATION_PENDING"
            row.next_attempt_at = utc_now() + timedelta(
                seconds=min(600, 2 ** min(row.attempt_count, 9))
            )
            item.result = {
                **item.result,
                "reason": explain("ORACLE_UNAVAILABLE"),
                "delivery_error": row.last_error,
                "needs_attention": bool(
                    row.attempt_count >= 10
                    or error_code
                    in {
                        "ORDS_AUTHENTICATION_FAILED",
                        "ORDS_AUTHENTICATION_NOT_CONFIGURED",
                        "ORDS_HTTP_404",
                        "ORDS_HTTP_405",
                        "ORDS_HTTP_400",
                        "ORDS_MALFORMED_RESPONSE",
                    }
                ),
            }
            item.error_code = row.last_error
            if error_code in {"ORDS_AUTHENTICATION_FAILED", "ORDS_AUTHENTICATION_NOT_CONFIGURED"}:
                item.result = {
                    **item.result,
                    "reason": "Oracle verification credentials need administrator attention. The attendance remains saved.",
                }
        item.updated_at = utc_now()


async def deliver_forced(claims, *, concurrency):
    from zk_add.worker import event_uid_is_valid

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 4)))

    async def one(claim):
        classification, token, code = "UNKNOWN", None, None
        matched_event_uid = None
        async with semaphore:
            try:
                uid = claim["payload"].get("event_uid")
                if claim["direct"] and potentially_recoverable(uid):
                    classification, token, matched_event_uid = await verify_legacy(claim)
                    should_send = False
                elif not event_uid_is_valid(uid):
                    classification = "INVALID_EVENT_UID"
                    should_send = False
                else:
                    classification, token = await verify(claim)
                    should_send = classification == "MISSING" or (
                        claim["direct"] and classification != "MATCH"
                    )
                if should_send:
                    if not await asyncio.to_thread(still_authorized, claim):
                        classification = "CHANGED"
                    elif claim["direct"] and not await asyncio.to_thread(
                        reserve_direct_post, claim, classification
                    ):
                        code = "ORACLE_CONTENT_VERIFICATION_PENDING"
                    else:
                        post_status = None
                        async with httpx.AsyncClient(
                            timeout=settings.ords_timeout_seconds,
                            headers={
                                "X-API-Username": settings.ords_username,
                                "X-API-Password": settings.ords_password,
                            },
                        ) as client:
                            try:
                                response = await client.post(
                                    settings.ords_base_url.rstrip("/") + "/raw-captures",
                                    json=claim["payload"],
                                )
                                post_status = response.status_code
                            except httpx.RequestError:
                                pass  # The write may have committed; content verification decides.
                        classification, token = await verify(claim)
                        if classification != "MATCH" and post_status in {400, 404, 405, 410, 422}:
                            classification, code = "REJECTED", f"ORDS_HTTP_{post_status}"
            except Exception as exc:
                classification = "UNKNOWN"
                # Only controlled error codes; no response bodies or identities in logs.
                code = getattr(exc, "code", "ORACLE_CONTENT_VERIFICATION_PENDING")
            await asyncio.to_thread(persist_result, claim, classification, token, code, matched_event_uid)

    await asyncio.gather(*(one(claim) for claim in claims))

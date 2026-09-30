#!/usr/bin/env python3
"""Read-only operational counters; never export raw logs or attendance payloads."""

from collections import Counter
import argparse
import json
import re
import sys


CHECK_ERROR_CODES = {
    "CONTRACT_VERSION_UNSUPPORTED", "BATCH_LIMIT", "INVALID_CHECK_ITEM",
    "CHECK_VALIDATION_FAILED", "ADD_ONLY_AUTH_REQUIRED",
}


def check_payload_shape(payload):
    """Export validation booleans only, never an approved identity or source value."""
    from datetime import datetime

    items = payload.get("items")
    item = items[0] if isinstance(items, list) and len(items) == 1 else {}
    item = item if isinstance(item, dict) else {}
    facts = item.get("immutable_facts")
    facts = facts if isinstance(facts, dict) else {}
    identity = item.get("desired_identity")
    identity = identity if isinstance(identity, dict) else {}
    timestamp = facts.get("device_event_time")
    timestamp_valid = bool(isinstance(timestamp, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})",
        timestamp,
    ))
    if timestamp_valid:
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            timestamp_valid = False
    return {
        "contract_version_valid": payload.get("contract_version") == "1",
        "single_item": isinstance(items, list) and len(items) == 1,
        "event_uid_format_valid": bool(isinstance(item.get("event_uid"), str)
                                       and re.fullmatch(r"[0-9a-f]{64}", item["event_uid"])),
        "device_serial_present": bool(facts.get("device_serial")),
        "source_user_id_present": bool(facts.get("source_user_id")),
        "timestamp_oracle_format_valid": timestamp_valid,
        "raw_punch_valid": facts.get("raw_punch") in ("T", "F"),
        "employee_name_present": bool(identity.get("employee_name")),
        "cnic_format_valid": bool(isinstance(identity.get("cnic"), str)
                                  and re.fullmatch(r"[0-9]{13}", identity["cnic"])),
    }


def check_response_summary(response, payload):
    """A fixed error allowlist prevents arbitrary Oracle bodies reaching logs."""
    summary = {"http_status": response.status_code, "shape": check_payload_shape(payload)}
    try:
        body = response.json()
    except ValueError:
        summary["error_code"] = "ORDS_MALFORMED_RESPONSE"
        return summary
    if not isinstance(body, dict):
        summary["error_code"] = "ORDS_MALFORMED_RESPONSE"
        return summary
    code = body.get("error_code")
    summary["error_code"] = code if isinstance(code, str) and code in CHECK_ERROR_CODES else (
        "UNRECOGNIZED_ERROR_CODE" if code is not None else None
    )
    results = body.get("results")
    summary["response_shape_valid"] = bool(
        body.get("success") is True and isinstance(results, list) and len(results) == 1
        and isinstance(results[0], dict)
        and results[0].get("event_uid") == payload["items"][0]["event_uid"]
    )
    return summary


def probe_saved_direct_checks(checks):
    """POST only the read-only content verifier; never send an attendance write."""
    import httpx
    from zk_add.settings import settings

    if not (settings.ords_base_url and settings.attendance_repair_ords_username
            and settings.attendance_repair_ords_password):
        return {"configured": False}
    url = settings.ords_base_url.rstrip("/") + "/raw-captures/identity-repairs/check"
    results = []
    with httpx.Client(timeout=15, headers={
        "X-API-Username": settings.attendance_repair_ords_username,
        "X-API-Password": settings.attendance_repair_ords_password,
    }, follow_redirects=False) as client:
        for check in checks[:3]:
            try:
                results.append(check_response_summary(client.post(url, json=check), check))
            except httpx.RequestError:
                results.append({"error_code": "ORDS_TRANSPORT_ERROR", "shape": check_payload_shape(check)})
    return {"configured": True, "checks": results}


def oracle_direct_run_diagnostics(direct_run_id):
    from sqlalchemy import text
    from zk_add.attendance_repair import _identity_digest, _protected_digest
    from zk_add.crypto import decrypt_json
    from zk_add.db import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            connection.execute(text("SET LOCAL lock_timeout = '1s'"))
            rows = connection.execute(text("""
                SELECT d.payload_encrypted, d.proof, c.connector_id, e.device_serial
                FROM add_attendance_force_release_decisions d
                JOIN add_attendance_recovery_jobs j ON j.id=d.job_id
                JOIN add_attendance_events e ON e.id=d.attendance_event_id
                JOIN add_connectors c ON c.id=e.connector_id
                WHERE j.job_id=:run_id AND j.action='MANUAL_DIRECT_ORDS'
                  AND d.proof->>'policy'='manual-direct-ords-v1'
                ORDER BY d.id LIMIT 3
            """), {"run_id": direct_run_id}).mappings().all()
            connection.rollback()
        checks = []
        for row in rows:
            payload = decrypt_json(row["payload_encrypted"])
            facts = row["proof"]["immutable_facts"]
            checks.append({
                "contract_version": "1", "connector_id": row["connector_id"],
                "terminal_serial": row["device_serial"], "items": [{
                    "event_uid": payload["event_uid"], "immutable_facts": facts,
                    "immutable_facts_digest": _protected_digest(facts),
                    "desired_identity": {
                        "employee_name": payload["employee_name"], "cnic": payload["cnic"],
                        "identity_digest": _identity_digest(payload["employee_name"], payload["cnic"]),
                    },
                }],
            })
        return probe_saved_direct_checks(checks)
    except Exception:
        return {"error_code": "SAVED_APPROVAL_DIAGNOSTIC_FAILED"}


# Bound the input before examining identity history. Only internal run/item IDs
# and booleans leave the database; names, identifiers, CNICs and hashes do not.
FORCE_CONFLICT_SQL = """
WITH recent AS (
    SELECT i.id, i.item_id, i.job_id, i.connector_id, i.attendance_event_id,
           j.job_id AS run_id
    FROM add_attendance_recovery_items i
    JOIN add_attendance_recovery_jobs j ON j.id=i.job_id
    WHERE j.action='MANUAL_FORCE_RELEASE' AND i.error_code='IDENTITY_CONFLICT'
    ORDER BY i.id DESC LIMIT 25
)
SELECT r.run_id, r.item_id,
       COALESCE(u.identity_conflict_code <> '', false) AS current_conflict,
       COALESCE(e.device_user_id <> u.id, false) AS captured_user_changed,
       e.identity_resolution_status IN ('QUARANTINED_REUSE','BLOCKED_SOURCE_CONFLICT')
           AS captured_conflict,
       COALESCE(e.identity_terminal_fingerprint <> '' AND
           e.identity_terminal_fingerprint IS DISTINCT FROM u.terminal_identity_fingerprint, false)
           AS captured_fingerprint_changed,
       EXISTS (SELECT 1 FROM add_device_users d WHERE d.zkt_device_id=z.id
           AND d.id<>u.id AND (d.user_id=u.user_id OR
               (NULLIF(u.uid,'') IS NOT NULL AND d.uid=u.uid))) AS other_user_row,
       EXISTS (SELECT 1 FROM add_identity_tombstones h WHERE h.zkt_device_id=z.id
           AND (h.user_id=u.user_id OR (NULLIF(u.uid,'') IS NOT NULL AND h.uid=u.uid))
           AND (h.device_user_id<>u.id OR h.cnic_lookup_hash<>u.cnic_lookup_hash)
           AND (h.device_serial=z.serial OR h.device_serial IS NULL OR h.device_serial=''))
           AS tombstone_current_or_unknown_terminal,
       EXISTS (SELECT 1 FROM add_identity_tombstones h WHERE h.zkt_device_id=z.id
           AND (h.user_id=u.user_id OR (NULLIF(u.uid,'') IS NOT NULL AND h.uid=u.uid))
           AND (h.device_user_id<>u.id OR h.cnic_lookup_hash<>u.cnic_lookup_hash)
           AND NULLIF(h.device_serial,'') IS NOT NULL AND h.device_serial<>z.serial)
           AS tombstone_other_terminal,
       EXISTS (SELECT 1 FROM add_attendance_identity_history h WHERE h.zkt_device_id=z.id
           AND h.user_id=u.user_id AND (h.terminal_serial=z.serial OR h.terminal_serial='')
           AND (h.device_user_id<>u.id OR h.cnic_lookup_hash<>u.cnic_lookup_hash))
           AS history_current_or_unknown_terminal,
       EXISTS (SELECT 1 FROM add_attendance_identity_history h WHERE h.zkt_device_id=z.id
           AND h.user_id=u.user_id AND NULLIF(h.terminal_serial,'') IS NOT NULL
           AND h.terminal_serial<>z.serial
           AND (h.device_user_id<>u.id OR h.cnic_lookup_hash<>u.cnic_lookup_hash))
           AS history_other_terminal,
       EXISTS (SELECT 1 FROM add_attendance_force_release_users b WHERE b.task_id=t.id
           AND (b.user_id=u.user_id OR (NULLIF(u.uid,'') IS NOT NULL AND b.uid=u.uid))
           AND (b.device_user_id<>u.id OR b.cnic_hash<>u.cnic_lookup_hash
               OR (NULLIF(b.identity_fingerprint,'') IS NOT NULL AND
                   b.identity_fingerprint IS DISTINCT FROM u.terminal_identity_fingerprint)))
           AS baseline_changed
FROM recent r
JOIN add_attendance_events e ON e.id=r.attendance_event_id
JOIN add_zkt_devices z ON z.connector_id=r.connector_id
JOIN add_attendance_force_release_tasks t ON t.job_id=r.job_id AND t.connector_id=r.connector_id
JOIN add_device_users u ON u.zkt_device_id=z.id AND u.user_id=e.user_id
    AND u.present=true AND u.lifecycle_state='ACTIVE'
ORDER BY r.id DESC
"""


def summarize_logs(lines):
    """Allowlist output fields instead of trying to redact arbitrary log messages."""
    responses = Counter()
    errors = Counter()
    frames = Counter()
    proxy = Counter()
    runtime = Counter()
    firmware_auth_rejections = Counter()
    for line in lines:
        response = re.search(r'"(GET|POST|PUT|PATCH|DELETE) ([^ ]+) HTTP/[^" ]+" (\d{3})', line)
        if response:
            method, path, code = response.groups()
            path = path.split("?", 1)[0]
            if path.startswith("/api/v2/attendance-recovery/"):
                family = "attendance-recovery"
            elif path == "/device/v2/firmware/capability":
                family = "firmware-capability"
            elif path == "/device/v2/firmware/assignment":
                family = "firmware-assignment"
            elif path == "/api/v1/auth/session":
                family = "auth-session"
            elif path.startswith("/health/"):
                family = "health"
            else:
                family = "other"
            responses[f"{method} {family} {code}"] += 1
        error = re.search(
            r"(?:^|\s)(?:[A-Za-z_][A-Za-z_0-9]*\.)*([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception|Violation|Timeout)):",
            line,
        )
        if error:
            errors[error.group(1)] += 1
        frame = re.search(r'File "[^"\r\n]*/zk_add/([a-z_]+\.py)", line (\d+), in ([a-z_]+)', line)
        if frame:
            frames[":".join(frame.groups())] += 1
        for phrase in (
            "upstream timed out",
            "connection refused",
            "upstream prematurely closed",
            "no live upstreams",
            "limiting requests",
        ):
            if phrase in line.lower():
                proxy[phrase] += 1
        for phrase in ("ADD event loop lag detected", "restarting API process"):
            if phrase in line:
                runtime[phrase] += 1
        rejected = re.search(
            r"OTA_AUTH_REJECTED connector_fp=([0-9a-f]{12}) reason=([A-Z_]+)", line
        )
        if rejected:
            firmware_auth_rejections[" ".join(rejected.groups())] += 1
    return {
        "http_counts": dict(responses),
        "exception_types": dict(errors),
        "code_frames": dict(frames),
        "proxy_errors": dict(proxy),
        "runtime_signals": dict(runtime),
        "firmware_auth_rejections": dict(firmware_auth_rejections),
    }


def database_report(direct_run_id=None, attendance_event_ids=None):
    from sqlalchemy import text
    from zk_add.db import engine
    from zk_add.settings import settings

    queries = {
        "force_conflicts": FORCE_CONFLICT_SQL,
        "jobs": """SELECT id, job_id, status, requested_count, eligible_count,
                   created_at, updated_at, (last_error IS NOT NULL) AS has_error
                   FROM add_attendance_recovery_jobs WHERE action = 'SAFE_REPAIR'
                   ORDER BY id DESC LIMIT 10""",
        "tasks": """SELECT job_id, status, count(*) AS tasks, sum(checked_count) AS checked
                    FROM add_attendance_safe_repair_tasks
                    GROUP BY job_id, status ORDER BY job_id DESC LIMIT 30""",
        "connections": """SELECT state, wait_event_type, wait_event, count(*) AS connections,
                          max(EXTRACT(EPOCH FROM (now()-xact_start))) AS oldest_transaction_seconds
                          FROM pg_stat_activity WHERE datname=current_database()
                          GROUP BY state, wait_event_type, wait_event""",
        "blocking": """SELECT a.pid, a.state, a.wait_event_type, a.wait_event,
                       EXTRACT(EPOCH FROM(now()-a.xact_start)) AS transaction_seconds,
                       pg_blocking_pids(a.pid) AS blockers
                       FROM pg_stat_activity a WHERE a.datname=current_database()
                       AND cardinality(pg_blocking_pids(a.pid)) > 0 LIMIT 20""",
        "delivery": """SELECT status, count(*) AS records FROM add_ords_outbox GROUP BY status""",
        "force_sync": """SELECT j.job_id, j.status AS job_status, t.status AS task_status,
                    t.error_code, t.sync_requested_at, t.sync_deadline,
                    c.command_id, c.status AS command_status, c.started_at, c.completed_at,
                    z.identity_snapshot_revision, z.identity_snapshot_observed_at,
                    z.identity_snapshot_received_at
                    FROM add_attendance_force_release_tasks t
                    JOIN add_attendance_recovery_jobs j ON j.id=t.job_id
                    LEFT JOIN add_device_commands c ON c.id=t.sync_command_id
                    LEFT JOIN add_zkt_devices z ON z.connector_id=t.connector_id
                    ORDER BY t.id DESC LIMIT 10""",
        "force_sync_order": """SELECT c.command_id, e.status, e.created_at,
                    e.details ->> 'sequence' AS message_sequence,
                    e.details ->> 'sent_at' AS device_sent_at,
                    e.details ->> 'observed_at' AS snapshot_observed_at
                    FROM add_device_command_events e
                    JOIN add_device_commands c ON c.id=e.command_id
                    WHERE e.status IN ('SYNC_READ_BEGIN','SYNC_READ_ROSTER','SYNC_READ_END')
                    ORDER BY e.id DESC LIMIT 30""",
    }
    result = {
        "flags": {
            "checks": settings.attendance_safe_repair_preview_enabled,
            "execution": settings.attendance_safe_repair_execution_enabled,
            "automatic": getattr(settings, "attendance_safe_repair_automatic_enabled", False),
            "force_checks": getattr(settings, "attendance_force_release_preview_enabled", False),
            "force_execution": getattr(settings, "attendance_force_release_execution_enabled", False),
            "oracle_delivery_configured": bool(settings.ords_base_url),
            "oracle_content_verification_configured": bool(
                settings.attendance_repair_ords_username
                and settings.attendance_repair_ords_password
            ),
        }
    }
    if direct_run_id:
        # Only operational codes leave the runner. No identities, CNICs,
        # event UIDs, payloads or secrets appear in workflow logs.
        queries["direct_run"] = """
            SELECT i.attendance_event_id, i.status AS item_status,
                   CASE WHEN i.error_code IS NULL THEN NULL
                        WHEN i.error_code ~ '^[A-Z][A-Z0-9_]{0,119}$'
                        THEN i.error_code ELSE 'REDACTED' END AS error_code,
                   o.status AS outbox_status,
                   o.attempt_count, o.last_http_status,
                   CASE WHEN o.last_error IS NULL THEN NULL
                        WHEN o.last_error ~ '^[A-Z][A-Z0-9_]{0,119}$'
                        THEN o.last_error ELSE 'REDACTED' END AS outbox_error,
                   COALESCE((i.result->>'needs_attention')::boolean, false)
                       AS needs_attention,
                   (e.event_uid ~ '^[0-9a-f]{64}$') AS event_uid_format_valid,
                   (extract(microseconds from e.device_event_time)::bigint % 1000000 <> 0)
                       AS timestamp_has_fraction
              FROM add_attendance_recovery_items i
              JOIN add_attendance_recovery_jobs j ON j.id = i.job_id
              JOIN add_attendance_events e ON e.id = i.attendance_event_id
              LEFT JOIN add_ords_outbox o ON o.attendance_event_id = e.id
             WHERE j.job_id = :direct_run_id AND j.action = 'MANUAL_DIRECT_ORDS'
             ORDER BY i.id LIMIT 100
        """
    if attendance_event_ids:
        # Build only a virtual proposed row. PostgreSQL evaluates the guard
        # against current roster/history without changing attendance or outbox.
        queries["reconciled_guard"] = """
            SELECT e.id AS event_id,
                   e.source, e.clock_quality,
                   e.captured_cnic_lookup_hash IS NULL AS captured_hash_null,
                   e.captured_cnic_lookup_hash = '' AS captured_hash_empty,
                   e.identity_terminal_fingerprint IS NOT NULL AS event_fingerprint_present,
                   e.captured_at BETWEEN e.device_event_time
                                     AND e.device_event_time + INTERVAL '10 minutes'
                       AS capture_window,
                   e.received_at BETWEEN e.captured_at - INTERVAL '30 seconds'
                                     AND e.captured_at + INTERVAL '10 minutes'
                       AS receipt_window,
                   u.snapshot_revision = z.identity_snapshot_revision
                       AND z.identity_snapshot_id IS NOT NULL AS current_snapshot,
                   (SELECT count(*) FROM add_attendance_identity_history h
                    WHERE h.zkt_device_id=z.id AND h.device_user_id=u.id
                      AND h.terminal_serial=e.device_serial
                      AND h.user_id=e.user_id AND h.uid=e.uid
                      AND h.fingerprint=e.identity_terminal_fingerprint
                      AND h.cnic_lookup_hash=u.cnic_lookup_hash
                      AND NOT h.revoked
                      AND h.observed_from <= e.device_event_time
                      AND h.observed_until >= e.device_event_time)
                       AS matching_history_count,
                   (SELECT count(*) FROM add_terminal_record_manifest m
                    WHERE m.attendance_event_id=e.id
                      AND (m.connector_id IS DISTINCT FROM e.connector_id
                        OR m.zkt_device_id IS DISTINCT FROM e.zkt_device_id
                        OR m.terminal_serial IS DISTINCT FROM e.device_serial
                        OR (m.observed_user_id IS NOT NULL
                          AND m.observed_user_id <> e.user_id)
                        OR (m.observed_uid IS NOT NULL AND m.observed_uid <> e.uid
                          AND NOT COALESCE(m.record_size=40 AND
                            m.observed_uid=(e.raw_event ->> 'attendance_record_uid'), false))
                        OR m.disposition NOT IN
                          ('EVENT','BLOCKED_IDENTITY','TERMINAL_DUPLICATE')))
                       AS conflicting_manifest_count,
                   add_auto_reconciled_cnic_verified(
                     jsonb_populate_record(NULL::add_attendance_events,
                       to_jsonb(e) || jsonb_build_object(
                         'identity_resolution_status','RESOLVED_SYNCED_CNIC',
                         'identity_repair_reason','VERIFIED_SYNCED_CNIC',
                         'device_user_id',u.id,
                         'cnic_lookup_hash',u.cnic_lookup_hash,
                         'cnic_encrypted',u.cnic_encrypted,
                         'identity_snapshot_id',z.identity_snapshot_id,
                         'ords_status','PENDING',
                         'manual_release_required',false,
                         'display_name',u.display_name)))
                       AS prospective_guard_accepts
            FROM add_attendance_events e
            JOIN add_zkt_devices z ON z.id=e.zkt_device_id
            JOIN add_device_users u ON u.zkt_device_id=z.id
              AND u.user_id=e.user_id AND u.present
              AND u.lifecycle_state='ACTIVE'
            WHERE e.id=ANY(:attendance_event_ids)
            ORDER BY e.id
            LIMIT 30
        """
    queries["schema"] = "SELECT version_num FROM alembic_version"
    for name, query in queries.items():
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                params = (
                    {"direct_run_id": direct_run_id} if name == "direct_run" else
                    {"attendance_event_ids": attendance_event_ids}
                    if name == "reconciled_guard" else {}
                )
                result[name] = [dict(row) for row in connection.execute(text(query), params).mappings()]
                connection.rollback()
        except Exception as exc:
            result[name] = {"error_type": type(exc).__name__}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--direct-run-id")
    parser.add_argument("--attendance-event-id")
    args = parser.parse_args()
    if args.direct_run_id and not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args.direct_run_id):
        parser.error("direct run ID must be a UUID")
    attendance_event_ids = None
    if args.attendance_event_id is not None:
        tokens = args.attendance_event_id.split(",")
        if not 1 <= len(tokens) <= 30 or any(
            not token.isdigit() or not 1 <= int(token) <= 2_000_000_000
            for token in tokens
        ):
            parser.error("provide up to 30 positive attendance database IDs")
        attendance_event_ids = [int(token) for token in tokens]
    result = (
        summarize_logs(sys.stdin) if args.logs
        else database_report(args.direct_run_id, attendance_event_ids)
    )
    if args.direct_run_id and not args.logs:
        result["direct_oracle_check"] = oracle_direct_run_diagnostics(args.direct_run_id)
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()

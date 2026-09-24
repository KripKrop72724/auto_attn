#!/usr/bin/env python3
"""Read-only operational counters; never export raw logs or attendance payloads."""

from collections import Counter
import argparse
import json
import re
import sys


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


def database_report(direct_run_id=None):
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
    queries["schema"] = "SELECT version_num FROM alembic_version"
    for name, query in queries.items():
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                params = {"direct_run_id": direct_run_id} if name == "direct_run" else {}
                result[name] = [dict(row) for row in connection.execute(text(query), params).mappings()]
                connection.rollback()
        except Exception as exc:
            result[name] = {"error_type": type(exc).__name__}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--direct-run-id")
    args = parser.parse_args()
    if args.direct_run_id and not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args.direct_run_id):
        parser.error("direct run ID must be a UUID")
    result = summarize_logs(sys.stdin) if args.logs else database_report(args.direct_run_id)
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()

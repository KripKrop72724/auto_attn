#!/usr/bin/env python3
"""Read-only operational counters; never export raw logs or attendance payloads."""

from collections import Counter
import argparse
import json
import re
import sys


def summarize_logs(lines):
    """Allowlist output fields instead of trying to redact arbitrary log messages."""
    responses = Counter()
    errors = Counter()
    frames = Counter()
    proxy = Counter()
    runtime = Counter()
    for line in lines:
        response = re.search(r'"(GET|POST|PUT|PATCH|DELETE) ([^ ]+) HTTP/[^" ]+" (\d{3})', line)
        if response:
            method, path, code = response.groups()
            path = path.split("?", 1)[0]
            if path.startswith("/api/v2/attendance-recovery/"):
                family = "attendance-recovery"
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
    return {
        "http_counts": dict(responses),
        "exception_types": dict(errors),
        "code_frames": dict(frames),
        "proxy_errors": dict(proxy),
        "runtime_signals": dict(runtime),
    }


def database_report():
    from sqlalchemy import text
    from zk_add.db import engine
    from zk_add.settings import settings

    queries = {
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
    }
    result = {
        "flags": {
            "checks": settings.attendance_safe_repair_preview_enabled,
            "execution": settings.attendance_safe_repair_execution_enabled,
            "automatic": getattr(settings, "attendance_safe_repair_automatic_enabled", False),
            "force_checks": getattr(settings, "attendance_force_release_preview_enabled", False),
            "force_execution": getattr(settings, "attendance_force_release_execution_enabled", False),
            "oracle_delivery_configured": bool(settings.ords_base_url),
        }
    }
    queries["schema"] = "SELECT version_num FROM alembic_version"
    for name, query in queries.items():
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                result[name] = [dict(row) for row in connection.execute(text(query)).mappings()]
                connection.rollback()
        except Exception as exc:
            result[name] = {"error_type": type(exc).__name__}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", action="store_true")
    args = parser.parse_args()
    result = summarize_logs(sys.stdin) if args.logs else database_report()
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()

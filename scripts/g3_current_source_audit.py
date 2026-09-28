#!/usr/bin/env python3
"""Read-only, bounded G3 source-membership audit for one completed reconcile.

Only internal ADD row IDs and aggregate counts leave the production runner.
Names, CNICs, event UIDs, and source bytes stay inside the ADD container.
"""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from zk_add.db import engine


PKT = timezone(timedelta(hours=5))
EXPECTED_SERIAL = "PGB1261200074"


def zk_time(value: int) -> datetime | None:
    if not value or value < 0:
        return None
    second = value % 60
    value //= 60
    minute = value % 60
    value //= 60
    hour = value % 24
    value //= 24
    day = value % 31 + 1
    value //= 31
    month = value % 12 + 1
    year = value // 12 + 2000
    try:
        decoded = datetime(year, month, day, hour, minute, second, tzinfo=PKT)
    except ValueError:
        return None
    return decoded if 2020 <= year <= 2035 else None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def key(user_id, event_time, status, punch):
    return (str(user_id or ""), utc(event_time).isoformat(), str(status or ""), str(punch or ""))


def audit(job_id: str) -> dict:
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            job = connection.execute(text("""
                SELECT id, connector_id, zkt_device_id, source_epoch_id,
                       terminal_serial, terminal_generation, cutoff_count,
                       requested_at, capture_certified_at, status
                FROM add_reconciliation_jobs WHERE job_id=:job_id
            """), {"job_id": job_id}).mappings().one()
            if job["terminal_serial"] != EXPECTED_SERIAL or job["status"] != "COMPLETED":
                raise ValueError("G3 job must have completed before this audit")
            if not job["capture_certified_at"] or not job["cutoff_count"] or not job["source_epoch_id"]:
                raise ValueError("G3 job has no certified source cutoff")
            manifests = connection.execute(text("""
                SELECT m.id AS manifest_id, m.attendance_event_id, m.raw_record_digest,
                       m.observed_user_id, m.raw_timestamp,
                       e.user_id, e.device_event_time, e.status, e.punch
                FROM add_terminal_record_manifest m
                LEFT JOIN add_attendance_events e ON e.id=m.attendance_event_id
                WHERE m.zkt_device_id=:device_id
                  AND m.generation=:generation
                  AND m.source_epoch_id=:source_epoch_id
                  AND m.canonical_source=true AND m.ordinal < :cutoff
                ORDER BY m.ordinal
            """), {
                "device_id": job["zkt_device_id"],
                "generation": job["terminal_generation"],
                "source_epoch_id": job["source_epoch_id"],
                "cutoff": job["cutoff_count"],
            }).mappings().all()
            if len(manifests) != job["cutoff_count"]:
                raise ValueError("Current source manifest does not cover the certified cutoff")
            dated = [zk_time(row["raw_timestamp"]) for row in manifests]
            first = min((value for value in dated if value is not None), default=None)
            if first is None:
                raise ValueError("No plausible source punch timestamp")
            current_ids = {row["attendance_event_id"] for row in manifests if row["attendance_event_id"] is not None}
            current_manifest_ids = {row["manifest_id"] for row in manifests}
            current_digests = {row["raw_record_digest"] for row in manifests}
            corrections = connection.execute(text("""
                SELECT manifest_id, original_digest, derived_attendance_event_id
                FROM add_attendance_source_corrections
                WHERE connector_id=:connector_id AND status='CREATED'
                  AND derived_attendance_event_id IS NOT NULL
            """), {"connector_id": job["connector_id"]}).mappings().all()
            current_ids.update(
                row["derived_attendance_event_id"] for row in corrections
                if row["manifest_id"] in current_manifest_ids
                or row["original_digest"] in current_digests
            )
            current_keys = {
                key(row["user_id"], row["device_event_time"], row["status"], row["punch"])
                for row in manifests if row["device_event_time"] is not None
            }
            current_raw_user_times = {
                (str(row["observed_user_id"]), decoded.isoformat())
                for row, decoded in zip(manifests, dated)
                if row["observed_user_id"] and decoded is not None
            }
            saved = connection.execute(text("""
                SELECT e.id, e.user_id, e.device_event_time, e.status, e.punch,
                       e.ords_status, e.oracle_confirmed_at
                FROM add_attendance_events e
                WHERE e.connector_id=:connector_id
                  AND e.zkt_device_id=:device_id
                  AND e.device_serial=:serial
                  AND e.device_event_time >= :first_time
                  AND e.received_at <= :requested_at
                ORDER BY e.id
            """), {
                "connector_id": job["connector_id"],
                "device_id": job["zkt_device_id"],
                "serial": EXPECTED_SERIAL,
                "first_time": first.astimezone(timezone.utc),
                "requested_at": job["requested_at"],
            }).mappings().all()
    missing = []
    possibly_matched = []
    for row in saved:
        if row["id"] in current_ids:
            continue
        if key(row["user_id"], row["device_event_time"], row["status"], row["punch"]) in current_keys:
            possibly_matched.append(row["id"])
            continue
        raw_key = (str(row["user_id"]), utc(row["device_event_time"]).astimezone(PKT).isoformat())
        if raw_key in current_raw_user_times:
            possibly_matched.append(row["id"])
            continue
        missing.append(row)
    confirmed = [row["id"] for row in missing if row["oracle_confirmed_at"] is not None]
    return {
        "job_id": job_id,
        "source_epoch_id": job["source_epoch_id"],
        "cutoff": job["cutoff_count"],
        "first_current_source_time_pkt": first.isoformat(),
        "saved_in_scope": len(saved),
        "not_linked_but_possible_match_ids": possibly_matched,
        "absent_from_current_source_ids": [row["id"] for row in missing],
        "absent_and_oracle_confirmed_ids": confirmed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args.job_id):
        raise SystemExit("Invalid job ID")
    print("G3_AUDIT_JSON=" + json.dumps(audit(args.job_id), separators=(",", ":")))

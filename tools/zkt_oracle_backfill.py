#!/usr/bin/env python3
"""Backfill current ZKT attendance records to Oracle ORDS.

The script intentionally reads credentials from the ignored firmware
`zone_lite_config.h` so secrets are not committed to the repository.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from zk import ZK


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "firmware" / "zone_lite" / "main" / "zone_lite_config.h"
OUTPUT_PATH = ROOT / "outputs" / "zkt_oracle_backfill_attempted.jsonl"
LOCAL_TZ = ZoneInfo("Asia/Karachi")


@dataclass(frozen=True)
class Config:
    zkt_ip: str
    zkt_port: int
    zkt_comm_key: int
    zone_id: str
    zone_name: str
    zone_device_id: str
    ords_base_url: str
    ords_username: str
    ords_password: str


def read_define(text: str, name: str) -> str:
    match = re.search(rf"^\s*#define\s+{re.escape(name)}\s+(.+?)\s*$", text, re.MULTILINE)
    if not match:
        raise KeyError(f"Missing {name} in {CONFIG_PATH}")
    raw = match.group(1).strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def load_config() -> Config:
    text = CONFIG_PATH.read_text()
    return Config(
        zkt_ip=read_define(text, "ZONE_LITE_ZKT_PREFERRED_IP"),
        zkt_port=int(read_define(text, "ZONE_LITE_ZKT_PORT")),
        zkt_comm_key=int(read_define(text, "ZONE_LITE_ZKT_COMM_KEY")),
        zone_id=read_define(text, "ZONE_LITE_ZONE_ID"),
        zone_name=read_define(text, "ZONE_LITE_ZONE_NAME"),
        zone_device_id=read_define(text, "ZONE_LITE_ZONE_DEVICE_ID"),
        ords_base_url=read_define(text, "ZONE_LITE_ORDS_BASE_URL").rstrip("/"),
        ords_username=read_define(text, "ZONE_LITE_ORDS_USERNAME"),
        ords_password=read_define(text, "ZONE_LITE_ORDS_PASSWORD"),
    )


def split_machine_identity(name: str) -> tuple[str, str, bool] | None:
    cleaned = " ".join((name or "").strip().split())
    match = re.match(r"^(?P<name>.+?)(?:-S)?-(?P<cnic>\d{13})$", cleaned)
    if not match:
        return None
    raw_punch = "-S-" in cleaned
    return match.group("name").strip(), match.group("cnic"), raw_punch


def event_uid(device_event_time: str, device_serial: str, user_id: str, punch: Any) -> str:
    import hashlib

    material = json.dumps(
        {
            "device_event_time": device_event_time,
            "device_serial": device_serial,
            "punch": str(punch) if punch is not None else None,
            "user_id": str(user_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(config: Config, serial: str, user: Any, attendance: Any) -> dict[str, Any] | None:
    identity = split_machine_identity(getattr(user, "name", "") if user else "")
    if identity is None:
        return None
    employee_name, cnic, raw_punch = identity
    user_id = str(getattr(attendance, "user_id", "") or getattr(user, "user_id", ""))
    timestamp = iso_utc(getattr(attendance, "timestamp"))
    punch = getattr(attendance, "punch", None)
    status = getattr(attendance, "status", None)
    return {
        "event_uid": event_uid(timestamp, serial, user_id, punch),
        "zone_id": config.zone_id,
        "zone_name": config.zone_name,
        "zone_device_id": config.zone_device_id,
        "device_serial": serial,
        "user_id": user_id,
        "employee_name": employee_name,
        "cnic": cnic,
        "device_event_time": timestamp,
        "status": str(status) if status is not None else None,
        "punch": str(punch) if punch is not None else None,
        "raw_punch": "T" if raw_punch else "F",
        "capturetype": "MANUAL_REPROCESS",
        "trust_status": "BACKFILL_ACCEPTED_CLOCK_OK",
    }


def chunked(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def send_bulk(config: Config, events: list[dict[str, Any]], dry_run: bool) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as attempted:
        for event in events:
            attempted.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    if dry_run:
        return
    headers = {
        "X-API-Username": config.ords_username,
        "X-API-Password": config.ords_password,
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=60) as client:
        for offset, batch in enumerate(chunked(events, 5000)):
            payload = {"batch_uid": f"manual-backfill-{offset}", "events": batch}
            response = client.post(f"{config.ords_base_url}/raw-captures/bulk", headers=headers, json=payload)
            ok = response.status_code in {200, 201, 409}
            print(
                f"chunk offset={offset * 5000} count={len(batch)} "
                f"status={response.status_code} ok={ok}"
            )
            response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Send records to Oracle instead of dry-run only.")
    args = parser.parse_args()

    config = load_config()
    zk = ZK(config.zkt_ip, port=config.zkt_port, password=config.zkt_comm_key, timeout=30)
    conn = zk.connect()
    try:
        conn.disable_device()
        serial = conn.get_serialnumber()
        users = {str(user.user_id): user for user in conn.get_users()}
        attendance = conn.get_attendance()
        events: list[dict[str, Any]] = []
        blocked_identity = 0
        for row in attendance:
            user = users.get(str(row.user_id))
            event = build_event(config, serial, user, row)
            if event is None:
                blocked_identity += 1
                continue
            events.append(event)
        unique = {event["event_uid"]: event for event in events}
        print(
            f"attendance={len(attendance)} valid={len(unique)} "
            f"blocked_identity={blocked_identity} duplicates_in_dump={len(events) - len(unique)} "
            f"users={len(users)}"
        )
        send_bulk(config, list(unique.values()), dry_run=not args.send)
        if args.send:
            print(f"sent={len(unique)}")
    finally:
        try:
            conn.enable_device()
        finally:
            conn.disconnect()


if __name__ == "__main__":
    main()

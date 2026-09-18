"""Vendor source identity shared by Hikvision qualification and future ingestion.

These helpers do not certify a model, infer a successful-authentication allowlist,
or deliver to ADD/Oracle. Ingestion must supply a verified device/source epoch and
an explicitly qualified event-code mapping before releasing attendance.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


class SourceIdentityError(ValueError):
    pass


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceObservation:
    terminal_serial: str
    source_epoch: str
    serial_no: int
    major: int
    minor: int
    employee_no: str | None
    event_time_utc: str
    timezone_assumed: bool
    channel: str
    attendance_status: str | None

    @property
    def event_uid(self) -> str:
        # Deliberately excludes names, CNIC, time, capture channel and currentEvent.
        # A changed physical fact for this key is a conflict, not a second punch.
        return canonical_digest([
            "hikvision-source-v1", self.terminal_serial, self.source_epoch, self.serial_no,
        ])

    @property
    def immutable_facts_digest(self) -> str:
        return canonical_digest([
            "hikvision-facts-v1", self.event_uid, self.major, self.minor,
            self.employee_no, self.event_time_utc,
        ])

    def disposition(self, qualified_success_codes: frozenset[tuple[int, int]]) -> str:
        if (self.major, self.minor) not in qualified_success_codes:
            # Unknown codes require review, not guessed attendance or exclusion.
            return "UNCLASSIFIED"
        if not self.employee_no:
            return "BLOCKED_IDENTITY"
        return "ATTENDANCE_CANDIDATE"


def normalize_observation(
    payload: dict, *, terminal_serial: str, source_epoch: str,
    site_timezone: str = "Asia/Karachi",
) -> SourceObservation:
    if not all(isinstance(value, str) and value and "\x00" not in value
               for value in (terminal_serial, source_epoch)):
        raise SourceIdentityError("VERIFIED_TERMINAL_AND_EPOCH_REQUIRED")
    if not isinstance(payload, dict):
        raise SourceIdentityError("INVALID_EVENT_OBJECT")
    is_xml = "EventNotificationAlert" in payload
    if is_xml:
        payload = payload["EventNotificationAlert"]
        if not isinstance(payload, dict):
            raise SourceIdentityError("INVALID_EVENT_OBJECT")
    is_stream = "AccessControllerEvent" in payload
    event = payload.get("AccessControllerEvent") if is_stream else payload
    if not isinstance(event, dict):
        raise SourceIdentityError("INVALID_EVENT_OBJECT")
    serial = event.get("serialNo")
    major = event.get("majorEventType" if is_stream else "major")
    minor = event.get("subEventType" if is_stream else "minor")
    if is_xml:
        serial, major, minor = (
            int(value) if isinstance(value, str) and value.isascii() and value.isdigit()
            and len(value) <= 10 else value for value in (serial, major, minor)
        )
    if type(serial) is not int or not 1 <= serial <= 3_000_000_000:
        raise SourceIdentityError("STABLE_SOURCE_SERIAL_REQUIRED")
    if any(type(value) is not int or value < 0 for value in (major, minor)):
        raise SourceIdentityError("INVALID_EVENT_CODES")
    # The qualified target provides employeeNoString. Do not coerce a numeric
    # employeeNo because a numeric representation cannot preserve leading zeros.
    employee = event.get("employeeNoString")
    if employee == "":
        employee = None
    if employee is not None and (
        not isinstance(employee, str) or len(employee) > 32 or "\x00" in employee
        or employee != employee.strip()
    ):
        raise SourceIdentityError("INVALID_EMPLOYEE_NUMBER")
    timestamp = payload.get("dateTime") if is_stream else event.get("time")
    try:
        if not isinstance(timestamp, str) or not any(sep in timestamp for sep in ("T", " ")):
            raise ValueError
        parsed = datetime.fromisoformat(timestamp)
        assumed = parsed.tzinfo is None
        if assumed:
            zone = ZoneInfo(site_timezone)
            first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError
            if first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed:
                raise ValueError
            parsed = first
        normalized_time = parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise SourceIdentityError("INVALID_OR_AMBIGUOUS_EVENT_TIME") from None
    current = event.get("currentEvent")
    if is_xml and current in ("true", "false"):
        current = current == "true"
    channel = "HISTORY" if not is_stream else (
        "LIVE" if current is True else "REPLAY" if current is False else "STREAM_UNKNOWN"
    )
    attendance_status = event.get("attendanceStatus")
    if attendance_status in (None, "", "undefined", "unknown"):
        attendance_status = None
    if attendance_status is not None and (
        not isinstance(attendance_status, str) or len(attendance_status) > 40
        or "\x00" in attendance_status
    ):
        raise SourceIdentityError("INVALID_ATTENDANCE_STATUS")
    return SourceObservation(
        terminal_serial=terminal_serial, source_epoch=source_epoch, serial_no=serial,
        major=major, minor=minor, employee_no=employee, event_time_utc=normalized_time,
        timezone_assumed=assumed, channel=channel, attendance_status=attendance_status,
    )

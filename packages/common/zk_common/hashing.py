from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported JSON value: {type(value)!r}")


def canonical_json(payload: Any) -> str:
    """Stable JSON used for event IDs, payload hashes, and audit-chain hashes."""
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def payload_hash(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


def audit_row_hash(
    previous_hash: str | None,
    record_type: str,
    record_id: str | int,
    payload: Any,
) -> str:
    previous_hash = previous_hash or ""
    material = previous_hash + record_type + str(record_id) + canonical_json(payload)
    return sha256_hex(material)


def attendance_event_uid(
    *,
    zone_id: str,
    device_serial: str,
    user_id: str,
    device_event_time: datetime,
    punch: str | int | None,
    source_uid: str | int | None = None,
) -> str:
    return payload_hash(
        {
            "device_serial": device_serial,
            "user_id": str(user_id),
            "device_event_time": device_event_time,
            "punch": None if punch is None else str(punch),
            "source_uid": None if source_uid is None else str(source_uid),
        }
    )

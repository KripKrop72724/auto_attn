from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from zk_add.time_utils import parse_datetime, utc_now


SIGNATURE_SKEW_SECONDS = 300


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_material(
    *, method: str, path: str, timestamp: str, nonce: str, body_hash: str
) -> str:
    # This byte-for-byte format is shared with deployed Zone Lite firmware.
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash])


def sign_request(
    *, token: str, method: str, path: str, timestamp: str, nonce: str, body_hash: str
) -> str:
    material = signature_material(
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body_hash=body_hash,
    )
    return hmac.new(
        token.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def timestamp_within_skew(
    value: str,
    *,
    now: datetime | None = None,
    skew_seconds: int = SIGNATURE_SKEW_SECONDS,
) -> bool:
    timestamp = parse_datetime(value).astimezone(timezone.utc)
    reference = (now or utc_now()).astimezone(timezone.utc)
    return abs(reference - timestamp) <= timedelta(seconds=skew_seconds)

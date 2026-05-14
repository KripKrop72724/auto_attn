from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from zk_common.hashing import sha256_hex
from zk_common.time_utils import parse_datetime, utc_now


SIGNATURE_SKEW_SECONDS = 300
PBKDF2_ITERATIONS = 260_000


def token_hash(token: str) -> str:
    return sha256_hex(token)


def verify_token(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(token_hash(token), expected_hash)


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_material(
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_hash: str,
) -> str:
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash])


def sign_request(
    *,
    token: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_hash: str,
) -> str:
    material = signature_material(
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body_hash=body_hash,
    )
    return hmac.new(token.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_request_signature(
    *,
    token: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_hash: str,
    signature: str,
) -> bool:
    expected = sign_request(
        token=token,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body_hash=body_hash,
    )
    return hmac.compare_digest(signature, expected)


def signed_timestamp() -> str:
    return utc_now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_within_skew(value: str, *, now: datetime | None = None, skew_seconds: int = SIGNATURE_SKEW_SECONDS) -> bool:
    timestamp = parse_datetime(value).astimezone(timezone.utc)
    now = (now or utc_now()).astimezone(timezone.utc)
    return abs(now - timestamp) <= timedelta(seconds=skew_seconds)


def password_hash(password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, expected_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = expected_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected_digest = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected_digest)

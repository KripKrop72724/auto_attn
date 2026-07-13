from __future__ import annotations

import base64
import hmac
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from zk_add.protocol import body_sha256, sign_request, timestamp_within_skew
from zk_add.settings import settings


MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
HKDF_SALT = b"state-life-zone-lite-onboarding-v1"


def normalize_mac(value: str) -> str:
    compact = "".join(character for character in value.lower() if character in "0123456789abcdef")
    if len(compact) != 12:
        raise ValueError("Invalid ESP Wi-Fi MAC address.")
    normalized = ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    if not MAC_RE.fullmatch(normalized):
        raise ValueError("Invalid ESP Wi-Fi MAC address.")
    return normalized


def derive_bootstrap_secret(mac: str, fleet_root_secret: str | None = None) -> str:
    normalized = normalize_mac(mac)
    root = (fleet_root_secret or settings.effective_fleet_root_secret).encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=HKDF_SALT,
        info=f"zone-lite:{normalized}".encode("ascii"),
    ).derive(root)
    return base64.urlsafe_b64encode(derived).decode("ascii")


def verify_onboarding_signature(
    *,
    mac: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    supplied_body_hash: str,
    signature: str,
    body: bytes,
) -> bool:
    if not timestamp_within_skew(
        timestamp, skew_seconds=settings.onboarding_signature_skew_seconds
    ):
        return False
    actual_hash = body_sha256(body)
    if not hmac.compare_digest(actual_hash, supplied_body_hash):
        return False
    expected = sign_request(
        token=derive_bootstrap_secret(mac),
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body_hash=supplied_body_hash,
    )
    return hmac.compare_digest(expected, signature)

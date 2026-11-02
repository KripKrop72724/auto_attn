"""Decrypt the per-board package ADD sends (vendored from provisioning_envelope.py).

Scheme X25519-HKDF-SHA256-AES-256-GCM, unchanged from
firmware/zone_lite/tools/provisioning_envelope.py: a fresh ephemeral key per
payload, HKDF salt sha256(request_id), info
"state-life-zone-lite-provisioning-envelope-v1:<mac>", AAD = canonical JSON.

The recipient key is generated per install and held only in memory: the private
half is never serialised, written or logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .api import EncryptedPackage

REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
ENVELOPE_INFO = b"state-life-zone-lite-provisioning-envelope-v1"
ALGORITHM = "X25519-HKDF-SHA256-AES-256-GCM"
NVS_OFFSET_TEXT = "0x11000"
NVS_SIZE = 0x6000
HMAC_PURPOSE = "zone-lite-nvs-hmac-efuse-key"
HMAC_KEY_SIZE = 32


class EnvelopeError(ValueError):
    """The package failed a check; nothing from it may be written to a board."""


def canonical_aad(values: dict) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_envelope_key(shared_secret: bytes, request_id: str, target_mac: str) -> bytes:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise EnvelopeError("Invalid provisioning request ID")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(request_id.encode("ascii")).digest(),
        info=ENVELOPE_INFO + b":" + target_mac.encode("ascii"),
    ).derive(shared_secret)


def _b64(value: object) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (ValueError, TypeError) as error:
        raise EnvelopeError("Envelope field is not base64") from error


class OneTimeKey:
    """The recipient key for one package. Discard it as soon as the package is open."""

    def __init__(self) -> None:
        self._private: Optional[X25519PrivateKey] = X25519PrivateKey.generate()
        raw = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key_b64 = base64.b64encode(raw).decode("ascii")

    def decrypt(self, ciphertext: bytes, manifest: dict) -> bytes:
        if self._private is None:
            raise EnvelopeError("The one-time key was already discarded")
        envelope = manifest.get("envelope")
        aad_values = manifest.get("aad")
        if not isinstance(envelope, dict) or not isinstance(aad_values, dict):
            raise EnvelopeError("Envelope is incomplete")
        if envelope.get("algorithm") != ALGORITHM:
            raise EnvelopeError("Unsupported provisioning envelope algorithm")
        ephemeral_raw = _b64(envelope.get("ephemeral_public_key_b64"))
        nonce = _b64(envelope.get("nonce_b64"))
        if len(ephemeral_raw) != 32 or len(nonce) != 12:
            raise EnvelopeError("Envelope key or nonce has the wrong size")
        ephemeral = X25519PublicKey.from_public_bytes(ephemeral_raw)
        key = derive_envelope_key(self._private.exchange(ephemeral),
                                  str(aad_values.get("request_id", "")),
                                  str(aad_values.get("target_mac", "")))
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, canonical_aad(aad_values))
        except InvalidTag as error:
            raise EnvelopeError("Package does not decrypt with this install's key") from error

    def discard(self) -> None:
        self._private = None


@dataclass(frozen=True)
class OpenedPackage:
    request_id: str
    nvs: bytes
    hmac_key: Optional[bytes]
    aad: Dict[str, object]

    def __repr__(self) -> str:  # never print the payloads
        return f"OpenedPackage(request_id={self.request_id!r}, nvs=<{len(self.nvs)} bytes>)"


def open_package(package: EncryptedPackage, key: OneTimeKey, *, request_id: str, mac: str,
                 want_hmac_key: bool) -> OpenedPackage:
    """Decrypt and check a package the same way provisioning_envelope.decrypt_package does.

    ``want_hmac_key`` is True only for a new board: no other mode burns a key, so the
    key is not even decrypted then.
    """
    manifest = package.provisioning_manifest
    if not isinstance(manifest, dict):
        raise EnvelopeError("Provisioning manifest is missing")
    aad = manifest.get("aad")
    if not isinstance(aad, dict):
        raise EnvelopeError("Provisioning manifest has no AAD")
    mac = mac.lower()
    if package.request_id != request_id or aad.get("request_id") != request_id:
        raise EnvelopeError("Provisioning package request ID mismatch")
    if str(aad.get("target_mac", "")).lower() != mac:
        raise EnvelopeError("Provisioning package MAC mismatch")
    if aad.get("schema_version") != 1:
        raise EnvelopeError("Unsupported provisioning schema")
    if aad.get("nvs_offset") != NVS_OFFSET_TEXT or aad.get("nvs_size") != NVS_SIZE:
        raise EnvelopeError("Provisioning NVS is not for the settings area at 0x11000")
    if hashlib.sha256(package.provisioning_ciphertext).hexdigest() != manifest.get(
            "ciphertext_sha256"):
        raise EnvelopeError("Provisioning ciphertext hash mismatch")
    nvs = key.decrypt(package.provisioning_ciphertext, manifest)
    if len(nvs) != NVS_SIZE:
        raise EnvelopeError("Provisioning NVS size mismatch")
    if hashlib.sha256(nvs).hexdigest() != aad.get("nvs_sha256"):
        raise EnvelopeError("Provisioning NVS hash mismatch")

    hmac_key: Optional[bytes] = None
    if want_hmac_key:
        hmac_manifest = manifest.get("hmac_key")
        if not isinstance(hmac_manifest, dict) or not package.hmac_ciphertext:
            raise EnvelopeError("Provisioning package has no encrypted security key")
        hmac_aad = hmac_manifest.get("aad")
        if not isinstance(hmac_aad, dict):
            raise EnvelopeError("Security key AAD is missing")
        if hmac_aad.get("request_id") != request_id:
            raise EnvelopeError("Provisioning HMAC request ID mismatch")
        if str(hmac_aad.get("target_mac", "")).lower() != mac:
            raise EnvelopeError("Provisioning HMAC MAC mismatch")
        if hmac_aad.get("purpose") != HMAC_PURPOSE:
            raise EnvelopeError("Provisioning HMAC purpose mismatch")
        if hashlib.sha256(package.hmac_ciphertext).hexdigest() != hmac_manifest.get(
                "ciphertext_sha256"):
            raise EnvelopeError("Provisioning HMAC ciphertext hash mismatch")
        hmac_key = key.decrypt(package.hmac_ciphertext, hmac_manifest)
        if len(hmac_key) != HMAC_KEY_SIZE or hmac_aad.get("key_size") != HMAC_KEY_SIZE:
            raise EnvelopeError("Provisioning HMAC key has the wrong size")
    return OpenedPackage(request_id=request_id, nvs=nvs, hmac_key=hmac_key, aad=dict(aad))

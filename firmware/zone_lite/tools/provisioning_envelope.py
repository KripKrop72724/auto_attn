#!/usr/bin/env python3
"""Encrypt and decrypt device-bound provisioning artifacts.

The ADD runner emits only X25519/AES-256-GCM ciphertext. The one-time private
recipient key never leaves the operator workstation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
ENVELOPE_INFO = b"state-life-zone-lite-provisioning-envelope-v1"


def canonical_aad(values: dict) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_envelope_key(shared_secret: bytes, request_id: str, target_mac: str) -> bytes:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("Invalid provisioning request ID")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(request_id.encode("ascii")).digest(),
        info=ENVELOPE_INFO + b":" + target_mac.encode("ascii"),
    ).derive(shared_secret)


def encrypt_for_recipient(
    plaintext: bytes,
    recipient_public_key_b64: str,
    aad_values: dict,
) -> tuple[bytes, dict]:
    recipient_raw = base64.b64decode(recipient_public_key_b64, validate=True)
    if len(recipient_raw) != 32:
        raise ValueError("Recipient X25519 public key must be 32 bytes")
    recipient = X25519PublicKey.from_public_bytes(recipient_raw)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key = derive_envelope_key(
        ephemeral.exchange(recipient),
        str(aad_values["request_id"]),
        str(aad_values["target_mac"]),
    )
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, canonical_aad(aad_values))
    return ciphertext, {
        "algorithm": "X25519-HKDF-SHA256-AES-256-GCM",
        "ephemeral_public_key_b64": base64.b64encode(ephemeral_public).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
    }


def decrypt_envelope(ciphertext: bytes, private_key: bytes, manifest: dict) -> bytes:
    if len(private_key) != 32:
        raise ValueError("Recipient X25519 private key must be 32 bytes")
    envelope = manifest["envelope"]
    if envelope.get("algorithm") != "X25519-HKDF-SHA256-AES-256-GCM":
        raise ValueError("Unsupported provisioning envelope algorithm")
    aad_values = manifest["aad"]
    private = X25519PrivateKey.from_private_bytes(private_key)
    ephemeral = X25519PublicKey.from_public_bytes(
        base64.b64decode(envelope["ephemeral_public_key_b64"], validate=True)
    )
    nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
    key = derive_envelope_key(
        private.exchange(ephemeral),
        str(aad_values["request_id"]),
        str(aad_values["target_mac"]),
    )
    return AESGCM(key).decrypt(nonce, ciphertext, canonical_aad(aad_values))


def create_keypair(private_path: Path) -> None:
    if private_path.exists():
        raise FileExistsError(f"Refusing to overwrite {private_path}")
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_raw)
    private_path.chmod(0o600)
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    print(base64.b64encode(public_raw).decode("ascii"))


def decrypt_package(
    package: Path,
    private_path: Path,
    output: Path,
    expected_request_id: str,
    expected_mac: str,
) -> None:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    aad_values = manifest.get("aad", {})
    if aad_values.get("request_id") != expected_request_id:
        raise ValueError("Provisioning package request ID mismatch")
    if str(aad_values.get("target_mac", "")).lower() != expected_mac.lower():
        raise ValueError("Provisioning package MAC mismatch")
    ciphertext = (package / "provision.bin.enc").read_bytes()
    if hashlib.sha256(ciphertext).hexdigest() != manifest.get("ciphertext_sha256"):
        raise ValueError("Provisioning ciphertext hash mismatch")
    plaintext = decrypt_envelope(ciphertext, private_path.read_bytes(), manifest)
    if len(plaintext) != int(aad_values.get("nvs_size", -1)):
        raise ValueError("Provisioning NVS size mismatch")
    if hashlib.sha256(plaintext).hexdigest() != aad_values.get("nvs_sha256"):
        raise ValueError("Provisioning NVS hash mismatch")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(plaintext)
    temporary.chmod(0o600)
    os.replace(temporary, output)
    print(f"Decrypted and verified provisioning NVS for {expected_mac}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("--private-key", type=Path, required=True)
    decrypt = subparsers.add_parser("decrypt")
    decrypt.add_argument("--package", type=Path, required=True)
    decrypt.add_argument("--private-key", type=Path, required=True)
    decrypt.add_argument("--output", type=Path, required=True)
    decrypt.add_argument("--expected-request-id", required=True)
    decrypt.add_argument("--expected-mac", required=True)
    args = parser.parse_args()
    if args.command == "keygen":
        create_keypair(args.private_key)
    else:
        decrypt_package(
            args.package,
            args.private_key,
            args.output,
            args.expected_request_id,
            args.expected_mac,
        )


if __name__ == "__main__":
    main()

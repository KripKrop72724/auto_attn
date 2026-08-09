from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PLATFORMS = {"windows-x64": ".exe", "macos-arm64": ".zip"}


def validate_candidate(source: Path, public_key_b64: str) -> dict:
    manifest_path = source / "manifest.json"
    signature_path = source / "manifest.sig"
    if not manifest_path.is_file() or not signature_path.is_file():
        raise ValueError("Companion candidate is missing its manifest or signature.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    try:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )
        key.verify(
            base64.b64decode(signature_path.read_text(encoding="ascii"), validate=True),
            canonical,
        )
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("Companion candidate signature is invalid.") from exc
    platform = str(manifest.get("platform", ""))
    version = str(manifest.get("version", ""))
    filename = Path(str(manifest.get("filename", ""))).name
    if platform not in PLATFORMS:
        raise ValueError("Companion candidate platform is invalid.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Companion candidate version is invalid.")
    if not filename.endswith(PLATFORMS[platform]):
        raise ValueError("Companion candidate filename is invalid.")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_sha", ""))):
        raise ValueError("Companion candidate source SHA is invalid.")
    artifact = source / filename
    if not artifact.is_file():
        raise ValueError("Companion candidate artifact is missing.")
    if artifact.stat().st_size != int(manifest.get("size", -1)):
        raise ValueError("Companion candidate size is invalid.")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, str(manifest.get("sha256", ""))):
        raise ValueError("Companion candidate SHA-256 is invalid.")
    return manifest


def publish(source: Path, store: Path, public_key_b64: str) -> Path:
    source = source.resolve()
    store = store.resolve()
    manifest = validate_candidate(source, public_key_b64)
    platform = str(manifest["platform"])
    version = str(manifest["version"])
    destination = store / platform / version
    manifest_digest = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    if destination.exists():
        existing_manifest = destination / "manifest.json"
        if not existing_manifest.is_file() or not hmac.compare_digest(
            hashlib.sha256(existing_manifest.read_bytes()).hexdigest(), manifest_digest
        ):
            raise ValueError("Published companion version is immutable.")
        validate_candidate(destination, public_key_b64)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{version}-{uuid4().hex}.tmp"
    try:
        shutil.copytree(source, staging)
        validate_candidate(staging, public_key_b64)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--public-key-b64", required=True)
    args = parser.parse_args()
    destination = publish(args.source, args.store, args.public_key_b64)
    print(f"Published immutable companion release {destination.parent.name}/{destination.name}")


if __name__ == "__main__":
    main()

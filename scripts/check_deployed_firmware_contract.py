"""Run inside the deployed ADD image before exposing a new signed package.

Imports deliberately use the installed backend, not the publishing checkout.
This checks admission support and its configured trust root without opening a
database, sending attendance, or changing the firmware store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def check(manifest_path: Path, signature_path: Path, expected_sha256: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("INVALID_MANIFEST_IDENTITY")
    with manifest_path.open("rb") as stream:
        raw = stream.read(65537)
    with signature_path.open("rb") as stream:
        signature = stream.read(8193)
    if (len(raw) > 65536 or len(signature) > 8192
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError("MANIFEST_IDENTITY_MISMATCH")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("INVALID_MANIFEST")
    from zk_add.ota import _verify_manifest
    from zk_add.storage_contract import validate_storage_contract
    from zk_add.terminal_families import release_family

    _verify_manifest(manifest, signature.decode("ascii").strip())
    release_family(manifest)
    validate_storage_contract(manifest, str(manifest.get("version", "")))
    return "ADD_FIRMWARE_CONTRACT_ACCEPTED:" + expected_sha256


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("signature", type=Path)
    parser.add_argument("expected_sha256")
    args = parser.parse_args(argv)
    try:
        result = check(args.manifest, args.signature, args.expected_sha256)
    except Exception:
        # Neither deployed configuration nor arbitrary package text belongs in
        # publication logs. Rejection is a fixed, actionable result.
        print("ADD_FIRMWARE_CONTRACT_REJECTED")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

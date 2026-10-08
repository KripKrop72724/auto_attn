"""Run inside the deployed ADD image before exposing a new signed package.

Imports deliberately use the installed backend, not the publishing checkout.
This checks admission support and its configured trust root. Optional published
HIL validation reads one existing release in a read-only transaction; neither
mode sends attendance, imports releases, or changes the firmware store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def require_published_hil(
    manifest: dict, signature: str, marker: dict, previous_prefix_count: int = 0
) -> str:
    """Inspect existing release only; old catalog after marker replacement is explicit."""
    from sqlalchemy import select, text
    from zk_add.db import SessionLocal
    from zk_add.ota import FirmwareRelease, _parse_release_hil_targets

    identity = tuple(
        manifest.get(key)
        for key in ("release_id", "version", "git_sha", "image_sha256", "application_sha256")
    )
    if (
        type(marker) is not dict
        or type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 2
        or marker.get("target_mac")
        or any(
            marker.get(key) != manifest.get(key)
            for key in ("version", "git_sha", "image_sha256", "application_sha256")
        )
    ):
        raise ValueError("PUBLISHED_HIL_MARKER_REFUSED")
    targets = [
        target.model_dump()
        for target in _parse_release_hil_targets(identity, marker.get("targets"))
    ]
    if (
        type(previous_prefix_count) is not int
        or previous_prefix_count < 0
        or previous_prefix_count >= len(targets)
    ):
        raise ValueError("PUBLISHED_HIL_PREVIOUS_PREFIX_REFUSED")
    if any(key.startswith("_") for key in manifest):
        raise ValueError("SIGNED_MANIFEST_INTERNAL_METADATA_REFUSED")
    with SessionLocal() as session:
        try:
            if session.bind.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            row = session.scalar(
                select(FirmwareRelease).where(
                    FirmwareRelease.release_id == manifest.get("release_id")
                )
            )
            stored = row.manifest if row is not None else None
            if (
                row is None
                or row.state != "HIL_ONLY"
                or row.revoked_at is not None
                or type(stored) is not dict
                or {k: v for k, v in stored.items() if not k.startswith("_")} != manifest
                or {k for k in stored if k.startswith("_")}
                != {"_publication_mode", "_hil_target_mac", "_hil_targets"}
                or stored.get("_publication_mode") != "HIL_ONLY"
                or stored.get("_hil_target_mac")
                or row.manifest_signature.strip() != signature
                or row.version != manifest.get("version")
                or row.git_sha != manifest.get("git_sha")
                or row.image_sha256 != manifest.get("image_sha256")
                or row.image_size != manifest.get("image_size")
                or row.signing_key_id != manifest.get("signing_key_id")
            ):
                raise ValueError("PUBLISHED_HIL_RELEASE_REFUSED")
            if stored.get("_hil_targets") == targets:
                return "CATALOG_CURRENT"
            if (
                previous_prefix_count
                and stored.get("_hil_targets") == targets[:previous_prefix_count]
            ):
                return "CATALOG_REFRESH_PENDING"
            raise ValueError("PUBLISHED_HIL_EXPOSURE_REFUSED")
        finally:
            session.rollback()


def check(
    manifest_path: Path,
    signature_path: Path,
    expected_sha256: str,
    *,
    published_hil: bool = False,
    publication_marker: Path | None = None,
    previous_prefix_count: int = 0,
) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("INVALID_MANIFEST_IDENTITY")
    with manifest_path.open("rb") as stream:
        raw = stream.read(65537)
    with signature_path.open("rb") as stream:
        signature = stream.read(8193)
    if (
        len(raw) > 65536
        or len(signature) > 8192
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
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
    result = "ADD_FIRMWARE_CONTRACT_ACCEPTED:" + expected_sha256
    if published_hil:
        if publication_marker is None:
            raise ValueError("PUBLISHED_HIL_MARKER_REQUIRED")
        with publication_marker.open("rb") as stream:
            raw_marker = stream.read(65537)
        if len(raw_marker) > 65536:
            raise ValueError("PUBLISHED_HIL_MARKER_SIZE")
        result += ":" + require_published_hil(
            manifest,
            signature.decode("ascii").strip(),
            json.loads(raw_marker),
            previous_prefix_count,
        )
    elif publication_marker is not None or previous_prefix_count:
        raise ValueError("PUBLISHED_HIL_MODE_REQUIRED")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-published-hil", action="store_true")
    parser.add_argument("--publication-marker", type=Path)
    parser.add_argument("--previous-prefix-count", type=int, default=0)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("signature", type=Path)
    parser.add_argument("expected_sha256")
    args = parser.parse_args(argv)
    try:
        result = check(
            args.manifest,
            args.signature,
            args.expected_sha256,
            published_hil=args.require_published_hil,
            publication_marker=args.publication_marker,
            previous_prefix_count=args.previous_prefix_count,
        )
    except Exception:
        # Neither deployed configuration nor arbitrary package text belongs in
        # publication logs. Rejection is a fixed, actionable result.
        print("ADD_FIRMWARE_CONTRACT_REJECTED")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

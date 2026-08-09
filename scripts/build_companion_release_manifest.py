from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.platform not in {"windows-x64", "macos-arm64"}:
        raise SystemExit("Unsupported companion release platform")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("Companion release version must be SemVer")
    git_sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
        raise SystemExit("Companion release requires a full lowercase Git SHA")
    key_b64 = os.environ.get("ADD_COMPANION_RELEASE_SIGNING_PRIVATE_KEY_B64")
    if not key_b64:
        raise SystemExit("Companion release signing key is required")
    raw_key = base64.b64decode(key_b64, validate=True)
    key = Ed25519PrivateKey.from_private_bytes(raw_key)
    manifest = {
        "schema_version": 1,
        "platform": args.platform,
        "version": args.version,
        "filename": args.artifact.name,
        "sha256": hashlib.sha256(args.artifact.read_bytes()).hexdigest(),
        "size": args.artifact.stat().st_size,
        "git_sha": git_sha,
        "os_signed": False,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_bytes(canonical)
    (args.output / "manifest.sig").write_text(
        base64.b64encode(key.sign(canonical)).decode("ascii"), encoding="ascii"
    )
    (args.output / args.artifact.name).write_bytes(args.artifact.read_bytes())


if __name__ == "__main__":
    main()

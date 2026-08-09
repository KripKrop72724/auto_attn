from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

OFFSETS = {
    "bootloader-signed.bin": 0x0,
    "partition-table.bin": 0x10000,
    "ota_data_initial.bin": 0x17000,
    "zone-lite-signed.bin": 0x20000,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--vault-manifest", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("Factory version must be SemVer")
    if not re.fullmatch(r"[0-9a-f]{40}", args.git_sha):
        raise SystemExit("Factory source must be a full lowercase Git SHA")
    vault = json.loads(args.vault_manifest.read_text(encoding="utf-8"))
    key_ids = [str(item["key_id"]) for item in vault.get("keys", [])]
    if len(key_ids) != 3 or len(set(key_ids)) != 3:
        raise SystemExit("Factory manifest requires exactly three distinct Secure Boot key IDs")
    images = []
    for name, offset in OFFSETS.items():
        path = args.bundle / name
        if not path.is_file():
            raise SystemExit(f"Factory bundle is missing {name}")
        images.append(
            {
                "name": name,
                "offset": offset,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "bundle_id": f"zone-lite-{args.version}-{args.git_sha[:12]}",
        "hardware_profile": "esp32s3-16mb-zone-lite-v1",
        "version": args.version,
        "git_sha": args.git_sha,
        "partition_layout": "zone-lite-factory-v1",
        "setup_password_supplied": True,
        "signing_key_ids": key_ids,
        "images": images,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    (args.bundle / "manifest.json").write_text(canonical, encoding="utf-8")


if __name__ == "__main__":
    main()

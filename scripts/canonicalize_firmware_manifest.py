"""Write or validate the exact JSON bytes verified by ADD's firmware loader."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise SystemExit("Firmware manifest must be an object")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if args.check:
        if raw != canonical:
            raise SystemExit("Firmware manifest is not in ADD's canonical signed form")
    else:
        args.manifest.write_bytes(canonical)


if __name__ == "__main__":
    main()

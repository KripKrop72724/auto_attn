"""Prepare an immutable, credential-free repair payload after protected signing."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("signed", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    key = (args.signed / "key-1-public.pem").read_bytes()
    if key != (args.baseline / "key-1-public.pem").read_bytes():
        raise SystemExit("Repair signing key differs from the originally installed image")
    baseline = json.loads((args.baseline / "bootstrap-manifest.json").read_text(encoding="utf-8-sig"))
    current = json.loads((args.signed / "bootstrap-manifest.json").read_text(encoding="utf-8-sig"))
    for manifest in (baseline, current):
        if manifest["target_mac"].lower() != "ac:27:6e:a3:0a:58" or manifest["firmware_family"] != "hikvision":
            raise SystemExit("Wrong signed package identity")
    if baseline["git_sha"] != "e3073cff6b3da48858644f371e8143effd46068e":
        raise SystemExit("Unexpected original build")
    for name in ("partition-table.bin", "ota_data_initial.bin"):
        if (args.signed / name).read_bytes() != (args.baseline / name).read_bytes():
            raise SystemExit("Repair changes the installed flash layout")
    args.output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in ("zone-lite-signed.bin", "partition-table.bin", "ota_data_initial.bin", "key-1-public.pem"):
        shutil.copy2(args.signed / name, args.output / name)
        hashes[name] = hashlib.sha256((args.output / name).read_bytes()).hexdigest()
    if hashes["zone-lite-signed.bin"] != current["application_sha256"]:
        raise SystemExit("Repair application manifest hash mismatch")
    meta = {"target_mac": "ac:27:6e:a3:0a:58", "zone": "LF-ZONE-BLD1-01",
            "version": "3.1.1-bld1fix5", "git_sha": current["git_sha"], "files": hashes}
    (args.output / "repair.json").write_text(json.dumps(meta, indent=2) + "\n")

if __name__ == "__main__":
    main()

"""Regression for the 2.6.11 nested-key signature mismatch."""

import json
import subprocess
import sys
from pathlib import Path


def test_manifest_canonicalizer_sorts_nested_predecessor_versions(tmp_path: Path):
    script = Path(__file__).resolve().parents[2] / "scripts/canonicalize_firmware_manifest.py"
    manifest = tmp_path / "manifest.json"
    payload = {
        "version": "2.6.12",
        "queue_storage": {
            "allowed_bootstrap_images": {
                "2.6.9": "a" * 64,
                "2.6.10": "b" * 64,
            }
        },
    }
    manifest.write_text(json.dumps(payload, separators=(",", ":")))

    assert subprocess.run([sys.executable, script, manifest, "--check"], capture_output=True).returncode != 0
    subprocess.run([sys.executable, script, manifest], check=True)
    assert manifest.read_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    subprocess.run([sys.executable, script, manifest, "--check"], check=True)

from __future__ import annotations

import importlib.util
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "zone_lite" / "tools"


def load_envelope():
    path = TOOLS / "provisioning_envelope.py"
    spec = importlib.util.spec_from_file_location("provisioning_envelope", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provisioning_envelope_round_trip_and_aad_binding():
    envelope = load_envelope()
    recipient = X25519PrivateKey.generate()
    public = recipient.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    import base64

    aad = {
        "schema_version": 1,
        "request_id": "request-12345678",
        "target_mac": "e0:72:a1:d7:05:c4",
        "nvs_size": 24576,
    }
    plaintext = b"encrypted-nvs-test-vector"
    ciphertext, metadata = envelope.encrypt_for_recipient(
        plaintext, base64.b64encode(public).decode("ascii"), aad
    )
    private = recipient.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    assert envelope.decrypt_envelope(
        ciphertext, private, {"aad": aad, "envelope": metadata}
    ) == plaintext


def test_workflow_never_uploads_plaintext_nvs():
    workflow = (ROOT / ".github" / "workflows" / "zone-lite-device-provisioning.yml").read_text(
        encoding="utf-8"
    )
    assert "provision.bin.enc" in workflow
    assert "manifest.json" in workflow
    assert "retention-days: 1" in workflow
    assert "path: provisioning-output/" in workflow
    assert "ADD_FLEET_ROOT_SECRET:" not in workflow
    assert "ADD_PII_LOOKUP_KEY:" not in workflow


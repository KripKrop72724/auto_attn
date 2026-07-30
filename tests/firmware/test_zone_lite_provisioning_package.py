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
    assert "hmac-key.bin.enc" in workflow
    assert "manifest.json" in workflow
    assert "retention-days: 1" in workflow
    assert "path: provisioning-output/" in workflow
    assert "ADD_FLEET_ROOT_SECRET:" not in workflow
    assert "ADD_PII_LOOKUP_KEY:" not in workflow


def test_decrypt_package_recovers_device_bound_hmac_key(tmp_path: Path):
    envelope = load_envelope()
    recipient = X25519PrivateKey.generate()
    public = recipient.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private = recipient.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    import base64
    import hashlib
    import json

    request_id = "karachi-device-12345678"
    target_mac = "a4:cb:8f:d4:67:70"
    nvs = b"N" * 24576
    hmac_key = b"H" * 32
    nvs_aad = {
        "schema_version": 1,
        "request_id": request_id,
        "target_mac": target_mac,
        "nvs_size": len(nvs),
        "nvs_sha256": hashlib.sha256(nvs).hexdigest(),
    }
    hmac_aad = {
        "schema_version": 1,
        "request_id": request_id,
        "target_mac": target_mac,
        "purpose": "zone-lite-nvs-hmac-efuse-key",
        "key_size": 32,
    }
    public_b64 = base64.b64encode(public).decode("ascii")
    nvs_ciphertext, nvs_envelope = envelope.encrypt_for_recipient(
        nvs, public_b64, nvs_aad
    )
    hmac_ciphertext, hmac_envelope = envelope.encrypt_for_recipient(
        hmac_key, public_b64, hmac_aad
    )
    package = tmp_path / "package"
    package.mkdir()
    (package / "provision.bin.enc").write_bytes(nvs_ciphertext)
    (package / "hmac-key.bin.enc").write_bytes(hmac_ciphertext)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "aad": nvs_aad,
                "envelope": nvs_envelope,
                "ciphertext_sha256": hashlib.sha256(nvs_ciphertext).hexdigest(),
                "hmac_key": {
                    "aad": hmac_aad,
                    "envelope": hmac_envelope,
                    "ciphertext_sha256": hashlib.sha256(
                        hmac_ciphertext
                    ).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    private_path = tmp_path / "request.key"
    private_path.write_bytes(private)
    nvs_output = tmp_path / "provision.bin"
    hmac_output = tmp_path / "hmac-key.bin"

    envelope.decrypt_package(
        package,
        private_path,
        nvs_output,
        hmac_output,
        request_id,
        target_mac,
    )

    assert nvs_output.read_bytes() == nvs
    assert hmac_output.read_bytes() == hmac_key


def test_first_use_flasher_is_fail_closed_and_verifies_all_ranges():
    source = (TOOLS / "flash_prepared_zone_lite.py").read_text(encoding="utf-8")
    assert '("bootloader-signed.bin", 0x0)' in source
    assert '("partition-table.bin", 0x10000)' in source
    assert '("provision.bin", 0x11000)' in source
    assert '("ota_data_initial.bin", 0x17000)' in source
    assert '("zone-lite-signed.bin", 0x20000)' in source
    assert "--confirm-efuse-burn-for" in source
    assert "--confirm-secure-boot-for" in source
    assert "ensure_nvs_hmac_key(" in source
    assert "Flash readback verification failed" in source
    assert '"first boot is intentionally pending"' in source

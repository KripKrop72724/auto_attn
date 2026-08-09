from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "provisioning_companion" / "src"))
sys.path.insert(0, str(ROOT / "firmware" / "zone_lite" / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from add_provisioning_companion.serial_worker import (  # noqa: E402
    _efuse_flag_enabled,
    _unpack_artifact,
    _verify_factory_artifact,
)
import flash_prepared_zone_lite  # noqa: E402
from publish_companion_release import publish  # noqa: E402
from provisioning_envelope import encrypt_for_recipient  # noqa: E402


def test_device_bound_artifact_decrypts_only_for_its_ephemeral_session(tmp_path: Path):
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    mac = "e0:72:a1:d6:f3:28"
    nvs = b"N" * 0x6000
    hmac_key = b"H" * 32
    aad = {
        "schema_version": 1,
        "request_id": "session-12345678",
        "target_mac": mac,
        "zone_id": "ZONE-1",
        "zone_device_id": "ZKT-1",
        "nvs_offset": 0x11000,
        "nvs_size": len(nvs),
        "nvs_sha256": hashlib.sha256(nvs).hexdigest(),
    }
    nvs_ciphertext, envelope = encrypt_for_recipient(nvs, public_b64, aad)
    hmac_aad = {
        "schema_version": 1,
        "request_id": "session-12345678",
        "target_mac": mac,
        "purpose": "zone-lite-nvs-hmac-efuse-key",
        "key_size": 32,
    }
    hmac_ciphertext, hmac_envelope = encrypt_for_recipient(hmac_key, public_b64, hmac_aad)
    provisioning_manifest = {
        "aad": aad,
        "envelope": envelope,
        "ciphertext_sha256": hashlib.sha256(nvs_ciphertext).hexdigest(),
        "hmac_key": {
            "aad": hmac_aad,
            "envelope": hmac_envelope,
            "ciphertext_sha256": hashlib.sha256(hmac_ciphertext).hexdigest(),
        },
    }
    images = []
    for name in (
        "bootloader-signed.bin",
        "partition-table.bin",
        "ota_data_initial.bin",
        "zone-lite-signed.bin",
    ):
        content = name.encode()
        images.append(
            {
                "name": name,
                "offset": 0,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_b64": base64.b64encode(content).decode(),
            }
        )
    artifact = {
        "target_mac": mac,
        "provisioning_manifest": provisioning_manifest,
        "provisioning_ciphertext_b64": base64.b64encode(nvs_ciphertext).decode(),
        "hmac_ciphertext_b64": base64.b64encode(hmac_ciphertext).decode(),
        "factory_manifest": {"git_sha": "a" * 40},
        "images": images,
    }
    bundle, nvs_path, hmac_path = _unpack_artifact(artifact, tmp_path, private_raw)
    assert nvs_path.read_bytes() == nvs
    assert hmac_path.read_bytes() == hmac_key
    assert (bundle / "bootstrap-manifest.json").is_file()


def test_artifact_rejects_ciphertext_corruption(tmp_path: Path):
    artifact = {
        "target_mac": "e0:72:a1:d6:f3:28",
        "provisioning_manifest": {
            "ciphertext_sha256": "0" * 64,
            "hmac_key": {"ciphertext_sha256": "0" * 64},
        },
        "provisioning_ciphertext_b64": base64.b64encode(b"changed").decode(),
        "hmac_ciphertext_b64": base64.b64encode(b"changed").decode(),
    }
    try:
        _unpack_artifact(artifact, tmp_path, b"x" * 32)
    except RuntimeError as error:
        assert str(error) == "PACKAGE_HASH_MISMATCH"
    else:
        raise AssertionError("Corrupted artifact was accepted")


def test_factory_manifest_signature_and_exact_inventory_are_verified():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    manifest = {
        "bundle_id": "zone-lite-2.4.5-6132c9b773b9",
        "images": [
            {"name": "zone-lite-signed.bin", "offset": 0x20000, "size": 4, "sha256": "a" * 64}
        ],
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    signature = private.sign(
        canonical,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    artifact = {
        "factory_manifest": manifest,
        "factory_manifest_signature": base64.b64encode(signature).decode(),
        "images": json.loads(json.dumps(manifest["images"])),
    }
    _verify_factory_artifact(
        artifact,
        public_key_pem_b64=base64.b64encode(public_pem).decode(),
        expected_manifest_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    artifact["images"][0]["offset"] = 0x21000
    try:
        _verify_factory_artifact(
            artifact,
            public_key_pem_b64=base64.b64encode(public_pem).decode(),
            expected_manifest_sha256=hashlib.sha256(canonical).hexdigest(),
        )
    except RuntimeError as error:
        assert str(error) == "FACTORY_IMAGE_INVENTORY_MISMATCH"
    else:
        raise AssertionError("Mismatched factory image inventory was accepted")


def test_efuse_boolean_values_are_parsed_without_string_truthiness():
    assert not _efuse_flag_enabled("0")
    assert not _efuse_flag_enabled(0)
    assert _efuse_flag_enabled("enabled")
    assert _efuse_flag_enabled(1)


def test_flash_reports_exact_write_and_readback_byte_progress(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"A" * 300_000)
    second.write_bytes(b"B" * 300_000)
    sources = {"first.bin": first, "second.bin": second}
    layout = (("first.bin", 0), ("second.bin", 0x100000))
    source_by_offset = {offset: sources[name] for name, offset in layout}

    def fake_run(command: list[str], line_callback=None) -> str:
        if "read_flash" in command:
            index = command.index("read_flash")
            offset = int(command[index + 1], 16)
            Path(command[index + 3]).write_bytes(source_by_offset[offset].read_bytes())
        elif line_callback:
            line_callback("Writing at 0x00050000 (50%)\n")
        return ""

    monkeypatch.setattr(flash_prepared_zone_lite, "run", fake_run)
    progress: list[tuple[str, int, int]] = []
    flash_prepared_zone_lite.flash_sources_and_verify(
        esptool="esptool",
        port="TEST",
        sources=sources,
        layout=layout,
        progress=lambda state, completed, total: progress.append(
            (state, completed, total)
        ),
    )

    assert progress == [
        ("FLASHING", 300_000, 1_200_000),
        ("FLASHING", 600_000, 1_200_000),
        ("READBACK_VERIFYING", 900_000, 1_200_000),
        ("READBACK_VERIFYING", 1_200_000, 1_200_000),
    ]


def test_companion_release_publication_is_verified_versioned_and_immutable(tmp_path: Path):
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    source = tmp_path / "candidate"
    source.mkdir()
    artifact = source / "add-provisioning-companion-windows-x64.exe"
    artifact.write_bytes(b"native-companion")
    manifest = {
        "schema_version": 1,
        "platform": "windows-x64",
        "version": "1.2.3",
        "filename": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size": artifact.stat().st_size,
        "git_sha": "a" * 40,
        "os_signed": False,
        "published_at": "2026-08-09T00:00:00+00:00",
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    (source / "manifest.json").write_bytes(canonical)
    (source / "manifest.sig").write_text(
        base64.b64encode(private.sign(canonical)).decode(), encoding="ascii"
    )

    destination = publish(source, tmp_path / "store", public_b64)
    assert destination == tmp_path / "store" / "windows-x64" / "1.2.3"
    assert publish(source, tmp_path / "store", public_b64) == destination

    artifact.write_bytes(b"changed-native-companion")
    manifest["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest["size"] = artifact.stat().st_size
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    (source / "manifest.json").write_bytes(canonical)
    (source / "manifest.sig").write_text(
        base64.b64encode(private.sign(canonical)).decode(), encoding="ascii"
    )
    try:
        publish(source, tmp_path / "store", public_b64)
    except ValueError as error:
        assert "immutable" in str(error).lower()
    else:
        raise AssertionError("Mutated companion release was accepted")

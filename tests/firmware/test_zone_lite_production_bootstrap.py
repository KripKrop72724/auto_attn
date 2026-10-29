from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware" / "zone_lite" / "tools"))

import flash_prepared_zone_lite as flasher  # noqa: E402

MAC = "14:c1:9f:46:ae:1c"


def partition_table(rows) -> bytes:
    table = b""
    for label, kind, subtype, offset, size in rows:
        table += b"\xaa\x50" + struct.pack("<BBII", kind, subtype, offset, size)
        table += label.encode().ljust(16, b"\0") + b"\0" * 4
    return table + b"\xff" * (0xC00 - len(table))


ZONE_LITE_TABLE = [
    ("nvs", 1, 0x02, 0x11000, 0x6000),
    ("otadata", 1, 0x00, 0x17000, 0x2000),
    ("phy_init", 1, 0x01, 0x19000, 0x1000),
    ("factory", 0, 0x00, 0x20000, 0x280000),
    ("ota_0", 0, 0x10, 0x2A0000, 0x280000),
    ("ota_1", 0, 0x11, 0x520000, 0x280000),
    ("storage", 1, 0x82, 0x7A0000, 0x800000),
]


def package(tmp_path: Path, *, production=True, table=ZONE_LITE_TABLE) -> Path:
    bundle = tmp_path / "signed"
    bundle.mkdir()
    application = b"exact published release bytes"
    (bundle / "bootloader-signed.bin").write_bytes(b"bootloader")
    (bundle / "partition-table.bin").write_bytes(partition_table(table))
    (bundle / "ota_data_initial.bin").write_bytes(b"\xff" * 0x2000)
    (bundle / "zone-lite-signed.bin").write_bytes(application)
    manifest = {
        "schema_version": 1, "firmware_family": "zkt", "target_mac": MAC, "git_sha": "a" * 40,
        "bootloader_sha256": hashlib.sha256(b"bootloader").hexdigest(),
        "application_sha256": hashlib.sha256(application).hexdigest(),
    }
    if production:
        manifest["production_release"] = {
            "release_id": "zone-lite-2.7.8", "version": "2.7.8", "git_sha": "b" * 40,
            "image_sha256": manifest["application_sha256"], "application_sha256": "c" * 64,
            "boot_slot": "ota_0",
        }
    (bundle / "bootstrap-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_otadata_selects_ota_0_with_esp_idf_crc():
    data = flasher.otadata_selecting_ota_0()
    assert len(data) == 0x2000
    seq, state, crc = struct.unpack_from("<I20xII", data, 0)
    assert (seq, state) == (1, 2) and (seq - 1) % 2 == 0
    assert crc == zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    assert data[32:] == b"\xff" * (0x2000 - 32)


def test_production_package_boots_the_exact_release_from_ota_0(tmp_path, monkeypatch):
    bundle = package(tmp_path)
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        assert kwargs["sources"]["otadata-ota0.bin"].read_bytes() == flasher.otadata_selecting_ota_0()

    monkeypatch.setattr(flasher, "flash_sources_and_verify", capture)
    flasher.flash_and_verify(esptool="esptool", port="PORT", signed_package=bundle,
                             provision_nvs=tmp_path / "provision.bin")
    (call,) = calls
    assert call["layout"] == flasher.PRODUCTION_LAYOUT
    offsets = [offset for name, offset in call["layout"] if name == "zone-lite-signed.bin"]
    assert offsets == [0x20000, 0x2A0000]
    assert ("otadata-ota0.bin", 0x17000) in call["layout"]
    assert "ota_data_initial.bin" not in dict(call["layout"])


def test_built_package_keeps_the_original_factory_layout(tmp_path, monkeypatch):
    bundle = package(tmp_path, production=False)
    calls = []
    monkeypatch.setattr(flasher, "flash_sources_and_verify", lambda **kwargs: calls.append(kwargs))
    flasher.flash_and_verify(esptool="esptool", port="PORT", signed_package=bundle,
                             provision_nvs=tmp_path / "provision.bin")
    assert calls[0]["layout"] == flasher.FLASH_LAYOUT


@pytest.mark.parametrize("fault", ["slot", "hash", "version", "shape"])
def test_production_release_claim_must_be_exact(tmp_path, fault):
    bundle = package(tmp_path)
    manifest = json.loads((bundle / "bootstrap-manifest.json").read_text())
    release = manifest["production_release"]
    if fault == "slot":
        release["boot_slot"] = "factory"
    elif fault == "hash":
        release["image_sha256"] = "0" * 64
    elif fault == "version":
        release["version"] = "latest"
    else:
        manifest["production_release"] = "2.7.8"
    with pytest.raises(RuntimeError, match="production release is invalid"):
        flasher.production_release(manifest)


def test_production_package_refuses_a_different_partition_layout(tmp_path):
    moved = [row if row[0] != "ota_0" else ("ota_0", 0, 0x10, 0x2B0000, 0x270000) for row in ZONE_LITE_TABLE]
    bundle = package(tmp_path, table=moved)
    nvs = tmp_path / "provision.bin"
    nvs.write_bytes(b"n" * 16)
    hmac = tmp_path / "hmac.bin"
    hmac.write_bytes(b"k" * 32)
    provisioning = tmp_path / "provisioning.json"
    provisioning.write_text(json.dumps({
        "aad": {"firmware_family": "zkt", "target_mac": MAC, "nvs_sha256": hashlib.sha256(b"n" * 16).hexdigest(),
                "nvs_size": 16},
        "hmac_key": {"aad": {"target_mac": MAC, "purpose": "zone-lite-nvs-hmac-efuse-key", "key_size": 32}},
    }))
    arguments = dict(signed_package=bundle, provisioning_manifest=provisioning, provision_nvs=nvs,
                     hmac_key=hmac, mac=MAC, expected_git_sha="a" * 40)
    with pytest.raises(RuntimeError, match="production OTA layout"):
        flasher.validate_inputs(**arguments)
    (bundle / "partition-table.bin").write_bytes(partition_table(ZONE_LITE_TABLE))
    flasher.validate_inputs(**arguments)


def test_bootstrap_carries_only_an_available_exact_store_release():
    signer = (ROOT / "deploy/add/bootstrap-firmware-signing.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/firmware-key-bootstrap.yml").read_text(encoding="utf-8")
    for required in (
        "[string]$ProductionReleaseDirectory = ''",
        "Only a factory bootstrap can carry a production release",
        "@('.hil-only.json', '.diagnostic-only.json')",
        "Production release image does not match its signed manifest",
        "Bootstrap application is not the exact production release image",
        "boot_slot = 'ota_0'",
        "$packageFields.production_release = $productionRelease",
    ):
        assert required in signer, required
    for required in (
        "default: latest",
        "PRODUCTION_RELEASE: ${{ inputs.production_release || 'latest' }}",
        "-not (Test-Path -LiteralPath (Join-Path $_.FullName '.hil-only.json'))",
        "-not $family -or $family -eq 'zkt'",
        "Sort-Object { [version]$_.Name } | Select-Object -Last 1",
        "@productionArguments",
    ):
        assert required in workflow, required

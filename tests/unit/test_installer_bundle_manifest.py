"""Installer bundle manifests: canonical form, signature, layout and refusals.

Also the publisher's other gates: the key pin, the installer app's ota_1
predecessors, deployed ADD's AVAILABLE releases, and the checkout bytes of
everything the installer pins.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_installer_bundle_manifest", ROOT / "scripts/build_installer_bundle_manifest.py")
bundles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundles)
sys.path.insert(0, str(ROOT / "firmware" / "zone_lite" / "tools"))
import flash_prepared_zone_lite as flasher  # noqa: E402

HIK_SOURCE = "dc7d9c3b0de1aa5e1f1b7f279f16e317bf7afe65"
WORKFLOW_SHA = "a" * 40
WORKFLOW = ROOT / ".github/workflows/firmware-key-bootstrap.yml"
SIGNER = ROOT / "deploy/add/bootstrap-firmware-signing.ps1"
# The key every State Life Device Installer pins, as the app ships it.
INSTALLER_KEY = ROOT / "apps/esp_installer/src/sl_installer/resources/firmware-signing-key-1.pem"
# Everything under these folders keeps its exact bytes on a Windows checkout.
BYTE_EXACT_FOLDERS = ("apps/esp_installer/src/sl_installer/resources",
                      "apps/esp_installer/tests/core/fixtures")
# ESP-IDF 5.5.3 gen_esp32part.py --flash-size 16MB of partitions.csv, both at
# main and at the exact Hikvision source.
PARTITION_TABLE_SHA256 = "18d740052be22ec90b06ec63d37df9393788d906c13cebd333ee63ad34154f41"


def esp_image(segment: bytes, chip: int = 9, hash_appended: int = 1) -> bytes:
    header = struct.pack("<BBBBI", 0xE9, 1, 2, 0x4F, 0x40380000)
    header += struct.pack("<B3sHBHH4sB", 0xEE, b"\0\0\0", chip, 0, 0, 0xFFFF, b"\0" * 4,
                          hash_appended)
    image = header + struct.pack("<II", 0x3C000020, len(segment)) + segment
    image += b"\0" * (15 - len(image) % 16)
    checksum = 0xEF
    for byte in segment:
        checksum ^= byte
    image += bytes([checksum])
    return image + hashlib.sha256(image).digest()


def signed(image: bytes, keys: int) -> bytes:
    """espsecure sign_data --version 2 layout, with fake key material."""
    image += b"\xff" * (-len(image) % 4096)
    digest = hashlib.sha256(image).digest()
    sector = b""
    for key in range(keys):
        block = struct.pack("<BBxx32s", 0xE7, 0x02, digest) + bytes([0x41 + key]) * 1160
        sector += block + struct.pack("<I", zlib.crc32(block)) + b"\0" * 16
    return image + sector + b"\xff" * (4096 - len(sector))


def application(project: str, version: str, keys: int = 1, filler: bytes = b"") -> bytes:
    descriptor = struct.pack("<II8x", 0xABCD5432, 0)
    descriptor += version.encode().ljust(32, b"\0") + project.encode().ljust(32, b"\0")
    body = descriptor + hashlib.sha256(version.encode()).digest() * 8 + filler
    return signed(esp_image(body), keys)


def empty_storage() -> bytes:
    """spiffsgen.py's empty 8 MiB SPIFFS: one magic per 4 KiB block."""
    blocks = bundles.STORAGE_SIZE // 4096
    return b"".join(
        b"\xff" * 252 + struct.pack("<H", (0x20140529 ^ 256 ^ (blocks - index)) & 0x1FFFF)
        + b"\xff" * 3842 for index in range(blocks))


def esp_application_sha256(data: bytes) -> str:
    """sign-firmware-release.ps1's Get-EspApplicationSha256, transcribed."""
    offset = 24
    for _ in range(data[1]):
        offset += 8 + struct.unpack_from("<I", data, offset + 4)[0]
    hash_offset = -(-(offset + 1) // 16) * 16
    return data[hash_offset:hash_offset + 32].hex()


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=3072) for _ in range(2)]


def public_pem(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(directory: Path, key, payload: bytes | None = None) -> None:
    payload = (directory / "manifest.json").read_bytes() if payload is None else payload
    signature = key.sign(payload, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                         hashes.SHA256())
    (directory / "manifest.sig").write_text(base64.b64encode(signature).decode())


@pytest.fixture
def inputs(tmp_path):
    files = {
        "bootloader": signed(esp_image(b"synthetic bootloader".ljust(64, b"\0")), 3),
        "partition-table": bundles.production_partition_table(),
        "storage": empty_storage(),
        "zkt": application("zone_lite", "2.7.8"),
        "predecessor": application("zone_lite", "2.5.2"),
        "hikvision": application("zone_lite_hikvision", "3.1.1"),
    }
    paths = {}
    for name, data in files.items():
        paths[name] = tmp_path / f"{name}.bin"
        paths[name].write_bytes(data)
    return paths


def claimed(path: Path, version: str):
    """What a signed release manifest would claim for this image."""
    try:
        return bundles.app_identity(path.read_bytes())[1:]
    except bundles.BundleError:
        return version, "0" * 64


def zkt_arguments(inputs, **change):
    factory_version, factory_digest = claimed(inputs["zkt"], "2.7.8")
    predecessor_version, predecessor_digest = claimed(inputs["predecessor"], "2.5.2")
    arguments = dict(
        family="zkt", git_sha=WORKFLOW_SHA, bootloader=inputs["bootloader"],
        partition_table=inputs["partition-table"], factory=inputs["zkt"], storage=inputs["storage"],
        predecessor=inputs["predecessor"], factory_version=factory_version,
        factory_app_digest=factory_digest, predecessor_version=predecessor_version,
        predecessor_app_digest=predecessor_digest, created_at="2026-11-01T00:00:00Z")
    arguments.update(change)
    return arguments


def hik_arguments(inputs, **change):
    arguments = dict(
        family="hikvision", git_sha=HIK_SOURCE, bootloader=inputs["bootloader"],
        partition_table=inputs["partition-table"], factory=inputs["hikvision"],
        storage=inputs["storage"], created_at="2026-11-01T00:00:00Z")
    arguments.update(change)
    return arguments


def rewrite(directory: Path, key, change) -> None:
    """Edit the manifest and re-sign it, so only the edited rule can refuse."""
    manifest = json.loads((directory / "manifest.json").read_bytes())
    change(manifest)
    (directory / "manifest.json").write_bytes(bundles.canonical_json(manifest))
    sign(directory, key)


def test_constants_follow_the_firmware_sources():
    rows = []
    with (ROOT / "firmware/zone_lite/partitions.csv").open() as stream:
        for row in csv.reader(line for line in stream if not line.startswith("#")):
            name, kind, subtype, offset, size = (cell.strip() for cell in row[:5])
            types = {"app": 0x00, "data": 0x01}
            subtypes = {"nvs": 0x02, "ota": 0x00, "phy": 0x01, "factory": 0x00,
                        "ota_0": 0x10, "ota_1": 0x11, "spiffs": 0x82}
            rows.append((name, types[kind], subtypes[subtype], int(offset, 16), int(size, 16)))
    assert tuple(rows) == bundles.PRODUCTION_PARTITIONS
    table = bundles.production_partition_table()
    assert len(table) == 3072 and hashlib.sha256(table).hexdigest() == PARTITION_TABLE_SHA256
    assert flasher.partition_entries(table)["ota_1"] == flasher.PRODUCTION_PARTITIONS["ota_1"]
    # The partition table is at CONFIG_PARTITION_TABLE_OFFSET 0x10000, as the
    # flash tool writes it, not 0x8000.
    defaults = (ROOT / "firmware/zone_lite/sdkconfig.defaults").read_text()
    assert "CONFIG_PARTITION_TABLE_OFFSET=0x10000" in defaults.splitlines()
    assert dict(flasher.PRODUCTION_LAYOUT)["partition-table.bin"] == bundles.PARTITION_TABLE_OFFSET
    offsets = {role: offset for role, (_name, offset) in bundles.IMAGES.items()}
    assert offsets["ota_0"] == flasher.OTA_0_OFFSET and offsets["ota_1"] == flasher.OTA_1_OFFSET
    assert offsets["storage"] == flasher.STORAGE_OFFSET
    assert offsets["otadata"] == flasher.OTADATA_OFFSET
    assert hashlib.sha256(empty_storage()).hexdigest() == bundles.EMPTY_STORAGE_SHA256


def test_otadata_is_the_flash_tools_ota_0_selector_or_blank():
    assert bundles.otadata("zkt") == flasher.otadata_selecting_ota_0()
    assert bundles.otadata("hikvision") == b"\xff" * 0x2000


def test_canonical_json_is_adds_signed_form():
    manifest = {"b": [1, {"d": "é", "c": True}], "a": "x"}
    raw = bundles.canonical_json(manifest)
    assert raw == json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert bundles.parse_manifest(raw) == manifest
    for bad in (json.dumps(manifest).encode(), b'{"a":"x","a":"y"}', b"[1]", b"\xff",
                json.dumps(manifest, separators=(",", ":")).encode()):
        with pytest.raises(bundles.BundleError):
            bundles.parse_manifest(bad)


def test_app_digest_is_the_appended_sha256_not_the_file_hash(inputs):
    data = inputs["zkt"].read_bytes()
    project, version, digest = bundles.app_identity(data)
    assert (project, version) == ("zone_lite", "2.7.8")
    assert digest == esp_application_sha256(data) != hashlib.sha256(data).hexdigest()
    assert bundles.signature_blocks(data) == 1
    assert bundles.signature_blocks(inputs["bootloader"].read_bytes()) == 3
    identity = bundles.check_identity(inputs["zkt"], "zkt", "2.7.8", digest)
    assert identity == (project, version, digest)
    for family, version_claim, digest_claim, message in (
            ("hikvision", None, None, "project"), ("zkt", "2.7.7", None, "version"),
            ("zkt", "2.7.8", "0" * 64, "digest")):
        with pytest.raises(bundles.BundleError, match=message):
            bundles.check_identity(inputs["zkt"], family, version_claim, digest_claim)


@pytest.mark.parametrize("image, message", [
    pytest.param(lambda: b"\x00" * 64, "not an ESP image", id="no-magic"),
    pytest.param(lambda: esp_image(b"x" * 64, chip=2), "not built for the ESP32-S3", id="esp32"),
    pytest.param(lambda: esp_image(b"x" * 64, hash_appended=0), "no appended SHA-256",
                 id="no-hash"),
    pytest.param(lambda: esp_image(b"x" * 64)[:-1] + bytes([esp_image(b"x" * 64)[-1] ^ 0xFF]),
                 "appended SHA-256 does not match", id="bad-hash"),
    pytest.param(lambda: esp_image(b"x" * 64), "no ESP application descriptor",
                 id="no-descriptor"),
])
def test_app_identity_refuses_images_it_cannot_prove(image, message):
    with pytest.raises(bundles.BundleError, match=message):
        bundles.app_identity(image())


def test_signature_sector_must_cover_the_image():
    image = application("zone_lite", "2.7.8")
    assert bundles.signature_blocks(image[:-4096]) == 0
    moved = bytearray(image)
    moved[100] ^= 0xFF
    with pytest.raises(bundles.BundleError, match="does not cover"):
        bundles.signature_blocks(bytes(moved))
    trailing = bytearray(image)
    trailing[-1] = 0
    with pytest.raises(bundles.BundleError, match="malformed"):
        bundles.signature_blocks(bytes(trailing))


def test_zkt_bundle_matches_design_section_6(tmp_path, inputs, keys):
    output = tmp_path / "bundle"
    manifest = bundles.build_bundle(output, **zkt_arguments(inputs))
    digest = bundles.app_identity(inputs["zkt"].read_bytes())[2]
    predecessor = bundles.app_identity(inputs["predecessor"].read_bytes())[2]
    assert (output / "manifest.json").read_bytes() == bundles.canonical_json(manifest)
    assert {key: value for key, value in manifest.items() if key != "images"} == {
        "schema": "zone-lite-installer-bundle-v1", "bundle_id": f"zkt-2.7.8-{digest[:8]}",
        "firmware_family": "zkt", "label": "ZKT 2.7.8", "version": "2.7.8",
        "release_state": "AVAILABLE", "recommended": True, "git_sha": WORKFLOW_SHA,
        "created_at": "2026-11-01T00:00:00Z", "partition_layout": "zone-lite-ota-v1",
        "notes": "Production release for ZKTeco terminals."}

    def sha(name):
        return hashlib.sha256(inputs[name].read_bytes()).hexdigest()

    def size(name):
        return inputs[name].stat().st_size

    application_fields = {"app_project": "zone_lite", "app_version": "2.7.8", "app_digest": digest}
    assert manifest["images"] == [
        {"role": "bootloader", "name": "bootloader-signed.bin", "offset": 0,
         "size": size("bootloader"), "sha256": sha("bootloader")},
        {"role": "partition-table", "name": "partition-table.bin", "offset": 65536, "size": 3072,
         "sha256": PARTITION_TABLE_SHA256},
        {"role": "otadata", "name": "ota-data.bin", "offset": 94208, "size": 8192,
         "sha256": hashlib.sha256(flasher.otadata_selecting_ota_0()).hexdigest()},
        {"role": "factory", "name": "zone-lite-signed.bin", "offset": 131072, "size": size("zkt"),
         "sha256": sha("zkt"), **application_fields},
        {"role": "ota_0", "name": "zone-lite-signed.bin", "offset": 2752512, "size": size("zkt"),
         "sha256": sha("zkt"), **application_fields},
        {"role": "ota_1", "name": "rollback-predecessor.bin", "offset": 5373952,
         "size": size("predecessor"), "sha256": sha("predecessor"), "app_project": "zone_lite",
         "app_version": "2.5.2", "app_digest": predecessor},
        {"role": "storage", "name": "storage-empty.bin", "offset": 7995392, "size": 8388608,
         "sha256": bundles.EMPTY_STORAGE_SHA256},
    ]
    assert sorted(path.name for path in output.iterdir()) == [
        "bootloader-signed.bin", "manifest.json", "ota-data.bin", "partition-table.bin",
        "rollback-predecessor.bin", "storage-empty.bin", "zone-lite-signed.bin"]
    sign(output, keys[0])
    assert bundles.verify_bundle(output, public_pem(keys[0])) == manifest


def test_hikvision_bundle_boots_factory_from_exact_source(tmp_path, inputs, keys):
    output = tmp_path / "bundle"
    manifest = bundles.build_bundle(output, **hik_arguments(inputs))
    digest = bundles.app_identity(inputs["hikvision"].read_bytes())[2]
    assert manifest["bundle_id"] == f"hikvision-3.1.1-dc7d9c3-{digest[:8]}"
    assert (manifest["release_state"], manifest["recommended"], manifest["git_sha"],
            manifest["label"]) == ("FIELD_BASELINE", True, HIK_SOURCE, "Hikvision 3.1.1")
    assert [(image["role"], image["offset"]) for image in manifest["images"]] == [
        ("bootloader", 0), ("partition-table", 0x10000), ("otadata", 0x17000),
        ("factory", 0x20000), ("storage", 0x7A0000)]
    assert manifest["images"][3]["app_project"] == "zone_lite_hikvision"
    assert (output / "ota-data.bin").read_bytes() == b"\xff" * 0x2000
    sign(output, keys[0])
    assert bundles.verify_bundle(output, public_pem(keys[0]))["bundle_id"] == manifest["bundle_id"]


def test_signature_is_rsa_pss_sha256_salt_32_by_the_pinned_key(tmp_path, inputs, keys):
    output = tmp_path / "bundle"
    bundles.build_bundle(output, **hik_arguments(inputs))
    sign(output, keys[0])
    with pytest.raises(bundles.BundleError, match="signature is invalid"):
        bundles.verify_bundle(output, public_pem(keys[1]))
    raw = (output / "manifest.json").read_bytes()
    salt = keys[0].sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=20),
                        hashes.SHA256())
    (output / "manifest.sig").write_text(base64.b64encode(salt).decode())
    with pytest.raises(bundles.BundleError, match="signature is invalid"):
        bundles.verify_bundle(output, public_pem(keys[0]))
    pretty = json.dumps(json.loads(raw), indent=1).encode()
    sign(output, keys[0], pretty)
    with pytest.raises(bundles.BundleError, match="signature is invalid"):
        bundles.verify_bundle(output, public_pem(keys[0]))
    small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(bundles.BundleError, match="RSA-3072"):
        bundles.verify_signature(raw, b"", public_pem(small))
    for text, message in ((b"not base64!", "not base64"), (base64.b64encode(b"x" * 100),
                                                           "not an RSA-3072 signature")):
        (output / "manifest.sig").write_bytes(text)
        with pytest.raises(bundles.BundleError, match=message):
            bundles.verify_bundle(output)


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(extra=1), "fields are not schema v1"),
    (lambda m: m.pop("notes"), "fields are not schema v1"),
    (lambda m: m.update(schema="zone-lite-installer-bundle-v2"), "schema or partition layout"),
    (lambda m: m.update(partition_layout="zone-lite-factory-v1"), "schema or partition layout"),
    (lambda m: m.update(firmware_family="hik"), "family is unknown"),
    (lambda m: m.update(version="2.7"), "not an exact release version"),
    (lambda m: m.update(git_sha="A" * 40), "full lowercase Git SHA"),
    (lambda m: m.update(release_state="HIL_ONLY"), "release state"),
    (lambda m: m.update(recommended=1), "release state"),
    (lambda m: m.update(created_at="2026-11-01 00:00:00"), "created_at"),
    (lambda m: m.update(label="ZKT 2.8.0"), "label does not match"),
    (lambda m: m.update(notes="line\nbreak"), "notes is invalid"),
    (lambda m: m.update(bundle_id="zkt-2.7.8-00000000"), "ID does not match"),
    (lambda m: m["images"].reverse(), "not the zkt layout"),
    (lambda m: m["images"].pop(5), "not the zkt layout"),
    (lambda m: m["images"][2].update(offset=0x8000), "ota-data.bin at 0x17000"),
    (lambda m: m["images"][1].update(name="../partition-table.bin"), "partition-table.bin at"),
    (lambda m: m["images"][3].update(size=1), "does not match its size and SHA-256"),
    (lambda m: m["images"][6].update(sha256="0" * 64), "does not match its size and SHA-256"),
    (lambda m: m["images"][4].pop("app_digest"), "ota_0 entry fields"),
    (lambda m: m["images"][0].update(app_version="1.0.0"), "bootloader entry fields"),
    (lambda m: m["images"][4].update(app_version="2.7.7"), "descriptor does not match"),
    (lambda m: m["images"][5].update(app_digest="0" * 64), "descriptor does not match"),
])
def test_verify_refuses_every_manifest_rule(tmp_path, inputs, keys, change, message):
    output = tmp_path / "bundle"
    bundles.build_bundle(output, **zkt_arguments(inputs))
    rewrite(output, keys[0], change)
    with pytest.raises(bundles.BundleError, match=message):
        bundles.verify_bundle(output, public_pem(keys[0]))


def test_verify_refuses_unlisted_missing_and_changed_files(tmp_path, inputs, keys):
    output = tmp_path / "bundle"
    bundles.build_bundle(output, **zkt_arguments(inputs))
    sign(output, keys[0])
    (output / "extra.bin").write_bytes(b"x")
    with pytest.raises(bundles.BundleError, match="does not list"):
        bundles.verify_bundle(output, public_pem(keys[0]))
    (output / "extra.bin").unlink()
    signature = (output / "manifest.sig").read_bytes()
    (output / "manifest.sig").unlink()
    with pytest.raises(bundles.BundleError, match="signature is missing"):
        bundles.verify_bundle(output)
    (output / "manifest.sig").write_bytes(signature)
    (output / "rollback-predecessor.bin").unlink()
    with pytest.raises(bundles.BundleError, match="rollback-predecessor.bin is missing"):
        bundles.verify_bundle(output)


@pytest.mark.parametrize("change, message", [
    (dict(predecessor=None), "rollback predecessor"),
    (dict(factory_app_digest="0" * 64), "not the exact signed release"),
    (dict(factory_version="2.7.7"), "not the exact signed release"),
    (dict(predecessor_app_digest="0" * 64), "not the exact signed rollback predecessor"),
    (dict(git_sha="dc7d9c3"), "full lowercase Git SHA"),
    (dict(family="zone"), "Unknown firmware family"),
])
def test_build_refuses_claims_that_are_not_the_signed_releases(tmp_path, inputs, change, message):
    with pytest.raises(bundles.BundleError, match=message):
        bundles.build_bundle(tmp_path / "bundle", **zkt_arguments(inputs, **change))
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("name, data, message", [
    pytest.param("storage", lambda: b"\xff" * 0x800000, "bench-verified empty SPIFFS",
                 id="erased-storage"),
    pytest.param("storage", lambda: empty_storage()[:0x400000], "bench-verified empty SPIFFS",
                 id="half-storage"),
    pytest.param("partition-table", lambda: b"\xff" * 3072, "production OTA layout",
                 id="blank-partition-table"),
    pytest.param("bootloader", lambda: signed(esp_image(b"bootloader".ljust(64, b"\0")), 1),
                 "all three vault keys", id="bootloader-one-key"),
    pytest.param("bootloader", lambda: signed(esp_image(b"b" * 0x10000), 3),
                 "overlaps the partition table", id="bootloader-too-large"),
    pytest.param("predecessor", lambda: application("zone_lite", "2.7.8", filler=b"other"),
                 "must be an older release", id="predecessor-not-older"),
    pytest.param("predecessor", lambda: application("zone_lite_hikvision", "2.5.2"),
                 "not an exact zkt release", id="predecessor-hikvision"),
    pytest.param("zkt", lambda: esp_image(b"unsigned") + b"\xff" * 16,
                 "no ESP application descriptor", id="factory-no-descriptor"),
    pytest.param("zkt", lambda: application("zone_lite", "2.7.8")[:-4096], "not signed",
                 id="factory-unsigned"),
    pytest.param("zkt", lambda: application("zone_lite", "2.7.8", filler=b"\0" * 0x280000),
                 "does not fit its slot", id="factory-too-large"),
])
def test_build_refuses_images_a_new_board_must_never_get(tmp_path, inputs, name, data, message):
    # Each application claims its own version and digest, so only the image
    # rule under test can refuse.
    inputs[name].write_bytes(data())
    with pytest.raises(bundles.BundleError, match=message):
        bundles.build_bundle(tmp_path / "bundle", **zkt_arguments(inputs))
    assert not (tmp_path / "bundle").exists()


def test_build_refuses_a_predecessor_for_hikvision_and_a_used_output(tmp_path, inputs):
    with pytest.raises(bundles.BundleError, match="carries no rollback predecessor"):
        bundles.build_bundle(tmp_path / "bundle",
                             **hik_arguments(inputs, predecessor=inputs["predecessor"]))
    with pytest.raises(bundles.BundleError, match="not an exact hikvision release"):
        bundles.build_bundle(tmp_path / "zkt-as-hik",
                             **hik_arguments(inputs, factory=inputs["zkt"]))
    assert not (tmp_path / "zkt-as-hik").exists()
    used = tmp_path / "used"
    used.mkdir()
    (used / "old.bin").write_bytes(b"old")
    with pytest.raises(bundles.BundleError, match="must be new or empty"):
        bundles.build_bundle(used, **hik_arguments(inputs))
    assert sorted(path.name for path in used.iterdir()) == ["old.bin"]


def test_cli_builds_and_verifies(tmp_path, inputs, keys, capsys):
    output = tmp_path / "bundle"
    arguments = hik_arguments(inputs)
    bundles.main([
        "build", "--output", str(output), "--family", "hikvision", "--git-sha", HIK_SOURCE,
        "--bootloader", str(arguments["bootloader"]),
        "--partition-table", str(arguments["partition_table"]),
        "--factory", str(arguments["factory"]), "--storage", str(arguments["storage"])])
    bundle_id = capsys.readouterr().out.strip()
    assert bundle_id.startswith("hikvision-3.1.1-dc7d9c3-")
    sign(output, keys[0])
    key = tmp_path / "key-1-public.pem"
    key.write_bytes(public_pem(keys[0]))
    bundles.main(["verify", "--bundle", str(output), "--public-key", str(key)])
    assert capsys.readouterr().out.strip() == bundle_id
    bundles.main(["identity", "--image", str(inputs["predecessor"]), "--family", "zkt",
                  "--version", "2.5.2"])
    assert capsys.readouterr().out.startswith("zone_lite 2.5.2 ")
    key.write_bytes(public_pem(keys[1]))
    with pytest.raises(SystemExit, match="signature is invalid"):
        bundles.main(["verify", "--bundle", str(output), "--public-key", str(key)])


# ------------------------------------------------------------ key pin (C2)

def job(workflow: str, name: str) -> str:
    """One job of firmware-key-bootstrap.yml, up to the next job."""
    start = workflow.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", workflow[start + 1:])
    return workflow[start:start + 1 + following.start()] if following else workflow[start:]


def test_spki_names_the_pinned_installer_key_whatever_its_line_endings(tmp_path, keys, capsys):
    pem = INSTALLER_KEY.read_bytes()
    spki = bundles.spki_sha256(pem)
    der = serialization.load_pem_public_key(pem).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert spki == hashlib.sha256(der).hexdigest()
    # A Windows checkout writes CRLF: the file's hash moves, the key's does not.
    unix = pem.replace(b"\r\n", b"\n")
    windows = unix.replace(b"\n", b"\r\n")
    assert hashlib.sha256(windows).digest() != hashlib.sha256(unix).digest()
    rewrapped = (b"-----BEGIN PUBLIC KEY-----\n" + base64.encodebytes(der)
                 + b"-----END PUBLIC KEY-----\n")
    assert bundles.spki_sha256(unix) == bundles.spki_sha256(windows) == spki
    assert bundles.spki_sha256(rewrapped) == spki
    assert bundles.spki_sha256(public_pem(keys[0])) != spki
    # The publisher pins this app's key, and the runbook names the same pin.
    assert f"INSTALLER_SIGNER_SPKI_SHA256: {spki}\n" in WORKFLOW.read_text(encoding="utf-8")
    assert spki in (ROOT / "docs/esp-installer-bundles.md").read_text(encoding="utf-8")
    key = tmp_path / "key-1-public.pem"
    key.write_bytes(windows)
    bundles.main(["spki", "--public-key", str(key)])
    assert capsys.readouterr().out.strip() == spki
    for bad in (b"", b"not a key", pem.replace(b"PUBLIC KEY", b"RSA PUBLIC KEY"), pem + pem,
                pem.replace(b"MII", b"!II", 1),
                b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"):
        with pytest.raises(bundles.BundleError, match="not a PEM public key"):
            bundles.spki_sha256(bad)


def test_installer_bundle_job_pins_the_key_by_spki_before_signing():
    publish = job(WORKFLOW.read_text(encoding="utf-8"), "installer-bundle-publish")
    steps = [
        "Invoke-BundleTool @('spki', '--public-key', (Join-Path $vault 'key-1-public.pem'))",
        "$signerSpki[0] -cne $env:INSTALLER_SIGNER_SPKI_SHA256",
        "./deploy/add/bootstrap-firmware-signing.ps1 @signing",
        "-ExpectedPublicKeySpkiSha256 $env:INSTALLER_SIGNER_SPKI_SHA256",
    ]
    positions = [publish.index(step) for step in steps]
    assert positions == sorted(positions)
    # No PEM file hash pins the key anywhere in the job.
    assert "Get-FileHash" not in publish


@pytest.mark.skipif(shutil.which("git") is None, reason="Git checkout is required")
def test_windows_checkout_keeps_the_pinned_key_and_hashed_files_byte_exact(tmp_path):
    # actions/checkout on windows-2022 runs with core.autocrlf=true. Without a
    # rule, the LF text files here come out CRLF and their SHA-256 changes.
    names = sorted(path.relative_to(ROOT).as_posix() for folder in BYTE_EXACT_FOLDERS
                   for path in (ROOT / folder).rglob("*") if path.is_file())
    assert INSTALLER_KEY.relative_to(ROOT).as_posix() in names
    (tmp_path / "no-hooks").mkdir()
    (tmp_path / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    # A file with no rule, to prove the checkout really converts.
    (tmp_path / "control.txt").write_bytes(b"one\ntwo\n")
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    git = ["git", "-c", "core.autocrlf=true", "-c", "core.eol=crlf",
           "-c", f"core.hooksPath={tmp_path / 'no-hooks'}"]
    for arguments in (["init", "-q"], ["add", "--", ".gitattributes", "control.txt", *names]):
        subprocess.run(git + arguments, cwd=tmp_path, check=True, capture_output=True, timeout=60)
    for name in ("control.txt", *names):
        (tmp_path / name).unlink()
    subprocess.run(git + ["checkout", "--", "control.txt", *names], cwd=tmp_path, check=True,
                   capture_output=True, timeout=60)
    assert (tmp_path / "control.txt").read_bytes() == b"one\r\ntwo\r\n"
    changed = [name for name in names
               if (tmp_path / name).read_bytes() != (ROOT / name).read_bytes()]
    assert changed == []


# ------------------------------------------------- ota_1 predecessors (C3)

def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_app_accepts_exactly_what_the_newest_firmware_qualifies():
    accepted = bundles.installer_predecessors()
    contract = load_module("installer_storage_contract",
                           ROOT / "apps/add_backend/zk_add/storage_contract.py")
    newest = contract.FLEET_PLATFORM_VERSION
    assert accepted == contract.platform_baselines(newest)[1], (
        f"QUALIFIED_PREDECESSORS in the State Life Device Installer must list exactly the ota_1 "
        f"predecessors ZKT {newest} qualifies: ship the app with the new ones (and raise ADD's "
        "installer_min_app_version) in the same change")
    # Each digest is also the one the signing contract names for that version.
    signing: dict = {}
    for version, digest in re.findall(r"'([0-9]+\.[0-9]+\.[0-9]+)'\]? = '([0-9a-f]{64})'",
                                      (ROOT / "deploy/add/firmware-storage-contract.ps1")
                                      .read_text(encoding="utf-8")):
        assert signing.setdefault(version, digest) == digest, version
    assert {version: signing.get(version) for version in accepted} == accepted


def test_predecessor_gate_refuses_what_the_installer_app_refuses(capsys):
    accepted = bundles.installer_predecessors()
    bundles.main(["predecessor", "--version", "2.5.2", "--app-digest", accepted["2.5.2"]])
    assert capsys.readouterr().out.strip() == "2.5.2"
    unknown = "9.9.9"
    assert unknown not in accepted
    for version, digest in ((unknown, accepted["2.5.2"]), ("2.5.2", accepted["2.4.12"]),
                            ("2.5.2", accepted["2.5.2"].upper())):
        refusal = (f"Rollback predecessor {version} is not one "
                   "the State Life Device Installer accepts")
        with pytest.raises(SystemExit, match=re.escape(refusal)):
            bundles.main(["predecessor", "--version", version, "--app-digest", digest])


DIGEST = "4" * 64


@pytest.mark.parametrize("source, message", [
    (f'QUALIFIED_PREDECESSORS = {{"2.5.2": "{DIGEST}"}}', None),
    (f'QUALIFIED_PREDECESSORS: Dict[str, str] = {{"2.5.2": "{DIGEST}"}}', None),
    ("QUALIFIED_PREDECESSORS = load_predecessors()", "not one literal table"),
    ("QUALIFIED_PREDECESSORS = {**OTHERS}", "not one literal table"),
    (f'QUALIFIED_PREDECESSORS = {{"2.5.2": "{DIGEST}"}}\nQUALIFIED_PREDECESSORS = {{}}',
     "not one literal table"),
    ("OTHER = {}", "not one literal table"),
    (f'QUALIFIED_PREDECESSORS = {{"2.5.2": "{DIGEST}", "2.5.2": "{"5" * 64}"}}',
     "not exact versions"),
    (f'QUALIFIED_PREDECESSORS = {{"2.5": "{DIGEST}"}}', "not exact versions"),
    ('QUALIFIED_PREDECESSORS = {"2.5.2": "4b4a"}', "not exact versions"),
    ("QUALIFIED_PREDECESSORS = {}", "not exact versions"),
    ("QUALIFIED_PREDECESSORS = {", "cannot be read"),
])
def test_predecessor_list_is_read_only_from_one_exact_literal(tmp_path, source, message):
    path = tmp_path / "bundles.py"
    path.write_text(source + "\n")
    if message is None:
        assert bundles.installer_predecessors(path) == {"2.5.2": DIGEST}
        bundles.check_installer_predecessor("2.5.2", DIGEST, path)
        return
    with pytest.raises(bundles.BundleError, match=message):
        bundles.check_installer_predecessor("2.5.2", DIGEST, path)


# ------------------------------------------- deployed ADD's releases (C1)

ADD_ROWS = [
    # release_id, version, image_sha256, manifest, state, revoked_at
    ("zone-lite-2.7.8", "2.7.8", "1" * 64,
     {"version": "2.7.8", "firmware_family": "zkt", "application_sha256": "a" * 64},
     "AVAILABLE", None),
    ("zone-lite-2.5.2", "2.5.2", "2" * 64, {"version": "2.5.2", "application_sha256": "b" * 64},
     "AVAILABLE", None),
    # A schema 1 manifest carries no application digest.
    ("zone-lite-2.4.12", "2.4.12", "0" * 64, {"version": "2.4.12"}, "AVAILABLE", None),
    ("zone-lite-2.7.7", "2.7.7", "3" * 64, {"version": "2.7.7", "application_sha256": "c" * 64},
     "REVOKED", "2026-11-01 00:00:00"),
    ("zone-lite-2.7.6", "2.7.6", "4" * 64, {"version": "2.7.6", "application_sha256": "d" * 64},
     "AVAILABLE", "2026-11-01 00:00:00"),
    ("zone-lite-2.8.0", "2.8.0", "5" * 64, {"version": "2.8.0", "application_sha256": "e" * 64},
     "HIL_ONLY", None),
    ("zone-lite-hikvision-3.1.1", "3.1.1", "6" * 64,
     {"version": "3.1.1", "firmware_family": "hikvision", "application_sha256": "f" * 64},
     "AVAILABLE", None),
    ("zone-lite-2.6.15", "2.6.15", "7" * 64, {"version": "2.6.15", "application_sha256": "A" * 64},
     "AVAILABLE", None),
    ("zone-lite-2.6.14", "2.6.14", "8" * 64, {"version": "2.6.13", "application_sha256": "9" * 64},
     "AVAILABLE", None),
]


def add_container(tmp_path: Path, rows, *, table: bool = True) -> Path:
    """A stand-in for the ADD API container: zk_add.db.engine over SQLite."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    database = tmp_path / "add.db"
    engine = sqlalchemy.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        if table:
            connection.execute(sqlalchemy.text(
                "CREATE TABLE add_firmware_releases (release_id VARCHAR(100), version VARCHAR(80), "
                "image_sha256 VARCHAR(64), manifest JSON, state VARCHAR(30), revoked_at DATETIME)"))
        for release_id, version, image, manifest, state, revoked_at in rows:
            connection.execute(sqlalchemy.text(
                "INSERT INTO add_firmware_releases VALUES "
                "(:release_id, :version, :image, :manifest, :state, :revoked_at)"),
                dict(release_id=release_id, version=version, image=image,
                     manifest=json.dumps(manifest), state=state, revoked_at=revoked_at))
    engine.dispose()
    package = tmp_path / "container" / "zk_add"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "db.py").write_text(
        "from sqlalchemy import create_engine\n"
        f"engine = create_engine({f'sqlite:///{database}'!r})\n")
    return package.parent


def container_run(container: Path, calls: list):
    """Run `docker exec <ADD API> python ...` as that container would, locally."""
    def run(command, **options):
        calls.append(command)
        assert command[:5] == ["docker", "exec", bundles.ADD_CONTAINER, "python", "-c"]
        environment = dict(os.environ, PYTHONPATH=str(container))
        return subprocess.run([sys.executable, "-c", *command[5:]], env=environment, **options)
    return run


def test_add_program_lists_only_available_unrevoked_zkt_releases(tmp_path):
    calls: list = []
    releases = bundles.add_releases(run=container_run(add_container(tmp_path, ADD_ROWS), calls))
    assert releases == {
        "2.4.12": {"application_sha256": None, "image_sha256": "0" * 64},
        "2.5.2": {"application_sha256": "b" * 64, "image_sha256": "2" * 64},
        "2.7.8": {"application_sha256": "a" * 64, "image_sha256": "1" * 64},
    }
    # One bounded ASCII argument carries the program; nothing travels on stdin.
    (command,) = calls
    assert command[5] == bundles.ADD_RELEASES_LOADER
    assert base64.b64decode(command[6], validate=True).decode() == bundles.ADD_RELEASES_PROGRAM
    assert len(command[6]) < 8192
    # Read-only on both of ADD's databases, never a write.
    assert "SET TRANSACTION READ ONLY" in bundles.ADD_RELEASES_PROGRAM
    assert "PRAGMA query_only=ON" in bundles.ADD_RELEASES_PROGRAM


def test_add_program_reads_the_real_release_table_columns():
    ota = (ROOT / "apps/add_backend/zk_add/ota.py").read_text(encoding="utf-8")
    start = ota.index("class FirmwareRelease(Base):")
    model = ota[start:ota.index("\nclass ", start + 1)]
    assert '__tablename__ = "add_firmware_releases"' in model
    for column in ("release_id", "version", "image_sha256", "manifest", "state", "revoked_at"):
        assert f"\n    {column}: Mapped[" in model, column


def test_add_program_fails_closed_without_its_table(tmp_path):
    with pytest.raises(bundles.BundleError, match="could not be read"):
        bundles.add_releases(run=container_run(add_container(tmp_path, [], table=False), []))


@pytest.mark.parametrize("outcome, message", [
    (FileNotFoundError("docker"), "could not be read"),
    (subprocess.TimeoutExpired("docker", 120), "could not be read"),
    ((1, b"ADD_ZKT_RELEASES_REJECTED\n"), "could not be read"),
    ((0, b""), "answered without"),
    ((0, b"ADD_ZKT_RELEASES_AVAILABLE:{}\nmore\n"), "answered without"),
    ((0, b"ADD_ZKT_RELEASES_AVAILABLE:[]\n"), "answered without"),
    ((0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.7.8": {"application_sha256": "' + b"a" * 64
      + b'", "image_sha256": "' + b"1" * 64 + b'"}}\n'), "answered without"),
    ((0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.7":{"application_sha256":"' + b"a" * 64
      + b'","image_sha256":"' + b"1" * 64 + b'"}}\n'), "answered without"),
    ((0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.7.8":{"application_sha256":"' + b"A" * 64
      + b'","image_sha256":"' + b"1" * 64 + b'"}}\n'), "answered without"),
    ((0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.7.8":{"image_sha256":"' + b"1" * 64 + b'"}}\n'),
     "answered without"),
    ((0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.7.8":{"application_sha256":"' + b"a" * 64
      + b'","image_sha256":null}}\n'), "answered without"),
])
def test_add_releases_fails_closed(outcome, message):
    def run(command, **options):
        if isinstance(outcome, Exception):
            raise outcome
        return subprocess.CompletedProcess(command, outcome[0], outcome[1],
                                           b"detail ADD never shows")
    with pytest.raises(bundles.BundleError, match=message):
        bundles.add_releases(run=run)


def test_add_releases_needs_a_plain_container_name_and_accepts_an_empty_list():
    empty = subprocess.CompletedProcess([], 0, b"ADD_ZKT_RELEASES_AVAILABLE:{}\n", b"")
    with pytest.raises(bundles.BundleError, match="container name is invalid"):
        bundles.add_releases("add-api; rm", run=lambda command, **options: empty)
    assert bundles.add_releases(run=lambda command, **options: empty) == {}
    schema_1 = subprocess.CompletedProcess([], 0, b'ADD_ZKT_RELEASES_AVAILABLE:{"2.4.12":'
                                           b'{"application_sha256":null,"image_sha256":"'
                                           + b"0" * 64 + b'"}}\n', b"")
    assert bundles.add_releases(run=lambda command, **options: schema_1) == {
        "2.4.12": {"application_sha256": None, "image_sha256": "0" * 64}}


def store_release(root: Path, version: str, image: str, application: str,
                  directory: str = "") -> Path:
    path = root / (directory or version)
    path.mkdir(parents=True)
    manifest = {"version": version, "image_sha256": image}
    if application:
        manifest["application_sha256"] = application
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path


def test_add_available_offers_only_store_releases_with_adds_exact_bytes(tmp_path, monkeypatch,
                                                                        capsys):
    offered = {
        "2.7.8": {"application_sha256": "a" * 64, "image_sha256": "1" * 64},
        "2.5.2": {"application_sha256": "b" * 64, "image_sha256": "2" * 64},
        "2.6.15": {"application_sha256": "c" * 64, "image_sha256": "3" * 64},
        "2.4.12": {"application_sha256": None, "image_sha256": "0" * 64},
        "2.6.14": {"application_sha256": None, "image_sha256": "5" * 64},
    }
    monkeypatch.setattr(bundles, "add_releases", lambda container=bundles.ADD_CONTAINER: offered)
    production = store_release(tmp_path, "2.7.8", "1" * 64, "a" * 64)
    predecessor = store_release(tmp_path, "2.5.2", "2" * 64, "b" * 64)
    schema_1 = store_release(tmp_path, "2.4.12", "0" * 64, "")
    # Revoked or never published: ADD does not list it.
    revoked = store_release(tmp_path, "2.7.7", "4" * 64, "d" * 64)
    # Other bytes or another application than ADD verified, or a directory
    # named for another version.
    rebuilt = store_release(tmp_path, "2.6.15", "9" * 64, "c" * 64)
    relabelled = store_release(tmp_path, "2.6.14", "5" * 64, "e" * 64)
    renamed = store_release(tmp_path / "other", "2.7.8", "1" * 64, "a" * 64, directory="2.7.9")
    bundles.main(["add-available", *map(str, (production, revoked, predecessor, rebuilt, schema_1,
                                              relabelled, renamed))])
    assert capsys.readouterr().out.split() == ["2.7.8", "2.5.2", "2.4.12"]
    bundles.main(["add-available", "--require-all", str(production), str(predecessor)])
    assert capsys.readouterr().out.split() == ["2.7.8", "2.5.2"]
    for refused in (revoked, rebuilt, relabelled, renamed):
        with pytest.raises(SystemExit, match=re.escape(
                f"Release {refused.name} is not AVAILABLE in deployed ADD")):
            bundles.main(["add-available", "--require-all", str(production), str(refused)])

    def unreachable(container=bundles.ADD_CONTAINER):
        raise bundles.BundleError("Deployed ADD's release catalogue could not be read")
    monkeypatch.setattr(bundles, "add_releases", unreachable)
    with pytest.raises(SystemExit, match="could not be read"):
        bundles.main(["add-available", str(production)])


def test_bootstrap_jobs_choose_only_releases_deployed_add_offers():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for name in ("generate-and-sign", "installer-bundle-publish"):
        steps = [
            "$available = @(Get-ChildItem -LiteralPath $env:ADD_FIRMWARE_STORE_HOST_PATH",
            "$offered = @(Invoke-BundleTool (@('add-available') + "
            "@($available | ForEach-Object { $_.FullName })))",
            "$available = @($available | Where-Object { $offered -ccontains $_.Name })",
            "$chosen = if ($env:PRODUCTION_RELEASE -eq 'latest') {",
            "$predecessor = $available | Where-Object { $_.Name -eq $env:ROLLBACK_PREDECESSOR }",
            "./deploy/add/bootstrap-firmware-signing.ps1",
        ]
        body = job(workflow, name)
        positions = [body.index(step) for step in steps]
        assert positions == sorted(positions), name


def test_signer_asks_the_app_and_deployed_add_before_the_vault():
    signer = SIGNER.read_text(encoding="utf-8")
    steps = [
        "'identity',",
        "'predecessor',\n            '--version', $rollbackPredecessor.version, "
        "'--app-digest', $rollbackPredecessor.application_sha256",
        "scripts/check_firmware_identity.py",
        "'add-available',\n            '--require-all', "
        "$production.Directory, $predecessor.Directory",
        "New-Item -ItemType Directory -Path $VaultDirectory -Force",
        "(Unprotect-Key $number)",
    ]
    positions = [signer.index(step) for step in steps]
    assert positions == sorted(positions)

#!/usr/bin/env python3
"""Build or verify a signed, MAC-independent Zone Lite installer bundle.

One bundle per firmware family and version serves every new board: the State
Life Device Installer writes its images and ADD makes only the per-board
settings. docs/esp-installer-design.md section 6 defines the format and
docs/esp-installer-bundles.md how firmware-key-bootstrap.yml publishes it.

``build`` copies the images into an empty directory and writes the canonical
manifest.json that the protected runner then signs with vault key 1.
``verify`` checks a bundle the way its readers must: canonical JSON, the fixed
role and offset layout, every size and SHA-256, the production partition
table, the otadata, the bench-verified empty storage, each application's
descriptor and digest, and, with a public key, the RSA-PSS signature. Only
the standard library is needed, except ``cryptography`` for that signature.

The publisher's other gates, all run before any key is decrypted:
``spki`` names a public key by its SubjectPublicKeyInfo, whatever line endings
its PEM has; ``predecessor`` refuses an ota_1 image the installer app does not
accept; ``add-available`` asks deployed ADD, read-only, which store releases
it still offers (ADD records a revocation only in its database).
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
from typing import Callable, Dict, Iterable, List, Optional, Tuple
import zlib

SCHEMA = "zone-lite-installer-bundle-v1"
PARTITION_LAYOUT = "zone-lite-ota-v1"
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"
# The empty SPIFFS that tools/build_empty_storage_image.py makes for the 8 MiB
# storage partition, bench-verified on new boards on 29 Oct 2026 (add-ci.yml
# pins the same bytes). Zone Lite never formats storage itself.
EMPTY_STORAGE_SHA256 = "f39db7c1c7626ee6156dbab5da7912968040a489d1d927c1fb288d1f42c5ec70"

SECTOR_SIZE = 0x1000
# CONFIG_PARTITION_TABLE_OFFSET; the bootloader must end before it.
PARTITION_TABLE_OFFSET = 0x10000
PARTITION_TABLE_SIZE = 0xC00
OTADATA_SIZE = 0x2000
APP_SLOT_SIZE = 0x280000
STORAGE_SIZE = 0x800000

# role: (file name, flash offset). factory and ota_0 hold the same bytes.
IMAGES = {
    "bootloader": ("bootloader-signed.bin", 0x0),
    "partition-table": ("partition-table.bin", PARTITION_TABLE_OFFSET),
    "otadata": ("ota-data.bin", 0x17000),
    "factory": ("zone-lite-signed.bin", 0x20000),
    "ota_0": ("zone-lite-signed.bin", 0x2A0000),
    "ota_1": ("rollback-predecessor.bin", 0x520000),
    "storage": ("storage-empty.bin", 0x7A0000),
}
# What each family writes, in flash order. A ZKT board boots the exact
# production release from ota_0 beside a qualified predecessor in ota_1 (2.7.x
# and later start storage only then); a Hikvision board boots factory.
FAMILY_ROLES = {
    "zkt": ("bootloader", "partition-table", "otadata", "factory", "ota_0", "ota_1", "storage"),
    "hikvision": ("bootloader", "partition-table", "otadata", "factory", "storage"),
}
APP_ROLES = frozenset({"factory", "ota_0", "ota_1"})
PROJECTS = {"zkt": "zone_lite", "hikvision": "zone_lite_hikvision"}
RELEASE_STATES = {"zkt": "AVAILABLE", "hikvision": "FIELD_BASELINE"}
LABELS = {"zkt": "ZKT", "hikvision": "Hikvision"}
MANIFEST_KEYS = frozenset({
    "schema", "bundle_id", "firmware_family", "label", "version", "release_state",
    "recommended", "git_sha", "created_at", "partition_layout", "notes", "images",
})
IMAGE_KEYS = frozenset({"role", "name", "offset", "size", "sha256"})
APP_KEYS = frozenset({"app_project", "app_version", "app_digest"})
# firmware/zone_lite/partitions.csv: (label, type, subtype, offset, size).
PRODUCTION_PARTITIONS = (
    ("nvs", 0x01, 0x02, 0x11000, 0x6000),
    ("otadata", 0x01, 0x00, 0x17000, OTADATA_SIZE),
    ("phy_init", 0x01, 0x01, 0x19000, 0x1000),
    ("factory", 0x00, 0x00, 0x20000, APP_SLOT_SIZE),
    ("ota_0", 0x00, 0x10, 0x2A0000, APP_SLOT_SIZE),
    ("ota_1", 0x00, 0x11, 0x520000, APP_SLOT_SIZE),
    ("storage", 0x01, 0x82, 0x7A0000, STORAGE_SIZE),
)

ESP_IMAGE_MAGIC = 0xE9
ESP32S3_CHIP_ID = 9
APP_DESCRIPTOR_MAGIC = 0xABCD5432
ESP_OTA_IMG_VALID = 2
# espsecure sign_data --version 2: a 4 KiB sector of 1216-byte RSA blocks.
SIGNATURE_BLOCK_SIZE = 1216
SIGNATURE_BLOCK_MAGIC = 0xE7
SIGNATURE_BLOCK_RSA = 0x02
BOOTLOADER_SIGNATURES = 3  # all three vault keys
RSA_3072_SIGNATURE_SIZE = 384

HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CREATED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
PEM_PUBLIC_KEY = re.compile(
    rb"-----BEGIN PUBLIC KEY-----([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----")
CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")

# Every installed State Life Device Installer refuses an ota_1 image outside
# its QUALIFIED_PREDECESSORS (BUNDLE_NOT_TRUSTED). The publisher reads that
# literal from the app's source, never a copy of it.
INSTALLER_PREDECESSORS = (Path(__file__).resolve().parents[1]
                          / "apps/esp_installer/src/sl_installer/core/bundles.py")

# ADD revokes a release in its database only: the firmware store is mounted
# read-only and never marked. This program runs inside the ADD API container
# (python -c), reads add_firmware_releases in a read-only transaction and
# prints one line: every ZKT release ADD has AVAILABLE and unrevoked, with its
# image SHA-256 and application digest. It prints no other data and no error
# detail.
ADD_CONTAINER = "attendance-device-dashboard-add-api-1"
ADD_RELEASES = "ADD_ZKT_RELEASES_AVAILABLE:"
ADD_RELEASES_PROGRAM = r'''
import json
import re
import sys


def available():
    from sqlalchemy import text
    from zk_add.db import engine

    version_pattern = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    with engine.connect() as connection, connection.begin():
        dialect = connection.dialect.name
        if dialect not in ("postgresql", "sqlite"):
            raise ValueError("DATABASE_DIALECT")
        connection.execute(text("SET TRANSACTION READ ONLY" if dialect == "postgresql"
                                else "PRAGMA query_only=ON"))
        rows = connection.execute(text(
            "SELECT release_id, version, image_sha256, manifest FROM add_firmware_releases "
            "WHERE state = 'AVAILABLE' AND revoked_at IS NULL")).mappings().all()
    releases = {}
    for row in rows:
        manifest, version = row["manifest"], row["version"]
        if isinstance(manifest, str):  # SQLite's JSON driver
            manifest = json.loads(manifest)
        if not (isinstance(manifest, dict) and isinstance(version, str)
                and version_pattern.fullmatch(version)
                and row["release_id"] == "zone-lite-" + version
                and manifest.get("version") == version
                and manifest.get("firmware_family") in (None, "zkt")):
            continue
        # A schema 1 manifest has no application digest; the image hash still
        # names the exact bytes ADD verified.
        image, application = row["image_sha256"], manifest.get("application_sha256") or None
        if (isinstance(image, str) and digest_pattern.fullmatch(image)
                and (application is None or isinstance(application, str)
                     and digest_pattern.fullmatch(application))):
            releases[version] = {"application_sha256": application, "image_sha256": image}
    return releases


try:
    report = "ADD_ZKT_RELEASES_AVAILABLE:" + json.dumps(
        available(), sort_keys=True, separators=(",", ":"))
except Exception:
    print("ADD_ZKT_RELEASES_REJECTED")
    sys.exit(1)
print(report)
'''
# The program travels base64-encoded as one bounded ASCII argument, so no
# Windows stdin or quoting rule can change it.
ADD_RELEASES_LOADER = ("import base64,sys; exec(compile(base64.b64decode(sys.argv[1], "
                       "validate=True), 'add-zkt-releases', 'exec'))")


class BundleError(Exception):
    """A bundle, or an input to one, that must not be published or written."""


def canonical_json(value: object) -> bytes:
    """The exact bytes ADD, the publisher and the installer sign and verify."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def otadata(family: str) -> bytes:
    """The otadata partition a new board gets.

    ZKT: one valid entry that selects ota_0, byte for byte what
    firmware/zone_lite/tools/flash_prepared_zone_lite.py writes (the
    bootloader boots slot (ota_seq - 1) % 2; the CRC is ESP-IDF otatool's).
    Hikvision: blank, as the build's ota_data_initial.bin, so it boots factory.
    """
    if family == "hikvision":
        return b"\xff" * OTADATA_SIZE
    seq = 1
    crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    entry = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", ESP_OTA_IMG_VALID, crc)
    return entry + b"\xff" * (OTADATA_SIZE - len(entry))


def production_partition_table() -> bytes:
    """ESP-IDF 5.5.3 gen_esp32part output for partitions.csv (MD5 row included)."""
    rows = b"".join(
        b"\xaa\x50" + struct.pack("<BBII", kind, subtype, offset, size)
        + label.encode("ascii").ljust(16, b"\0") + b"\0" * 4
        for label, kind, subtype, offset, size in PRODUCTION_PARTITIONS
    )
    rows += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(rows, usedforsecurity=False).digest()
    return rows + b"\xff" * (PARTITION_TABLE_SIZE - len(rows))


def _esp_image(data: bytes, what: str) -> None:
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        raise BundleError(f"{what} is not an ESP image")
    if struct.unpack_from("<H", data, 12)[0] != ESP32S3_CHIP_ID:
        raise BundleError(f"{what} is not built for the ESP32-S3")


def appended_digest(data: bytes, what: str = "Image") -> str:
    """The SHA-256 an ESP image carries after its segments, checked.

    For an application this is what esp_partition_get_sha256() returns
    (bootloader_common_get_sha256_of_partition uses the appended digest), so
    it is what ug_direct_predecessor_matches() and ADD's release manifests
    (application_sha256) compare, never the file hash. The walk is
    sign-firmware-release.ps1's Get-EspApplicationSha256.
    """
    _esp_image(data, what)
    if data[23] != 1:
        raise BundleError(f"{what} carries no appended SHA-256")
    offset = 24
    for _ in range(data[1]):
        if offset + 8 > len(data):
            raise BundleError(f"{what} has a truncated segment header")
        offset += 8 + struct.unpack_from("<I", data, offset + 4)[0]
        if offset > len(data):
            raise BundleError(f"{what} has a truncated segment")
    hash_offset = (offset + 1 + 15) // 16 * 16
    stored = data[hash_offset:hash_offset + 32]
    if len(stored) != 32 or hashlib.sha256(data[:hash_offset]).digest() != stored:
        raise BundleError(f"{what} appended SHA-256 does not match its contents")
    return stored.hex()


def app_identity(data: bytes, what: str = "Application") -> Tuple[str, str, str]:
    """Project name, version and digest of an ESP-IDF application image.

    The descriptor offsets are scripts/check_firmware_identity.py's.
    """
    digest = appended_digest(data, what)
    if len(data) < 112 or struct.unpack_from("<I", data, 32)[0] != APP_DESCRIPTOR_MAGIC:
        raise BundleError(f"{what} has no ESP application descriptor")
    try:
        version = data[48:80].split(b"\0", 1)[0].decode("ascii")
        project = data[80:112].split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        raise BundleError(f"{what} descriptor is not ASCII") from None
    return project, version, digest


def check_identity(image: Path, family: str, version: Optional[str] = None,
                   app_digest: Optional[str] = None) -> Tuple[str, str, str]:
    """An application against the family, version and digest its release claims."""
    identity = app_identity(_read(image, "Application image"), "Application image")
    if identity[0] != PROJECTS[family]:
        raise BundleError("Application project does not match the firmware family")
    if version is not None and identity[1] != version:
        raise BundleError("Application version does not match its release")
    if app_digest is not None and identity[2] != app_digest:
        raise BundleError("Application digest does not match its release")
    return identity


def signature_blocks(data: bytes, what: str = "Image") -> int:
    """Secure Boot V2 RSA signature blocks that espsecure sign_data appended.

    Checks each block's magic, version, CRC and image digest; the RSA
    signatures themselves are what the chip verifies.
    """
    if len(data) < 2 * SECTOR_SIZE or len(data) % SECTOR_SIZE:
        return 0
    sector = data[-SECTOR_SIZE:]
    digest = hashlib.sha256(data[:-SECTOR_SIZE]).digest()
    count = 0
    while count < 3:
        block = sector[count * SIGNATURE_BLOCK_SIZE:(count + 1) * SIGNATURE_BLOCK_SIZE]
        if (block[0] != SIGNATURE_BLOCK_MAGIC or block[1] != SIGNATURE_BLOCK_RSA
                or struct.unpack_from("<I", block, 1196)[0] != zlib.crc32(block[:1196])):
            break
        if block[4:36] != digest:
            raise BundleError(f"{what} Secure Boot signature does not cover its bytes")
        count += 1
    # Unsigned bytes end in ordinary image data; a signed sector pads with 0xFF.
    if count and sector[count * SIGNATURE_BLOCK_SIZE:].strip(b"\xff"):
        raise BundleError(f"{what} Secure Boot signature sector is malformed")
    return count


def bundle_id(family: str, version: str, git_sha: str, app_digest: str) -> str:
    if family == "zkt":
        return f"zkt-{version}-{app_digest[:8]}"
    return f"hikvision-{version}-{git_sha[:7]}-{app_digest[:8]}"


def label(family: str, version: str) -> str:
    return f"{LABELS[family]} {version}"


def notes(family: str, git_sha: str) -> str:
    if family == "zkt":
        return "Production release for ZKTeco terminals."
    return f"Field baseline for Hikvision terminals, built from exact source {git_sha[:7]}."


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _reject_duplicates(pairs):
    fields: Dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise BundleError("Installer bundle manifest repeats a field")
        fields[key] = value
    return fields


def parse_manifest(raw: bytes) -> dict:
    """The manifest, only if its bytes are exactly its canonical form."""
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError):
        raise BundleError("Installer bundle manifest is not valid JSON") from None
    if not isinstance(manifest, dict) or canonical_json(manifest) != raw:
        raise BundleError("Installer bundle manifest is not in canonical signed form")
    return manifest


def _read(path: Path, what: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BundleError(f"{what} is missing")
    return path.read_bytes()


def _text_field(manifest: dict, key: str, limit: int) -> str:
    value = manifest.get(key)
    if (not isinstance(value, str) or len(value) > limit or not TEXT.fullmatch(value)
            or value != value.strip()):
        raise BundleError(f"Installer bundle {key} is invalid")
    return value


def check_bundle(directory: Path, *, signed: bool = True) -> dict:
    """Every rule except the RSA-PSS signature; returns the manifest.

    ``signed`` False is the builder's own check of a bundle that the vault
    has not signed yet.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise BundleError("Installer bundle directory is missing")
    manifest = parse_manifest(_read(directory / MANIFEST_NAME, "Installer bundle manifest"))
    if set(manifest) != MANIFEST_KEYS:
        raise BundleError("Installer bundle manifest fields are not schema v1")
    if (manifest["schema"] != SCHEMA or manifest["partition_layout"] != PARTITION_LAYOUT):
        raise BundleError("Installer bundle schema or partition layout is unknown")
    family = manifest["firmware_family"]
    if family not in FAMILY_ROLES:
        raise BundleError("Installer bundle firmware family is unknown")
    version, git_sha = manifest["version"], manifest["git_sha"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise BundleError("Installer bundle version is not an exact release version")
    if not isinstance(git_sha, str) or not HEX40.fullmatch(git_sha):
        raise BundleError("Installer bundle source is not a full lowercase Git SHA")
    if (manifest["release_state"] != RELEASE_STATES[family]
            or type(manifest["recommended"]) is not bool):
        raise BundleError("Installer bundle release state does not match its family")
    created_at = manifest["created_at"]
    if not isinstance(created_at, str) or not CREATED_AT.fullmatch(created_at):
        raise BundleError("Installer bundle created_at is not a UTC timestamp")
    if _text_field(manifest, "label", 64) != label(family, version):
        raise BundleError("Installer bundle label does not match its family and version")
    _text_field(manifest, "notes", 500)

    images = manifest["images"]
    roles = FAMILY_ROLES[family]
    if (not isinstance(images, list)
            or [image.get("role") if isinstance(image, dict) else None for image in images]
            != list(roles)):
        raise BundleError(f"Installer bundle images are not the {family} layout {', '.join(roles)}")
    contents: Dict[str, bytes] = {}
    entries: Dict[str, dict] = {}
    for image in images:
        role = image["role"]
        name, offset = IMAGES[role]
        if set(image) != (IMAGE_KEYS | APP_KEYS if role in APP_ROLES else IMAGE_KEYS):
            raise BundleError(f"Installer bundle {role} entry fields are not schema v1")
        if image["name"] != name or type(image["offset"]) is not int or image["offset"] != offset:
            raise BundleError(f"Installer bundle {role} must be {name} at {offset:#x}")
        if name not in contents:
            contents[name] = _read(directory / name, f"Installer bundle image {name}")
        data = contents[name]
        if (type(image["size"]) is not int or image["size"] != len(data)
                or not isinstance(image["sha256"], str) or image["sha256"] != sha256_hex(data)):
            raise BundleError(f"Installer bundle {name} does not match its size and SHA-256")
        entries[role] = image

    if signed:
        decode_signature(_read(directory / SIGNATURE_NAME, "Installer bundle signature"))
    expected_files = {MANIFEST_NAME, *contents} | ({SIGNATURE_NAME} if signed else set())
    if {entry.name for entry in directory.iterdir()} != expected_files:
        raise BundleError("Installer bundle directory holds files its manifest does not list")

    bootloader = contents[IMAGES["bootloader"][0]]
    appended_digest(bootloader, "Bootloader")
    if (len(bootloader) > PARTITION_TABLE_OFFSET
            or signature_blocks(bootloader, "Bootloader") != BOOTLOADER_SIGNATURES):
        raise BundleError(
            "Bootloader is not signed by all three vault keys or overlaps the partition table")
    if contents[IMAGES["partition-table"][0]] != production_partition_table():
        raise BundleError("Partition table does not match the production OTA layout")
    if contents[IMAGES["otadata"][0]] != otadata(family):
        raise BundleError(
            "Otadata does not select ota_0" if family == "zkt" else "Otadata is not blank")
    storage = entries["storage"]
    if storage["sha256"] != EMPTY_STORAGE_SHA256 or storage["size"] != STORAGE_SIZE:
        raise BundleError(
            "Storage is not the bench-verified empty SPIFFS of the 8 MiB storage partition")
    for role in roles:
        if role not in APP_ROLES:
            continue
        entry = entries[role]
        data = contents[entry["name"]]
        if len(data) > APP_SLOT_SIZE or signature_blocks(data, f"{role} application") < 1:
            raise BundleError(f"The {role} application is not signed or does not fit its slot")
        identity = app_identity(data, f"The {role} application")
        if identity != (entry["app_project"], entry["app_version"], entry["app_digest"]):
            raise BundleError(f"The {role} application descriptor does not match the manifest")
        if identity[0] != PROJECTS[family] or not VERSION.fullmatch(identity[1]):
            raise BundleError(f"The {role} application is not an exact {family} release")
    factory = entries["factory"]
    if factory["app_version"] != version:
        raise BundleError("Installer bundle version is not its factory application's")
    if family == "zkt":
        same = (IMAGE_KEYS | APP_KEYS) - {"role", "offset"}
        if any(entries["ota_0"][key] != factory[key] for key in same):
            raise BundleError("ota_0 must hold the exact production application of factory")
        predecessor = entries["ota_1"]
        if (version_key(predecessor["app_version"]) >= version_key(version)
                or predecessor["app_digest"] == factory["app_digest"]):
            raise BundleError("The ota_1 rollback predecessor must be an older release")
    if manifest["bundle_id"] != bundle_id(family, version, git_sha, factory["app_digest"]):
        raise BundleError("Installer bundle ID does not match its family, version and application")
    return manifest


def decode_signature(text: bytes) -> bytes:
    try:
        signature = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise BundleError("Installer bundle signature is not base64") from None
    if len(signature) != RSA_3072_SIGNATURE_SIZE:
        raise BundleError("Installer bundle signature is not an RSA-3072 signature")
    return signature


def verify_signature(manifest_bytes: bytes, signature_text: bytes, public_key_pem: bytes) -> None:
    """RSA-PSS SHA-256 with a 32-byte salt over the canonical manifest."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise BundleError("Signature verification needs the cryptography package") from None
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except ValueError:
        raise BundleError("Installer bundle signing key is not a PEM public key") from None
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 3072:
        raise BundleError("Installer bundle signing key is not an RSA-3072 key")
    try:
        public_key.verify(
            decode_signature(signature_text),
            manifest_bytes,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except InvalidSignature:
        raise BundleError("Installer bundle manifest signature is invalid") from None


def verify_bundle(directory: Path, public_key_pem: Optional[bytes] = None) -> dict:
    """A published bundle: every rule, plus the signature when a key is given."""
    manifest = check_bundle(directory, signed=True)
    if public_key_pem is not None:
        # ADD verifies the re-canonicalised manifest; check_bundle proved the
        # file is exactly those bytes.
        verify_signature(
            canonical_json(manifest),
            _read(Path(directory) / SIGNATURE_NAME, "Installer bundle signature"),
            public_key_pem,
        )
    return manifest


def build_bundle(
    output: Path,
    *,
    family: str,
    git_sha: str,
    bootloader: Path,
    partition_table: Path,
    factory: Path,
    storage: Path,
    predecessor: Optional[Path] = None,
    factory_version: Optional[str] = None,
    factory_app_digest: Optional[str] = None,
    predecessor_version: Optional[str] = None,
    predecessor_app_digest: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    """Write an unsigned bundle (images and canonical manifest) into ``output``."""
    if family not in FAMILY_ROLES:
        raise BundleError("Unknown firmware family")
    if not HEX40.fullmatch(git_sha):
        raise BundleError("Installer bundle source must be a full lowercase Git SHA")
    if family == "zkt":
        claims = (predecessor, factory_version, factory_app_digest,
                  predecessor_version, predecessor_app_digest)
        if any(value is None for value in claims):
            raise BundleError(
                "A ZKT bundle needs its production release and ota_1 rollback predecessor, "
                "each with the version and application digest of its signed release manifest")
    elif predecessor is not None:
        raise BundleError("A Hikvision bundle boots factory and carries no rollback predecessor")
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    contents = {
        "bootloader": _read(bootloader, "Signed bootloader"),
        "partition-table": _read(partition_table, "Partition table"),
        "otadata": otadata(family),
        "factory": _read(factory, "Signed factory application"),
        "storage": _read(storage, "Empty storage image"),
    }
    if family == "zkt":
        contents["ota_0"] = contents["factory"]
        contents["ota_1"] = _read(predecessor, "Signed rollback predecessor")
    images = []
    for role in FAMILY_ROLES[family]:
        name, offset = IMAGES[role]
        data = contents[role]
        entry = {"role": role, "name": name, "offset": offset, "size": len(data),
                 "sha256": sha256_hex(data)}
        if role in APP_ROLES:
            project, version, digest = app_identity(data, f"The {role} application")
            entry.update(app_project=project, app_version=version, app_digest=digest)
        images.append(entry)
    factory_entry = images[FAMILY_ROLES[family].index("factory")]
    version = factory_entry["app_version"]
    if family == "zkt":
        predecessor_entry = images[FAMILY_ROLES[family].index("ota_1")]
        if ((factory_entry["app_version"], factory_entry["app_digest"])
                != (factory_version, factory_app_digest)):
            raise BundleError("The production application is not the exact signed release")
        if ((predecessor_entry["app_version"], predecessor_entry["app_digest"])
                != (predecessor_version, predecessor_app_digest)):
            raise BundleError("The ota_1 application is not the exact signed rollback predecessor")
    elif factory_version is not None and version != factory_version:
        raise BundleError("The factory application is not the expected version")
    if not VERSION.fullmatch(version):
        raise BundleError("The factory application version is not an exact release version")
    manifest = {
        "schema": SCHEMA,
        "bundle_id": bundle_id(family, version, git_sha, factory_entry["app_digest"]),
        "firmware_family": family,
        "label": label(family, version),
        "version": version,
        "release_state": RELEASE_STATES[family],
        "recommended": True,
        "git_sha": git_sha,
        "created_at": created_at,
        "partition_layout": PARTITION_LAYOUT,
        "notes": notes(family, git_sha),
        "images": images,
    }

    output = Path(output)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise BundleError("Installer bundle output directory must be new or empty")
    created = not output.exists()
    output.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for image in images:
            target = output / image["name"]
            if target not in written:
                target.write_bytes(contents[image["role"]])
                written.append(target)
        written.append(output / MANIFEST_NAME)
        written[-1].write_bytes(canonical_json(manifest))
        # The bundle must pass exactly the checks every reader runs.
        return check_bundle(output, signed=False)
    except BundleError:
        # Leave nothing that a later step could take for a bundle.
        for path in written:
            path.unlink(missing_ok=True)
        if created:
            output.rmdir()
        raise


def spki_sha256(pem: bytes) -> str:
    """SHA-256 of the DER SubjectPublicKeyInfo that a PEM public key carries.

    This names the key itself. The PEM file's own hash changes with its line
    endings and wrapping (a Windows checkout writes CRLF), so it never pins
    the key every installer trusts.
    """
    match = PEM_PUBLIC_KEY.fullmatch(pem.strip())
    try:
        der = base64.b64decode(b"".join(match.group(1).split()), validate=True) if match else b""
    except (binascii.Error, ValueError):
        der = b""
    if len(der) < 2 or der[0] != 0x30:
        raise BundleError("Installer bundle signing key is not a PEM public key")
    return sha256_hex(der)


def installer_predecessors(source: Path = INSTALLER_PREDECESSORS) -> Dict[str, str]:
    """The app's QUALIFIED_PREDECESSORS: ota_1 version to exact application digest.

    Read as a literal, never imported, so the publisher needs none of the
    app's dependencies and shares its one list.
    """
    what = "The State Life Device Installer's qualified predecessors"
    try:
        tree = ast.parse(_read(source, f"{what} source").decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        raise BundleError(f"{what} cannot be read") from None
    values = [node.value for node in tree.body
              if (isinstance(node, ast.Assign)
                  and [getattr(target, "id", None) for target in node.targets]
                  == ["QUALIFIED_PREDECESSORS"])
              or (isinstance(node, ast.AnnAssign)
                  and getattr(node.target, "id", None) == "QUALIFIED_PREDECESSORS")]
    try:
        if len(values) != 1 or not isinstance(values[0], ast.Dict):
            raise ValueError
        versions = [ast.literal_eval(key) for key in values[0].keys]
        table = ast.literal_eval(values[0])
    except (ValueError, TypeError, SyntaxError):
        raise BundleError(f"{what} are not one literal table") from None
    if (not table or len(versions) != len(table) or any(
            not isinstance(version, str) or not VERSION.fullmatch(version)
            or not isinstance(digest, str) or not HEX64.fullmatch(digest)
            for version, digest in table.items())):
        raise BundleError(f"{what} are not exact versions and application digests")
    return table


def check_installer_predecessor(version: str, app_digest: str,
                                source: Path = INSTALLER_PREDECESSORS) -> None:
    """Refuse an ota_1 image the State Life Device Installer would refuse."""
    if installer_predecessors(source).get(version) != app_digest:
        raise BundleError(
            f"Rollback predecessor {version} is not one the State Life Device Installer accepts")


def parse_add_releases(stdout: bytes) -> Dict[str, Dict[str, Optional[str]]]:
    """Deployed ADD's AVAILABLE ZKT releases, only from its exact one-line answer."""
    lines = stdout.splitlines()
    prefix = ADD_RELEASES.encode("ascii")
    raw = lines[0][len(prefix):] if len(lines) == 1 and lines[0].startswith(prefix) else b""
    try:
        releases = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError, BundleError):
        releases = None
    if (not isinstance(releases, dict) or canonical_json(releases) != raw or any(
            not VERSION.fullmatch(version) or not isinstance(release, dict)
            or set(release) != {"application_sha256", "image_sha256"}
            or not (isinstance(release["image_sha256"], str)
                    and HEX64.fullmatch(release["image_sha256"]))
            or not (release["application_sha256"] is None
                    or isinstance(release["application_sha256"], str)
                    and HEX64.fullmatch(release["application_sha256"]))
            for version, release in releases.items())):
        raise BundleError("Deployed ADD answered without its list of AVAILABLE releases")
    return releases


def add_releases(container: str = ADD_CONTAINER,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                 ) -> Dict[str, Dict[str, Optional[str]]]:
    """Every ZKT release deployed ADD has AVAILABLE and unrevoked, read-only."""
    if not CONTAINER.fullmatch(container):
        raise BundleError("ADD container name is invalid")
    program = base64.b64encode(ADD_RELEASES_PROGRAM.encode("utf-8")).decode("ascii")
    try:
        # The container's stderr is dropped: nothing from ADD's settings or
        # database reaches the job log.
        result = run(["docker", "exec", container, "python", "-c", ADD_RELEASES_LOADER, program],
                     stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        raise BundleError("Deployed ADD's release catalogue could not be read")
    return parse_add_releases(result.stdout)


def available_in_add(directories: Iterable[Path], releases: Dict[str, Dict[str, Optional[str]]],
                     *, require_all: bool = False) -> List[str]:
    """The store release directories deployed ADD offers with these exact bytes.

    A directory counts only when ADD lists its version as AVAILABLE and
    unrevoked with the same image SHA-256 and application digest (none on
    either side for a schema 1 manifest) as the store manifest, and the
    directory is named after that version.
    """
    offered = []
    for directory in directories:
        directory = Path(directory)
        try:
            raw = _read(directory / MANIFEST_NAME, "Release manifest")
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            manifest = None
        version = manifest.get("version") if isinstance(manifest, dict) else None
        if (isinstance(version, str) and directory.name == version and releases.get(version) == {
                "application_sha256": manifest.get("application_sha256") or None,
                "image_sha256": manifest.get("image_sha256")}):
            offered.append(version)
        elif require_all:
            raise BundleError(f"Release {directory.name} is not AVAILABLE in deployed ADD "
                              "(revoked, never published, or other bytes)")
    return offered


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="write an unsigned bundle into a new directory")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--family", choices=sorted(FAMILY_ROLES), required=True)
    build.add_argument("--git-sha", required=True)
    build.add_argument("--bootloader", type=Path, required=True)
    build.add_argument("--partition-table", type=Path, required=True)
    build.add_argument("--factory", type=Path, required=True)
    build.add_argument("--storage", type=Path, required=True)
    build.add_argument("--factory-version")
    build.add_argument("--factory-app-digest")
    build.add_argument("--predecessor", type=Path)
    build.add_argument("--predecessor-version")
    build.add_argument("--predecessor-app-digest")
    build.add_argument("--created-at")
    verify = commands.add_parser("verify", help="check a signed bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--public-key", type=Path,
                        help="PEM public key; verifies the RSA-PSS signature too")
    identity = commands.add_parser("identity", help="check one application image")
    identity.add_argument("--image", type=Path, required=True)
    identity.add_argument("--family", choices=sorted(FAMILY_ROLES), required=True)
    identity.add_argument("--version")
    identity.add_argument("--app-digest")
    fingerprint = commands.add_parser(
        "spki", help="print a PEM public key's SubjectPublicKeyInfo SHA-256")
    fingerprint.add_argument("--public-key", type=Path, required=True)
    accepted = commands.add_parser(
        "predecessor", help="refuse an ota_1 predecessor the installer app does not accept")
    accepted.add_argument("--version", required=True)
    accepted.add_argument("--app-digest", required=True)
    offered = commands.add_parser(
        "add-available", help="print the store releases deployed ADD offers, one per line")
    offered.add_argument("--container", default=ADD_CONTAINER)
    offered.add_argument("--require-all", action="store_true",
                         help="refuse unless ADD offers every release given")
    offered.add_argument("releases", nargs="*", type=Path, help="store release directories")
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            print(" ".join(check_identity(args.image, args.family, args.version, args.app_digest)))
            return
        if args.command == "spki":
            print(spki_sha256(_read(args.public_key, "Signing public key")))
            return
        if args.command == "predecessor":
            check_installer_predecessor(args.version, args.app_digest)
            print(args.version)
            return
        if args.command == "add-available":
            releases = add_releases(args.container)
            for version in available_in_add(args.releases, releases, require_all=args.require_all):
                print(version)
            return
        if args.command == "build":
            manifest = build_bundle(
                args.output,
                family=args.family,
                git_sha=args.git_sha,
                bootloader=args.bootloader,
                partition_table=args.partition_table,
                factory=args.factory,
                storage=args.storage,
                predecessor=args.predecessor,
                factory_version=args.factory_version,
                factory_app_digest=args.factory_app_digest,
                predecessor_version=args.predecessor_version,
                predecessor_app_digest=args.predecessor_app_digest,
                created_at=args.created_at,
            )
        else:
            key = _read(args.public_key, "Signing public key") if args.public_key else None
            manifest = verify_bundle(args.bundle, key)
    except BundleError as error:
        raise SystemExit(str(error)) from None
    print(manifest["bundle_id"])


if __name__ == "__main__":
    main()

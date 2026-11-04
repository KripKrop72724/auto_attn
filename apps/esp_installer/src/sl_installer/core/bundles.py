"""Installer bundles: download, cache and the bundle gate (design sections 4.6 and 6).

A bundle is trusted only after every check below passes, on download and again on
every use:

* the manifest signature: RSA-PSS SHA-256, salt 32, over canonical JSON
  (``json.dumps(sort_keys=True, separators=(",", ":"))``, as ADD's ota.py), by the
  key pinned in resources/firmware-signing-key-1.pem;
* the manifest's structure: schema, layout name, family, version, release state,
  exactly the roles of the family at their production offsets and sizes;
* each file's size and SHA-256;
* the partition table holds the production partitions, and no partition reaches
  past 0xFA0000, where the firmware's redo area starts;
* the images: ESP32-S3 headers, valid appended digests, app descriptors naming the
  family's project and version, Secure Boot V2 signature blocks that verify, a
  bootloader that trusts the pinned key and an app signed by one of its keys;
* ZKT: otadata that selects ota_0, the same app in factory and ota_0, and an ota_1
  image that is a qualified, older predecessor;
* storage: exactly the empty SPIFFS image (sha256 f39db7c1...).

The app carries that 8 MiB storage image gzip-compressed and uses it instead of
downloading when the manifest hash matches. The blank redo area (all 0xFF, sha256
c97fce78...) is never in a bundle: the app makes it from a constant.
"""
from __future__ import annotations

import base64
import errno
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from . import texts
from .api import (BundleInfo, BytesProgressFn, CancelToken, Family, InstallerError,
                  LocalBundle)
from .flashplan import (APP_SLOT_SIZE, BUNDLE_ROLES, OTADATA_SIZE, PARTITION_TABLE_MAX,
                        PRODUCTION_PARTITIONS, REDO_AREA_OFFSET, REDO_AREA_SIZE, ROLE_OFFSETS,
                        START_AREA_SIZE, STORAGE_SIZE, FlashRange, blank_otadata,
                        blank_redo_area, otadata_selecting_ota_0, partition_entries,
                        partitions_end)
from .logging_setup import get_logger
from .validation import version_tuple

log = get_logger("bundles")

SCHEMA = "zone-lite-installer-bundle-v1"
PARTITION_LAYOUT = "zone-lite-ota-v1"
STORAGE_EMPTY_SHA256 = "f39db7c1c7626ee6156dbab5da7912968040a489d1d927c1fb288d1f42c5ec70"
STORAGE_IMAGE_NAME = "storage-empty.bin"
# 0x60000 bytes of 0xFF (flashplan.blank_redo_area).
REDO_AREA_BLANK_SHA256 = "c97fce784428a2e10cd6d4ef97ccc756f2e4c1b146a9b9dc5415d4e3e245b70a"
REDO_AREA_IMAGE_NAME = "redo-area-blank.bin"
INSTALLABLE_STATES: Dict[Family, Tuple[str, ...]] = {
    Family.ZKT: ("AVAILABLE",),
    Family.HIKVISION: ("FIELD_BASELINE", "AVAILABLE"),
}
PROJECTS = {Family.ZKT: "zone_lite", Family.HIKVISION: "zone_lite_hikvision"}
# Exact application digests a ZKT direct-upgrade release accepts in its other OTA
# slot (firmware/zone_lite/main/upgrade_guard.c, ug_direct_predecessor_matches).
# Without one, 2.7.x and later boot to LOCAL_FAILURE and the cause reaches only ADD.
QUALIFIED_PREDECESSORS: Dict[str, str] = {
    "2.4.12": "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589",
    "2.5.2": "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
    "2.6.6": "69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e",
    "2.6.7": "3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c",
    "2.6.8": "fecc5df0223a3c7c8b019a445bcf829bc8d09dd93e920aecc8446908fadeadc6",
    "2.6.9": "ad71339fef6926b21a21a05c1e1c4e30a936e0df5be6160871c7283841ad91b8",
    "2.6.10": "a95370b1487d9c1454dfb932c41432451f24ef69d0abc1292a5c0262c17c243c",
    "2.6.12": "3f9028126c8dde9816486783a27a9802ed41179cd9f8caf9935deb79a0057fc1",
    "2.6.13": "c7be4171153333563f9ed6e43f91376d42dfb9d3ca2faefda2ca6d1ac87b2def",
    "2.6.14": "7d0381fc68a01b34ab6989dfed2b5a1a93f39c293bfd0b7db1734b0871fa6469",
    "2.6.15": "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6",
    "2.7.2": "e908496e0695d8c29e96a6f2db3840060eeab526c4012e29a3deef826f7c8eea",
    "2.7.3": "98855248bb4325103b5253ab4b15f4e939adf3ace89c208d463b09ec9ca21a6a",
    "2.7.4": "3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac",
    "2.7.5": "430e2daf29dd99ff006a476ff584e4201493d661cee67e4b4f9ba15545b17a40",
    "2.7.6": "9d7050768f1cb716e9bc11f9b7b4d20b423b3eccbe1531809130457998e96f0c",
    "2.7.7": "6d9ee63dd7e5917c83a3ada14685405c0081073d593a7901487eb2a3fd2c3bc1",
    "2.7.8": "b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e",
    "2.8.0": "21ac81b292b463cf7fa1bf0d6ce364a49493951836ad962a19dcae049f40ed76",
    "2.8.1": "21238fa36df844642545e8c2f6bcfec03be37470890b776a600c39c0cdd1f5f0",
}

ESP_IMAGE_MAGIC = 0xE9
ESP32S3_CHIP_ID = 9
APP_DESC_MAGIC = 0xABCD5432
SIG_SECTOR = 4096
SIG_BLOCK = 1216
SIG_MAGIC = 0xE7
SIG_VERSION_RSA = 0x02
BOOTLOADER_SIGNATURES = 3

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BUNDLE_ID = re.compile(
    r"^(?:zkt-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{8}"
    r"|hikvision-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7}-[0-9a-f]{8})$")


def _not_trusted(cause: str) -> InstallerError:
    log.error("bundle refused: %s", cause)
    return InstallerError(texts.BUNDLE_NOT_TRUSTED, cause=cause)


def canonical_json(manifest: Mapping[str, object]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def manifest_sha256(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def load_public_key(path: Path) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("pinned firmware signing key must be RSA-3072")
    return key


def verify_manifest_signature(manifest: Mapping[str, object], signature_b64: str,
                              key: rsa.RSAPublicKey) -> None:
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
        key.verify(signature, canonical_json(manifest),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                   hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError) as error:
        raise _not_trusted(f"manifest signature: {type(error).__name__}") from error


# ------------------------------------------------------------- image facts

class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class SignatureBlock:
    modulus: int
    valid: bool


@dataclass(frozen=True)
class ImageFacts:
    chip_id: int
    digest: Optional[str]          # the appended SHA-256 ESP-IDF calls the app digest
    digest_valid: bool
    project: Optional[str]
    version: Optional[str]
    signatures: Tuple[SignatureBlock, ...] = ()
    # esp_image_header_t min_chip_rev_full / max_chip_rev_full (major * 100 + minor);
    # ESP-IDF treats a maximum of 0 or 0xFFFF as not set.
    min_chip_rev: int = 0
    max_chip_rev: int = 0xFFFF


def _c_string(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def parse_image(data: bytes) -> ImageFacts:
    """Header, appended digest, app descriptor and Secure Boot V2 blocks of an image.

    Follows esptool's bin_image (checksum on a 16-byte boundary, then the 32-byte
    digest of everything before it) and the ets_secure_boot_sig_block_t layout.
    """
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        raise ImageError("not an ESP image")
    segments = data[1]
    chip_id = struct.unpack_from("<H", data, 12)[0]
    hash_appended = data[23] == 1
    position = 24
    for _ in range(segments):
        if position + 8 > len(data):
            raise ImageError("truncated segment header")
        _load, size = struct.unpack_from("<II", data, position)
        position += 8 + size
        if position > len(data):
            raise ImageError("truncated segment")
    position += 15 - (position % 16)
    position += 1
    digest = None
    digest_valid = False
    if hash_appended:
        stored = data[position:position + 32]
        if len(stored) == 32:
            digest = stored.hex()
            digest_valid = hashlib.sha256(data[:position]).digest() == stored
    project = version = None
    if len(data) >= 112 and struct.unpack_from("<I", data, 32)[0] == APP_DESC_MAGIC:
        version = _c_string(data[48:80])
        project = _c_string(data[80:112])
    min_rev, max_rev = struct.unpack_from("<HH", data, 15)
    return ImageFacts(chip_id, digest, digest_valid, project, version,
                      signature_blocks(data), min_rev, max_rev)


def header_chip_revisions(data: bytes) -> Tuple[int, int]:
    """(min_chip_rev_full, max_chip_rev_full) of an image's 24-byte header."""
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        raise ImageError("not an ESP image")
    return struct.unpack_from("<HH", data, 15)


APP_ROLES = ("factory", "ota_0", "ota_1")


def chip_revision_window(bundle: LocalBundle) -> Tuple[int, Optional[int]]:
    """The chip revisions every image of ``bundle`` starts on: (lowest, highest or None).

    As ESP-IDF 5.5.3 checks them (bootloader_common_check_chip_revision_validity): the
    bootloader checks its own minimum, and each app's minimum and maximum (a maximum of
    0 or 0xFFFF is not set). The maximum is waived on a chip with the
    DISABLE_WAFER_VERSION_MAJOR eFuse, which the app does not count on.
    """
    checked = check_manifest(bundle.manifest, bundle.info.bundle_id)
    directory = Path(bundle.directory)
    low, high = 0, None
    for role, entry in checked.images.items():
        if role != "bootloader" and role not in APP_ROLES:
            continue
        try:
            with (directory / entry.name).open("rb") as handle:
                minimum, maximum = header_chip_revisions(handle.read(24))
        except (OSError, ImageError) as error:
            raise _not_trusted(f"{role} header: {error}") from error
        low = max(low, minimum)
        if role != "bootloader" and maximum not in (0, 0xFFFF):
            high = maximum if high is None else min(high, maximum)
    return low, high


def signature_blocks(data: bytes) -> Tuple[SignatureBlock, ...]:
    if len(data) < 2 * SIG_SECTOR or len(data) % SIG_SECTOR:
        return ()
    sector = data[-SIG_SECTOR:]
    signed_digest = hashlib.sha256(data[:-SIG_SECTOR]).digest()
    blocks = []
    for index in range(3):
        block = sector[index * SIG_BLOCK:(index + 1) * SIG_BLOCK]
        if block[0] != SIG_MAGIC or block[1] != SIG_VERSION_RSA:
            break
        if struct.unpack_from("<I", block, 1196)[0] != zlib.crc32(block[:1196]) & 0xFFFFFFFF:
            blocks.append(SignatureBlock(0, False))
            continue
        modulus = int.from_bytes(block[36:420], "little")
        exponent = struct.unpack_from("<I", block, 420)[0]
        valid = False
        if block[4:36] == signed_digest:
            try:
                rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
                    block[812:1196][::-1], block[4:36],
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                    utils.Prehashed(hashes.SHA256()))
                valid = True
            except (InvalidSignature, ValueError):
                valid = False
        blocks.append(SignatureBlock(modulus, valid))
    return tuple(blocks)


# --------------------------------------------------------- manifest checks

@dataclass(frozen=True)
class ImageEntry:
    role: str
    name: str
    offset: int
    size: int
    sha256: str
    app_project: Optional[str] = None
    app_version: Optional[str] = None
    app_digest: Optional[str] = None


@dataclass(frozen=True)
class CheckedManifest:
    bundle_id: str
    family: Family
    version: str
    release_state: str
    images: Dict[str, ImageEntry]

    def files(self) -> Dict[str, ImageEntry]:
        """One entry per distinct file name (factory and ota_0 share a file)."""
        unique: Dict[str, ImageEntry] = {}
        for entry in self.images.values():
            unique.setdefault(entry.name, entry)
        return unique


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("not an integer")
    return value


def check_manifest(manifest: Mapping[str, object], bundle_id: Optional[str] = None
                   ) -> CheckedManifest:
    """Structural gate; raises InstallerError(BUNDLE_NOT_TRUSTED) on any deviation."""
    try:
        if manifest.get("schema") != SCHEMA:
            raise ValueError("unknown schema")
        if manifest.get("partition_layout") != PARTITION_LAYOUT:
            raise ValueError("partition layout is not zone-lite-ota-v1")
        identifier = str(manifest.get("bundle_id", ""))
        if not _BUNDLE_ID.fullmatch(identifier) or (bundle_id and identifier != bundle_id):
            raise ValueError("bundle id")
        family = Family(manifest.get("firmware_family"))
        version = str(manifest.get("version", ""))
        if not _VERSION.fullmatch(version):
            raise ValueError("version")
        release_state = str(manifest.get("release_state", ""))
        if release_state not in INSTALLABLE_STATES[family]:
            raise ValueError(f"release state {release_state} is not installable")
        if not _GIT_SHA.fullmatch(str(manifest.get("git_sha", ""))):
            raise ValueError("git sha")
        raw_images = manifest.get("images")
        if not isinstance(raw_images, list):
            raise ValueError("images")
        images: Dict[str, ImageEntry] = {}
        for raw in raw_images:
            if not isinstance(raw, dict):
                raise ValueError("image entry")
            entry = ImageEntry(
                role=str(raw.get("role")), name=str(raw.get("name")),
                offset=_int(raw.get("offset")), size=_int(raw.get("size")),
                sha256=str(raw.get("sha256", "")),
                app_project=raw.get("app_project") if raw.get("app_project") else None,
                app_version=raw.get("app_version") if raw.get("app_version") else None,
                app_digest=raw.get("app_digest") if raw.get("app_digest") else None)
            if entry.role in images:
                raise ValueError(f"duplicate role {entry.role}")
            if not _NAME.fullmatch(entry.name) or not _SHA256.fullmatch(entry.sha256):
                raise ValueError(f"{entry.role} name or hash")
            if entry.offset != ROLE_OFFSETS.get(entry.role, -1):
                raise ValueError(f"{entry.role} offset")
            if entry.app_digest is not None and not _SHA256.fullmatch(str(entry.app_digest)):
                raise ValueError(f"{entry.role} app digest")
            images[entry.role] = entry
    except (ValueError, TypeError) as error:
        raise _not_trusted(f"manifest: {error}") from error

    if sorted(images) != sorted(BUNDLE_ROLES[family]):
        raise _not_trusted(f"manifest roles {sorted(images)}")
    limits = {"bootloader": START_AREA_SIZE, "partition-table": PARTITION_TABLE_MAX,
              "factory": APP_SLOT_SIZE, "ota_0": APP_SLOT_SIZE, "ota_1": APP_SLOT_SIZE}
    for role, entry in images.items():
        if entry.size <= 0 or entry.size > limits.get(role, entry.size):
            raise _not_trusted(f"{role} size")
    if images["otadata"].size != OTADATA_SIZE or images["storage"].size != STORAGE_SIZE:
        raise _not_trusted("otadata or storage size")
    if images["storage"].sha256 != STORAGE_EMPTY_SHA256:
        raise _not_trusted("storage is not the empty image")
    by_name: Dict[str, ImageEntry] = {}
    for entry in images.values():
        other = by_name.setdefault(entry.name, entry)
        if (other.sha256, other.size) != (entry.sha256, entry.size):
            raise _not_trusted(f"file {entry.name} listed with two hashes")
    factory = images["factory"]
    if factory.app_version not in (None, version):
        raise _not_trusted("factory app version")
    if factory.app_project not in (None, PROJECTS[family]):
        raise _not_trusted("factory app project is for the other family")
    if factory.app_digest is None:
        raise _not_trusted("factory app digest missing")
    if family is Family.ZKT:
        if (images["ota_0"].sha256 != factory.sha256 or images["ota_0"].name != factory.name):
            raise _not_trusted("ota_0 is not the factory release")
        predecessor = images["ota_1"]
        if not predecessor.app_version or not _VERSION.fullmatch(predecessor.app_version):
            raise _not_trusted("ota_1 version")
        if version_tuple(predecessor.app_version) >= version_tuple(version):
            raise _not_trusted("ota_1 must be older than the release")
        if predecessor.app_digest is None:
            raise _not_trusted("ota_1 app digest missing")
        expected_id = f"zkt-{version}-{factory.app_digest[:8]}"
    else:
        expected_id = (f"hikvision-{version}-{str(manifest.get('git_sha'))[:7]}-"
                       f"{factory.app_digest[:8]}")
    if identifier != expected_id:
        raise _not_trusted(f"bundle id should be {expected_id}")
    return CheckedManifest(identifier, family, version, release_state, images)


def bundle_info(manifest: Mapping[str, object], checked: CheckedManifest) -> BundleInfo:
    total = sum(entry.size for entry in checked.files().values())
    return BundleInfo(
        bundle_id=checked.bundle_id, family=checked.family,
        label=str(manifest.get("label") or f"{checked.family.value.upper()} {checked.version}"),
        version=checked.version, recommended=bool(manifest.get("recommended")),
        release_state=checked.release_state, notes=str(manifest.get("notes") or ""),
        download_bytes=total)


# ------------------------------------------------------------- file checks

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Verifier:
    """Checks bundle files. ``image_key`` is the Secure Boot key the bootloader must
    carry (in production the same vault key-1 that signs manifests)."""

    manifest_key: rsa.RSAPublicKey
    image_key: Optional[rsa.RSAPublicKey] = None
    qualified: Mapping[str, str] = field(default_factory=lambda: dict(QUALIFIED_PREDECESSORS))

    def verify(self, directory: Path, manifest: Mapping[str, object], signature_b64: str,
               bundle_id: Optional[str] = None) -> CheckedManifest:
        verify_manifest_signature(manifest, signature_b64, self.manifest_key)
        checked = check_manifest(manifest, bundle_id)
        for name, entry in checked.files().items():
            path = directory / name
            if not path.is_file() or path.stat().st_size != entry.size:
                raise InstallerError(texts.BUNDLE_FILE_DAMAGED, cause=f"{name} missing or size")
            if sha256_file(path) != entry.sha256:
                raise InstallerError(texts.BUNDLE_FILE_DAMAGED, cause=f"{name} hash")
        self._check_contents(directory, checked)
        return checked

    def _check_contents(self, directory: Path, checked: CheckedManifest) -> None:
        images = checked.images
        read = {role: (directory / entry.name).read_bytes() for role, entry in images.items()
                if role != "storage"}
        table = partition_entries(read["partition-table"])
        if any(table.get(name) != expected for name, expected in PRODUCTION_PARTITIONS.items()):
            raise _not_trusted("partition table is not the production layout")
        if partitions_end(read["partition-table"]) > REDO_AREA_OFFSET:
            # The firmware then has no redo area, and every mode clears 0xFA0000-0xFFFFFF.
            raise _not_trusted("a partition reaches past 0xFA0000 (the redo area)")
        otadata = read["otadata"]
        if checked.family is Family.ZKT and otadata != otadata_selecting_ota_0():
            raise _not_trusted("ZKT otadata must select ota_0")
        if checked.family is Family.HIKVISION and otadata != blank_otadata():
            raise _not_trusted("Hikvision otadata must be blank")

        try:
            bootloader = parse_image(read["bootloader"])
            factory = parse_image(read["factory"])
        except ImageError as error:
            raise _not_trusted(f"image: {error}") from error
        if bootloader.chip_id != ESP32S3_CHIP_ID or not bootloader.digest_valid:
            raise _not_trusted("bootloader header or digest")
        boot_keys = [block.modulus for block in bootloader.signatures if block.valid]
        if (len(bootloader.signatures) != BOOTLOADER_SIGNATURES
                or len(boot_keys) != BOOTLOADER_SIGNATURES):
            raise _not_trusted("bootloader must carry three valid signatures")
        pinned = (self.image_key or self.manifest_key).public_numbers().n
        if pinned not in boot_keys:
            raise _not_trusted("bootloader does not trust the State Life key")
        self._check_app(factory, PROJECTS[checked.family], checked.version,
                        images["factory"].app_digest, boot_keys, "factory")
        if checked.family is Family.ZKT:
            predecessor_entry = images["ota_1"]
            try:
                predecessor = parse_image(read["ota_1"])
            except ImageError as error:
                raise _not_trusted(f"ota_1: {error}") from error
            self._check_app(predecessor, PROJECTS[Family.ZKT], predecessor_entry.app_version,
                            predecessor_entry.app_digest, None, "ota_1")
            if self.qualified.get(str(predecessor_entry.app_version)) != predecessor.digest:
                raise _not_trusted("ota_1 is not a qualified predecessor")

    @staticmethod
    def _check_app(facts: ImageFacts, project: str, version: Optional[str],
                   digest: Optional[str], boot_keys: Optional[Sequence[int]],
                   role: str) -> None:
        if facts.chip_id != ESP32S3_CHIP_ID or not facts.digest_valid:
            raise _not_trusted(f"{role} header or digest")
        if facts.project != project or facts.version != version:
            raise _not_trusted(f"{role} is {facts.project} {facts.version}")
        if digest is not None and facts.digest != digest:
            raise _not_trusted(f"{role} app digest")
        if not facts.signatures or not facts.signatures[0].valid:
            raise _not_trusted(f"{role} is not signed")
        if boot_keys is not None and facts.signatures[0].modulus not in boot_keys:
            raise _not_trusted(f"{role} is signed by a key the bootloader does not trust")


# --------------------------------------------------------- download + cache

class BundleSource(Protocol):
    def bundle_manifest(self, bundle_id: str) -> Tuple[Dict[str, object], str]: ...
    def download_bundle_file(self, bundle_id: str, name: str, destination: Path,
                             on_chunk: Callable[[int], None], cancel: CancelToken) -> None: ...


class BundleStore:
    """Bundles cached under ``root/<bundle_id>/`` with their manifest and signature."""

    def __init__(self, root: Path, verifier: Verifier,
                 storage_gz: Optional[Path] = None) -> None:
        self.root = root
        self.verifier = verifier
        self.storage_gz = storage_gz

    def directory(self, bundle_id: str) -> Path:
        if not _BUNDLE_ID.fullmatch(bundle_id):
            raise _not_trusted("bundle id")
        return self.root / bundle_id

    def fetch(self, source: BundleSource, bundle_id: str, on_bytes: Optional[BytesProgressFn],
              cancel: CancelToken, expected_manifest_sha256: Optional[str] = None
              ) -> LocalBundle:
        manifest, signature = source.bundle_manifest(bundle_id)
        if expected_manifest_sha256 and manifest_sha256(manifest) != expected_manifest_sha256:
            raise _not_trusted("manifest differs from the catalogue")
        verify_manifest_signature(manifest, signature, self.verifier.manifest_key)
        checked = check_manifest(manifest, bundle_id)
        directory = self.directory(bundle_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            needed = [entry for entry in checked.files().values()
                      if not self._cached(directory / entry.name, entry)]
            for entry in list(needed):
                if entry.name == STORAGE_IMAGE_NAME or entry.sha256 == STORAGE_EMPTY_SHA256:
                    if self._use_embedded_storage(directory / entry.name, entry):
                        needed.remove(entry)
            total = sum(entry.size for entry in needed)
            done = 0
            if on_bytes:
                on_bytes(0, total)
            for entry in needed:
                if cancel.cancelled:
                    raise InstallerError(texts.CANCELLED, cause="download cancelled")
                done = self._download(source, bundle_id, directory, entry, done, total,
                                      on_bytes, cancel)
            (directory / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
            (directory / "manifest.sig").write_text(signature.strip() + "\n", encoding="utf-8")
        except OSError as error:
            raise _disk_error(error) from error
        return self.verify_local(bundle_id)

    def verify_local(self, bundle_id: str) -> LocalBundle:
        """Re-check a cached bundle completely; use before every write to a board."""
        directory = self.directory(bundle_id)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            signature = (directory / "manifest.sig").read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as error:
            raise InstallerError(texts.BUNDLE_FILE_DAMAGED, cause="cached manifest") from error
        checked = self.verifier.verify(directory, manifest, signature, bundle_id)
        return LocalBundle(bundle_info(manifest, checked), str(directory), manifest)

    def storage_image(self) -> Path:
        """A verified empty storage image, from the copy that ships with the app."""
        target = self.root / "shared" / STORAGE_IMAGE_NAME
        entry = ImageEntry("storage", STORAGE_IMAGE_NAME, ROLE_OFFSETS["storage"], STORAGE_SIZE,
                           STORAGE_EMPTY_SHA256)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._cached(target, entry) or self._use_embedded_storage(target, entry):
                return target
        except OSError as error:
            raise _disk_error(error) from error
        raise InstallerError(texts.TOOL_MISSING, cause="embedded storage image unusable")

    def redo_area_image(self) -> Path:
        """The blank redo area (all 0xFF), made here from a constant, never downloaded.

        Checked by size and pinned hash on every call, like the storage image, and
        made again whenever the copy on disk differs.
        """
        target = self.root / "shared" / REDO_AREA_IMAGE_NAME
        entry = ImageEntry("redo-area", REDO_AREA_IMAGE_NAME, REDO_AREA_OFFSET, REDO_AREA_SIZE,
                           REDO_AREA_BLANK_SHA256)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._cached(target, entry):
                return target
            data = blank_redo_area()
            if (len(data) != REDO_AREA_SIZE
                    or hashlib.sha256(data).hexdigest() != REDO_AREA_BLANK_SHA256):
                raise InstallerError(texts.TOOL_MISSING,
                                     cause="blank redo area does not match its pinned hash")
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(data)
            os.replace(partial, target)
            if self._cached(target, entry):
                return target
        except OSError as error:
            raise _disk_error(error) from error
        raise InstallerError(texts.TOOL_MISSING, cause="blank redo area image unusable")

    @staticmethod
    def _cached(path: Path, entry: ImageEntry) -> bool:
        try:
            return (path.is_file() and path.stat().st_size == entry.size
                    and sha256_file(path) == entry.sha256)
        except OSError:
            return False

    def _use_embedded_storage(self, target: Path, entry: ImageEntry) -> bool:
        if self.storage_gz is None or not self.storage_gz.is_file():
            return False
        if entry.sha256 != STORAGE_EMPTY_SHA256 or entry.size != STORAGE_SIZE:
            return False
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        size = 0
        with gzip.open(self.storage_gz, "rb") as source, partial.open("wb") as out:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        if size != STORAGE_SIZE or digest.hexdigest() != STORAGE_EMPTY_SHA256:
            partial.unlink()
            log.error("embedded storage image does not match its pinned hash")
            return False
        os.replace(partial, target)
        return True

    @staticmethod
    def _download(source: BundleSource, bundle_id: str, directory: Path, entry: ImageEntry,
                  done: int, total: int, on_bytes: Optional[BytesProgressFn],
                  cancel: CancelToken) -> int:
        partial = directory / (entry.name + ".part")
        if partial.exists():
            partial.unlink()
        received = [0]

        def chunk(count: int) -> None:
            received[0] += count
            if on_bytes:
                on_bytes(min(total, done + received[0]), total)

        source.download_bundle_file(bundle_id, entry.name, partial, chunk, cancel)
        if partial.stat().st_size != entry.size or sha256_file(partial) != entry.sha256:
            partial.unlink()
            raise InstallerError(texts.BUNDLE_FILE_DAMAGED, cause=f"{entry.name} hash")
        os.replace(partial, directory / entry.name)
        return done + entry.size


def _disk_error(error: OSError) -> InstallerError:
    if error.errno == errno.ENOSPC:
        return InstallerError(texts.DISK_FULL, cause=str(error))
    return InstallerError(texts.BUNDLE_DOWNLOAD_FAILED, cause=f"disk: {error}")


def flash_ranges(bundle: LocalBundle) -> List[FlashRange]:
    """The bundle's images as ranges to write, by role."""
    checked = check_manifest(bundle.manifest, bundle.info.bundle_id)
    directory = Path(bundle.directory)
    return [FlashRange(role, entry.offset, str(directory / entry.name), entry.size)
            for role, entry in checked.images.items()]


def clean_cache(root: Path, keep: Iterable[str]) -> None:
    """Remove cached bundles not in ``keep`` (the shared storage and redo area images stay)."""
    keep = set(keep)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name != "shared" and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)

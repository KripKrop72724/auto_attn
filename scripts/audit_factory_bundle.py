"""Read one fixed factory bundle; export numeric/hash metadata only, never bytes.

The CLI runs inside the reviewed read-only /factory-bundle mount. This is archive
identity evidence, not installed-device, rollback, storage, or HIL qualification.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import sys
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

BUNDLE_ID = "zone-lite-2.5.2-27c3bb80eb20"
SOURCE_SHA = "27c3bb80eb203ea7ee19a42cdc3f871664a97d1b"
KEY_PEM_SHA256 = "9fc614e085ba24f260ad92be0812103beef0b43c74881d72e800caf68c602a55"  # gitleaks:allow - public key fingerprint, not key material
KEY_SPKI_SHA256 = "0523b6a90880e60c1526533128931c770024659d89f798a756c92fa7edae7bf0"  # gitleaks:allow - public key fingerprint, not key material
TARGETS = {
    "a1ff7b24-4dcb-4dde-ad41-1a8401c7b006": "e068ee75073e3198f7894f04a249169eef96feb996d9b7db7e5a8c7a04829e91",
    "2f5cedd8-e314-47b0-8074-bc4bb8a603cc": "191b63c5f18485a9aa7f705f77679f932e79f326e1bf87e3c4bb237d249fb786",
    "a886e2d9-204d-425c-bc8f-ded85fc89874": "00dcc3514b997570fcdf7495f7b8a85302bcff6c2670120d13245f93c0424e8b",
}
FILES = {
    "bootloader-signed.bin": (0, 0x10000),
    "partition-table.bin": (0x10000, 0x1000),
    "ota_data_initial.bin": (0x17000, 0x2000),
    "zone-lite-signed.bin": (0x20000, 0x280000),
}
PARTITIONS = (
    (1, 2, 0x11000, 0x6000, "nvs", 0),
    (1, 0, 0x17000, 0x2000, "otadata", 0),
    (1, 1, 0x19000, 0x1000, "phy_init", 0),
    (0, 0, 0x20000, 0x280000, "factory", 0),
    (0, 0x10, 0x2A0000, 0x280000, "ota_0", 0),
    (0, 0x11, 0x520000, 0x280000, "ota_1", 0),
    (1, 0x82, 0x7A0000, 0x800000, "storage", 0),
)


class Refused(Exception):
    """Only constant, allowlisted error codes may cross the CLI boundary."""


def require(value: object, code: str) -> None:
    if not value:
        raise Refused(code)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def read_regular(root: Path, name: str, limit: int) -> bytes:
    require(name in {*FILES, "manifest.json", "manifest.sig", "key-1-public.pem"}, "FILE_NOT_ALLOWED")
    require(root.is_dir() and not root.is_symlink(), "BUNDLE_DIRECTORY_INVALID")
    fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit, "FILE_SIZE_OR_TYPE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        require(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and len(raw) == before.st_size,
            "FILE_CHANGED_DURING_READ",
        )
        return raw
    finally:
        os.close(fd)


@dataclass(frozen=True)
class TrustAnchor:
    pem_sha256: str
    spki_sha256: str


PRODUCTION_ANCHOR = TrustAnchor(KEY_PEM_SHA256, KEY_SPKI_SHA256)


def verify_secure_boot(raw: bytes, public: rsa.RSAPublicKey) -> int:
    """RSA SBv2 layout from pinned IDF5.5.3 espsecure, plus embedded-key binding.

    Verify the digest, CRC, RSA key primitives and PSS signature. The installed
    eFuse trust/revocation state is deliberately not inferred from these bytes.
    """
    require(len(raw) >= 8192 and len(raw) % 4096 == 0, "ESP_SIGNATURE_SIZE")
    digest = hashlib.sha256(raw[:-4096]).digest()
    numbers = public.public_numbers()
    for index in range(3):
        begin = len(raw) - 4096 + index * 1216
        block = raw[begin : begin + 1216]
        magic, version, embedded_digest, n, exponent, rinv, mprime, signature, crc = struct.unpack(
            "<BBxx32s384sI384sI384sI16x", block
        )
        if magic != 0xE7 or version != 2 or zlib.crc32(block[:1196]) & 0xFFFFFFFF != crc:
            continue
        if (
            embedded_digest != digest
            or int.from_bytes(n, "little") != numbers.n
            or exponent != numbers.e
            or int.from_bytes(rinv, "little") != pow(2, 6144, numbers.n)
            or mprime != (-pow(numbers.n, -1, 1 << 32)) & 0xFFFFFFFF
        ):
            continue
        try:
            public.verify(
                signature[::-1], digest,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                utils.Prehashed(hashes.SHA256()),
            )
            return index
        except Exception:
            continue
    raise Refused("ESP_TRUST_ANCHOR_SIGNATURE_INVALID")


def application_identity(raw: bytes) -> dict:
    require(len(raw) >= 256 and raw[0] == 0xE9 and 0 < raw[1] <= 16 and raw[23] == 1,
            "APPLICATION_HEADER")
    require(struct.unpack_from("<H", raw, 12)[0] == 9, "APPLICATION_CHIP")
    require(struct.unpack_from("<I", raw, 32)[0] == 0xABCD5432, "APPLICATION_DESCRIPTOR")
    require(raw[48:80].split(b"\0", 1)[0] == b"2.5.2"
            and raw[80:112].split(b"\0", 1)[0] == b"zone_lite", "APPLICATION_IDENTITY")
    position, checksum = 24, 0xEF
    for _ in range(raw[1]):
        require(position + 8 <= len(raw) - 4096, "APPLICATION_SEGMENT_BOUND")
        length = struct.unpack_from("<I", raw, position + 4)[0]
        start, position = position + 8, position + 8 + length
        require(position <= len(raw) - 4096, "APPLICATION_SEGMENT_BOUND")
        for byte in raw[start:position]:
            checksum ^= byte
    end = (position + 16) // 16 * 16
    require(end + 32 <= len(raw) - 4096 and raw[end - 1] == checksum, "APPLICATION_CHECKSUM")
    digest = hashlib.sha256(raw[:end]).digest()
    require(raw[end : end + 32] == digest, "APPLICATION_VALIDATION_DIGEST")
    return {"version": "2.5.2", "project_name": "zone_lite", "chip_id": 9,
            "application_sha256": digest.hex(), "elf_sha256": raw[176:208].hex()}


def verify_partitions(raw: bytes) -> list[dict]:
    require(len(raw) in (0xC00, 0x1000), "PARTITION_TABLE_SIZE")
    for index, expected in enumerate(PARTITIONS):
        magic, kind, subtype, offset, size, label, flags = struct.unpack_from("<HBBII16sI", raw, index * 32)
        wanted = expected[4].encode().ljust(16, b"\0")
        require(magic == 0x50AA and (kind, subtype, offset, size, flags)
                == (*expected[:4], expected[5]) and label == wanted, "PARTITION_LAYOUT_MISMATCH")
    end = len(PARTITIONS) * 32
    require(raw[end : end + 16] == b"\xeb\xeb" + b"\xff" * 14
            and raw[end + 16 : end + 32] == hashlib.md5(raw[:end], usedforsecurity=False).digest()
            and raw[end + 32 :] == b"\xff" * (len(raw) - end - 32), "PARTITION_TABLE_MD5_OR_TAIL")
    return [{"name": row[4], "type": row[0], "subtype": row[1], "offset": row[2],
             "size": row[3], "flags": row[5]} for row in PARTITIONS]


def audit_bundle(root: Path, anchor: TrustAnchor = PRODUCTION_ANCHOR) -> dict:
    pem = read_regular(root, "key-1-public.pem", 4096)
    require(sha(pem) == anchor.pem_sha256, "PUBLIC_KEY_PEM_PIN")
    public = serialization.load_pem_public_key(pem)
    require(isinstance(public, rsa.RSAPublicKey) and public.key_size == 3072, "PUBLIC_KEY_TYPE")
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    require(sha(der) == anchor.spki_sha256, "PUBLIC_KEY_SPKI_PIN")
    manifest_raw = read_regular(root, "manifest.json", 65536)
    manifest = json.loads(manifest_raw, object_pairs_hook=_object)
    require(type(manifest) is dict and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1 and manifest.get("bundle_id") == BUNDLE_ID
            and manifest.get("version") == "2.5.2" and manifest.get("git_sha") == SOURCE_SHA
            and manifest.get("hardware_profile") == "esp32s3-16mb-zone-lite-v1"
            and manifest.get("partition_layout") == "zone-lite-factory-v1"
            and manifest.get("setup_password_supplied") is True
            and manifest.get("firmware_family", "zkt") == "zkt"
            and manifest.get("project_name", "zone_lite") == "zone_lite", "MANIFEST_IDENTITY")
    keys = manifest.get("signing_key_ids")
    require(type(keys) is list and len(keys) == 3 and all(type(k) is str and len(k) == 64
            and set(k) <= set("0123456789abcdef") for k in keys)
            and len(set(keys)) == 3 and keys[0] == anchor.pem_sha256, "MANIFEST_KEY_IDS")
    signature = base64.b64decode(read_regular(root, "manifest.sig", 8192).strip(), validate=True)
    public.verify(signature, canonical(manifest),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    images = manifest.get("images")
    require(type(images) is list and len(images) == len(FILES) and all(type(i) is dict for i in images),
            "MANIFEST_IMAGE_INVENTORY")
    require({i.get("name") for i in images} == set(FILES), "MANIFEST_IMAGE_INVENTORY")
    payloads, inventory = {}, []
    for entry in images:
        name = entry["name"]
        offset, maximum = FILES[name]
        require(type(entry.get("offset")) is int and entry["offset"] == offset
                and type(entry.get("size")) is int and 0 < entry["size"] <= maximum, "MANIFEST_IMAGE_BOUNDS")
        raw = read_regular(root, name, maximum)
        require(len(raw) == entry["size"] and sha(raw) == entry.get("sha256"), "IMAGE_FILE_HASH")
        payloads[name] = raw
        inventory.append({"name": name, "offset": offset, "size": len(raw), "sha256": sha(raw)})
    app = application_identity(payloads["zone-lite-signed.bin"])
    app_block = verify_secure_boot(payloads["zone-lite-signed.bin"], public)
    boot_block = verify_secure_boot(payloads["bootloader-signed.bin"], public)
    partitions = verify_partitions(payloads["partition-table.bin"])
    require(payloads["ota_data_initial.bin"] == b"\xff" * 0x2000, "INITIAL_OTADATA_NOT_EMPTY")
    require(read_regular(root, "manifest.json", 65536) == manifest_raw, "MANIFEST_CHANGED")
    return {"schema_version": 1, "status": "ARCHIVE_VERIFIED", "bundle_id": BUNDLE_ID,
            "source_sha": SOURCE_SHA, "manifest_sha256": sha(canonical(manifest)),
            "public_key_pem_sha256": anchor.pem_sha256, "public_key_spki_sha256": anchor.spki_sha256,
            "manifest_signature": "VERIFIED", "application_signature_block": app_block,
            "bootloader_signature_block": boot_block, "application": app,
            "images": sorted(inventory, key=lambda item: item["offset"]), "partitions": partitions,
            "initial_otadata_empty": True,
            "device_hash_comparisons": [{"connector_id": connector, "expected_application_sha256": digest,
                "application_digest_matches": app["application_sha256"] == digest}
                for connector, digest in TARGETS.items()],
            "installed_security_or_rollback_verified": False, "hil_qualification": "NOT_ASSERTED",
            "raw_firmware_exported": False}


def main() -> int:
    def timed_out(_signal, _frame):
        raise Refused("AUDIT_TIME_LIMIT")

    try:
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(60)
        require(len(sys.argv) == 1, "CLI_ARGUMENTS_NOT_ALLOWED")
        result = audit_bundle(Path("/factory-bundle"))
    except Refused as error:
        result = {"schema_version": 1, "status": "REFUSED", "error_code": error.args[0],
                  "raw_firmware_exported": False, "hil_qualification": "NOT_ASSERTED"}
    except Exception:
        result = {"schema_version": 1, "status": "REFUSED", "error_code": "INVALID_OR_UNAVAILABLE_INPUT",
                  "raw_firmware_exported": False, "hil_qualification": "NOT_ASSERTED"}
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "ARCHIVE_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

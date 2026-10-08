"""Read-only, pre-sign reader proof using deployed ADD store/trust/catalog.

No catalog synchronization, qualification grant, key-vault access or writes.
The installed policy must equal the reviewed writer's compiled matrix hash.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils


def require(condition, code):
    if not condition:
        raise ValueError(code)


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_METADATA")
        result[key] = value
    return result


def read_regular(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit, "PACKAGE_FILE_INVALID")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, key) == getattr(after, key) for key in fields)
                and len(raw) == before.st_size, "PACKAGE_CHANGED")
        return raw
    finally:
        os.close(fd)


def verify_secure_boot(raw, public):
    """Pinned ESP-IDF 5.5.3 RSA SBv2: full signed region and embedded key.

    This checks the package signature, not any device's eFuse trust state.
    All post-application-hash padding is covered by the signed-region digest.
    """
    require(len(raw) >= 8192 and len(raw) % 4096 == 0, "SECURE_BOOT_SIZE")
    digest = hashlib.sha256(raw[:-4096]).digest()
    numbers = public.public_numbers()
    for index in range(3):
        begin = len(raw) - 4096 + index * 1216
        block = raw[begin:begin + 1216]
        magic, version, embedded, n, exponent, rinv, mprime, signature, crc = struct.unpack(
            "<BBxx32s384sI384sI384sI16x", block)
        if (magic != 0xE7 or version != 2 or zlib.crc32(block[:1196]) & 0xFFFFFFFF != crc
                or embedded != digest or int.from_bytes(n, "little") != numbers.n
                or exponent != numbers.e or int.from_bytes(rinv, "little") != pow(2, 6144, numbers.n)
                or mprime != (-pow(numbers.n, -1, 1 << 32)) & 0xFFFFFFFF):
            continue
        try:
            public.verify(signature[::-1], digest,
                          padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                          utils.Prehashed(hashes.SHA256()))
            return
        except Exception:
            continue
    raise ValueError("SECURE_BOOT_SIGNATURE")


def application_digest(raw, version):
    require(len(raw) >= 256 and raw[0] == 0xE9 and 0 < raw[1] <= 16 and raw[23] == 1
            and struct.unpack_from("<H", raw, 12)[0] == 9
            and struct.unpack_from("<I", raw, 32)[0] == 0xABCD5432
            and raw[48:80].split(b"\0", 1)[0] == version.encode("ascii")
            and raw[80:112].split(b"\0", 1)[0] == b"zone_lite", "APPLICATION_IDENTITY")
    position, checksum = 24, 0xEF
    for _ in range(raw[1]):
        require(position + 8 <= len(raw) - 4096, "SEGMENT_BOUND")
        length = struct.unpack_from("<I", raw, position + 4)[0]
        start, position = position + 8, position + 8 + length
        require(position <= len(raw) - 4096, "SEGMENT_BOUND")
        for byte in raw[start:position]:
            checksum ^= byte
    end = (position + 16) // 16 * 16
    require(end + 32 <= len(raw) - 4096 and raw[end - 1] == checksum, "APPLICATION_CHECKSUM")
    digest = hashlib.sha256(raw[:end]).digest()
    require(raw[end:end + 32] == digest, "APPLICATION_HASH")
    return digest.hex()


def verify_package(root, entry, record, public_pem):
    from zk_add.ota import OTA_LAYOUT, _parse_release_hil_targets
    from zk_add.storage_contract import validate_storage_contract
    from zk_add.zkt_bridge_contract import validate_bridge_image
    from zk_add.zkt_reader_matrix import canonical

    version = entry["version"]
    image_name = "zone-lite-" + version + ".bin"
    require(record is not None and record["state"] == "HIL_ONLY" and record["revoked_at"] is None,
            "READER_NOT_PUBLISHED_OR_REVOKED")
    expected = {"release_id": entry["release_id"], "version": version, "git_sha": entry["source_sha"],
                "image_sha256": entry["artifact_sha256"], "signing_key_id": entry["signing_key_id"],
                "partition_layout": OTA_LAYOUT, "storage_name": version + "/" + image_name}
    require(all(record[key] == value for key, value in expected.items()), "CATALOG_IDENTITY")
    # Open the configured mount and exact version directory without following
    # package symlinks. Names come only from the validated installed matrix.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.open(version, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            raw_manifest = read_regular(directory, "manifest.json", 65536)
            raw_signature = read_regular(directory, "manifest.sig", 8192)
            raw_marker = read_regular(directory, ".hil-only.json", 65536)
            image = read_regular(directory, image_name, 0x280000 - 128 * 1024)
        finally:
            os.close(directory)
    finally:
        os.close(root_fd)
    manifest = json.loads(raw_manifest, object_pairs_hook=object_pairs)
    require(type(manifest) is dict and raw_manifest == canonical(manifest), "MANIFEST_CANONICAL")
    stored = record["manifest"]
    require(type(stored) is dict and canonical({k: v for k, v in stored.items() if not k.startswith("_")})
            == raw_manifest, "CATALOG_MANIFEST")
    signature = raw_signature.decode("ascii").strip()
    require(signature == record["manifest_signature"], "CATALOG_SIGNATURE")
    require(all(manifest.get(key) == value for key, value in expected.items() if key != "storage_name")
            and manifest.get("image_name") == image_name
            and type(manifest.get("image_size")) is int
            and manifest["image_size"] == record["image_size"] == len(image)
            and manifest.get("application_sha256") == entry["application_sha256"], "MANIFEST_IDENTITY")
    require(hashlib.sha256(public_pem).hexdigest() == entry["signing_key_id"], "TRUST_KEY_IDENTITY")
    public = serialization.load_pem_public_key(public_pem)
    require(isinstance(public, rsa.RSAPublicKey) and public.key_size == 3072, "TRUST_KEY_TYPE")
    public.verify(base64.b64decode(signature, validate=True), raw_manifest,
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    validate_storage_contract(manifest, version)
    require(hashlib.sha256(image).hexdigest() == entry["artifact_sha256"], "ARTIFACT_HASH")
    verify_secure_boot(image, public)
    require(application_digest(image, version) == entry["application_sha256"], "APPLICATION_IDENTITY")
    validate_bridge_image(image, version)
    marker = json.loads(raw_marker, object_pairs_hook=object_pairs)
    require(type(marker) is dict and type(marker.get("schema_version")) is int and marker["schema_version"] == 2
            and marker.get("version") == version and not marker.get("target_mac")
            and marker.get("git_sha") == entry["source_sha"]
            and marker.get("image_sha256") == entry["artifact_sha256"]
            and marker.get("application_sha256") == entry["application_sha256"], "HIL_PUBLICATION")
    targets = [row.model_dump() for row in _parse_release_hil_targets(
        (entry["release_id"], version, entry["source_sha"], entry["artifact_sha256"],
         entry["application_sha256"]), marker.get("targets"))]
    require(stored.get("_publication_mode") == "HIL_ONLY" and stored.get("_hil_targets") == targets,
            "CATALOG_PUBLICATION")


def check(expected_matrix_sha256):
    from sqlalchemy import text
    from zk_add.db import engine
    from zk_add.settings import settings
    from zk_add.zkt_reader_matrix import load_matrix, matrix_hash

    require(re.fullmatch(r"[0-9a-f]{64}", expected_matrix_sha256) is not None, "MATRIX_IDENTITY")
    matrix = load_matrix(require_pinned=True)
    require(matrix_hash(matrix) == expected_matrix_sha256, "DEPLOYED_MATRIX_MISMATCH")
    public_pem = base64.b64decode(settings.firmware_signing_public_key_pem_b64, validate=True)
    require(0 < len(public_pem) <= 8192, "TRUST_KEY_SIZE")
    root = Path(settings.firmware_store_path)
    # Never use sync_release_store/session_scope here: signing only observes
    # already published catalog rows and explicitly refuses an absent row.
    with engine.connect() as connection, connection.begin():
        require(connection.dialect.name in {"postgresql", "sqlite"}, "DATABASE_DIALECT")
        connection.execute(text("SET TRANSACTION READ ONLY" if connection.dialect.name == "postgresql"
                                else "PRAGMA query_only=ON"))
        for entry in matrix["readers"]:
            row = connection.execute(text("SELECT release_id, version, git_sha, image_sha256, image_size, "
                "signing_key_id, partition_layout, storage_name, manifest, manifest_signature, state, revoked_at "
                "FROM add_firmware_releases WHERE release_id = :release_id LIMIT 2"),
                {"release_id": entry["release_id"]}).mappings().all()
            require(len(row) == 1, "READER_NOT_PUBLISHED")
            record = dict(row[0])
            if isinstance(record["manifest"], str):  # SQLite JSON driver.
                record["manifest"] = json.loads(record["manifest"], object_pairs_hook=object_pairs)
            verify_package(root, entry, record, public_pem)
    return "ADD_READER_PACKAGES_ACCEPTED:" + expected_matrix_sha256


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix_sha256")
    args = parser.parse_args(argv)
    try:
        report = check(args.matrix_sha256)
    except Exception:
        print("ADD_READER_PACKAGES_REJECTED")
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

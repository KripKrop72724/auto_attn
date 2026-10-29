#!/usr/bin/env python3
"""Flash an envelope-prepared, first-use Zone Lite ESP32-S3.

The protected runner creates the signed bootstrap and device-bound encrypted
NVS/HMAC payloads. This tool operates only on already decrypted temporary
files, requires exact-MAC confirmation for both irreversible operations, and
verifies every flashed range before allowing the first boot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Callable
import zlib

from provision_zone_lite import ensure_nvs_hmac_key, normalize_mac, read_mac


FLASH_LAYOUT = (
    ("bootloader-signed.bin", 0x0),
    ("partition-table.bin", 0x10000),
    ("provision.bin", 0x11000),
    ("ota_data_initial.bin", 0x17000),
    ("zone-lite-signed.bin", 0x20000),
)

# A production package boots the exact published release from ota_0, as if it
# had arrived by OTA: 2.6.10 and later refuse to upgrade an image running from
# factory, and ADD verifies a predecessor only in an OTA slot. The same bytes
# in factory remain the bootloader's last resort.
#
# Two more ranges make the first boot accepted (both found on new boards on
# 29 Oct 2026, where the release alone booted to LOCAL_FAILURE):
# - ota_1 holds an older published release. 2.7.x direct-upgrade builds start
#   storage only when the other OTA slot holds a qualified predecessor (an
#   exact application digest) as their rollback image.
# - storage holds an empty, formatted SPIFFS. The firmware never formats a
#   partition that will not mount, so erased flash would stay unusable.
OTA_0_OFFSET = 0x2A0000
OTA_1_OFFSET = 0x520000
APP_SLOT_SIZE = 0x280000
STORAGE_OFFSET = 0x7A0000
STORAGE_SIZE = 0x800000
OTADATA_OFFSET = 0x17000
OTADATA_SIZE = 0x2000
PRODUCTION_PARTITIONS = {
    "otadata": (1, 0x00, OTADATA_OFFSET, OTADATA_SIZE),
    "factory": (0, 0x00, 0x20000, APP_SLOT_SIZE),
    "ota_0": (0, 0x10, OTA_0_OFFSET, APP_SLOT_SIZE),
    "ota_1": (0, 0x11, OTA_1_OFFSET, APP_SLOT_SIZE),
    "storage": (1, 0x82, STORAGE_OFFSET, STORAGE_SIZE),
}
ROLLBACK_PREDECESSOR_IMAGE = "rollback-predecessor.bin"
STORAGE_IMAGE = "storage-empty.bin"
PRODUCTION_LAYOUT = (
    ("bootloader-signed.bin", 0x0),
    ("partition-table.bin", 0x10000),
    ("provision.bin", 0x11000),
    ("otadata-ota0.bin", OTADATA_OFFSET),
    ("zone-lite-signed.bin", 0x20000),
    ("zone-lite-signed.bin", OTA_0_OFFSET),
    (ROLLBACK_PREDECESSOR_IMAGE, OTA_1_OFFSET),
    (STORAGE_IMAGE, STORAGE_OFFSET),
)
ESP_OTA_IMG_VALID = 2
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

FlashProgress = Callable[[str, int, int], None]


def run(
    command: list[str], line_callback: Callable[[str], None] | None = None
) -> str:
    if line_callback is None:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return completed.stdout
    process = subprocess.Popen(
        command,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)
        line_callback(line)
    return_code = process.wait()
    rendered = "".join(output)
    if return_code:
        raise subprocess.CalledProcessError(return_code, command, output=rendered)
    return rendered


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def otadata_selecting_ota_0() -> bytes:
    """Both otadata sectors, with one valid entry that selects ota_0.

    The bootloader boots the valid entry with the highest ota_seq, slot
    (ota_seq - 1) % 2; its CRC is ESP-IDF otatool's.
    """
    seq = 1
    crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    entry = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", ESP_OTA_IMG_VALID, crc)
    return entry + b"\xff" * (OTADATA_SIZE - len(entry))


def partition_entries(table: bytes) -> dict[str, tuple[int, int, int, int]]:
    entries = {}
    for start in range(0, len(table) - 31, 32):
        row = table[start:start + 32]
        if row[:2] != b"\xaa\x50":
            break
        kind, subtype, offset, size = struct.unpack_from("<BBII", row, 2)
        label = row[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        entries[label] = (kind, subtype, offset, size)
    return entries


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def production_release(bootstrap: dict) -> dict | None:
    """The signed production claim, or None for a package of this build.

    A production claim must also name the rollback predecessor for ota_1 and
    the empty storage image: without them 2.7.x boots to LOCAL_FAILURE.
    """
    release = bootstrap.get("production_release")
    if release is None:
        return None
    if (not isinstance(release, dict) or release.get("boot_slot") != "ota_0"
            or not VERSION.fullmatch(str(release.get("version", "")))
            or release.get("image_sha256") != bootstrap.get("application_sha256")):
        raise RuntimeError("Signed bootstrap production release is invalid")
    predecessor = bootstrap.get("rollback_predecessor")
    if not isinstance(predecessor, dict):
        raise RuntimeError(
            "Signed bootstrap production release has no rollback predecessor for ota_1; "
            "2.7.x refuses storage without one"
        )
    if (predecessor.get("slot") != "ota_1"
            or not VERSION.fullmatch(str(predecessor.get("version", "")))
            or not SHA256_HEX.fullmatch(str(predecessor.get("image_sha256", "")))
            or predecessor.get("image_sha256") == release.get("image_sha256")):
        raise RuntimeError("Signed bootstrap rollback predecessor is invalid")
    if version_tuple(predecessor["version"]) >= version_tuple(release["version"]):
        raise RuntimeError(
            "Signed bootstrap rollback predecessor must be older than the production release"
        )
    if (not SHA256_HEX.fullmatch(str(bootstrap.get("storage_image_sha256", "")))
            or bootstrap.get("storage_image_size") != STORAGE_SIZE):
        raise RuntimeError(
            "Signed bootstrap production release has no empty storage image for the "
            "8 MiB storage partition"
        )
    return release


def validate_inputs(
    *,
    signed_package: Path,
    provisioning_manifest: Path,
    provision_nvs: Path,
    hmac_key: Path,
    mac: str,
    expected_git_sha: str,
) -> None:
    bootstrap = json.loads(
        (signed_package / "bootstrap-manifest.json").read_text(encoding="utf-8")
    )
    if normalize_mac(str(bootstrap.get("target_mac", ""))) != mac:
        raise RuntimeError("Signed bootstrap target MAC does not match attached ESP")
    if bootstrap.get("git_sha") != expected_git_sha:
        raise RuntimeError("Signed bootstrap Git SHA does not match the approved source")
    if sha256(signed_package / "bootloader-signed.bin") != bootstrap.get(
        "bootloader_sha256"
    ):
        raise RuntimeError("Signed bootloader hash does not match its manifest")
    if sha256(signed_package / "zone-lite-signed.bin") != bootstrap.get(
        "application_sha256"
    ):
        raise RuntimeError("Signed application hash does not match its manifest")
    if production_release(bootstrap) is not None:
        partitions = partition_entries((signed_package / "partition-table.bin").read_bytes())
        if any(partitions.get(name) != expected for name, expected in PRODUCTION_PARTITIONS.items()):
            raise RuntimeError("Partition table does not match the production OTA layout")
        for name, expected_sha256 in (
            (ROLLBACK_PREDECESSOR_IMAGE, bootstrap["rollback_predecessor"]["image_sha256"]),
            (STORAGE_IMAGE, bootstrap["storage_image_sha256"]),
        ):
            image = signed_package / name
            if not image.is_file():
                raise RuntimeError(f"Signed bootstrap production package is missing {name}")
            if sha256(image) != expected_sha256:
                raise RuntimeError(f"Signed bootstrap {name} hash does not match its manifest")
        if any((signed_package / name).stat().st_size > APP_SLOT_SIZE
               for name in ("zone-lite-signed.bin", ROLLBACK_PREDECESSOR_IMAGE)):
            raise RuntimeError("A production application image does not fit its OTA slot")
        if (signed_package / STORAGE_IMAGE).stat().st_size != STORAGE_SIZE:
            raise RuntimeError("Empty storage image does not cover the whole storage partition")

    provisioning = json.loads(provisioning_manifest.read_text(encoding="utf-8"))
    aad = provisioning.get("aad", {})
    family = aad.get("firmware_family", "zkt")
    if family not in {"zkt", "hikvision"} or bootstrap.get("firmware_family", "zkt") != family:
        raise RuntimeError("Provisioning and signed bootstrap firmware families do not match")
    if normalize_mac(str(aad.get("target_mac", ""))) != mac:
        raise RuntimeError("Provisioning NVS target MAC does not match attached ESP")
    if sha256(provision_nvs) != aad.get("nvs_sha256"):
        raise RuntimeError("Provisioning NVS hash does not match its manifest")
    if provision_nvs.stat().st_size != int(aad.get("nvs_size", -1)):
        raise RuntimeError("Provisioning NVS size does not match its manifest")
    hmac_aad = provisioning.get("hmac_key", {}).get("aad", {})
    if normalize_mac(str(hmac_aad.get("target_mac", ""))) != mac:
        raise RuntimeError("Provisioning HMAC target MAC does not match attached ESP")
    if hmac_aad.get("purpose") != "zone-lite-nvs-hmac-efuse-key":
        raise RuntimeError("Provisioning HMAC purpose is invalid")
    if hmac_key.stat().st_size != 32 or hmac_aad.get("key_size") != 32:
        raise RuntimeError("Provisioning HMAC key size is invalid")


def flash_and_verify(
    *,
    esptool: str,
    port: str,
    signed_package: Path,
    provision_nvs: Path,
    progress: FlashProgress | None = None,
) -> None:
    sources = {
        "bootloader-signed.bin": signed_package / "bootloader-signed.bin",
        "partition-table.bin": signed_package / "partition-table.bin",
        "provision.bin": provision_nvs,
        "ota_data_initial.bin": signed_package / "ota_data_initial.bin",
        "zone-lite-signed.bin": signed_package / "zone-lite-signed.bin",
    }
    bootstrap = json.loads((signed_package / "bootstrap-manifest.json").read_text(encoding="utf-8"))
    if production_release(bootstrap) is None:
        flash_sources_and_verify(
            esptool=esptool,
            port=port,
            sources=sources,
            layout=FLASH_LAYOUT,
            progress=progress,
        )
        return
    with tempfile.TemporaryDirectory(prefix="zone-lite-otadata-") as directory:
        otadata = Path(directory) / "otadata-ota0.bin"
        otadata.write_bytes(otadata_selecting_ota_0())
        flash_sources_and_verify(
            esptool=esptool,
            port=port,
            sources={
                **sources,
                "otadata-ota0.bin": otadata,
                ROLLBACK_PREDECESSOR_IMAGE: signed_package / ROLLBACK_PREDECESSOR_IMAGE,
                STORAGE_IMAGE: signed_package / STORAGE_IMAGE,
            },
            layout=PRODUCTION_LAYOUT,
            progress=progress,
            # The first-use device is not in Secure Download Mode yet, so the
            # stub can hash each range on chip. A ROM read_flash readback of
            # the 8 MiB storage image alone takes about 15 minutes over USB,
            # and on 29 Oct 2026 the readback of the second application range
            # failed its CRC on every new board although the bytes were right.
            verify_with_stub=True,
        )


def flash_sources_and_verify(
    *,
    esptool: str,
    port: str,
    sources: dict[str, Path],
    layout: tuple[tuple[str, int], ...],
    progress: FlashProgress | None = None,
    verify_with_stub: bool = False,
) -> None:
    image_bytes = sum(sources[name].stat().st_size for name, _offset in layout)
    total_bytes = image_bytes * 2
    command = [
        esptool,
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--before",
        "default_reset",
        "--after",
        "no_reset",
        "--no-stub",
        "write_flash",
        "--flash_mode",
        "dio",
        "--flash_size",
        "keep",
        "--flash_freq",
        "80m",
    ]
    for name, offset in layout:
        command.extend([hex(offset), str(sources[name])])
    last_streamed_bytes = 0
    ordered_ranges = [
        (offset, offset + sources[name].stat().st_size, sources[name].stat().st_size)
        for name, offset in layout
    ]

    def write_line(line: str) -> None:
        nonlocal last_streamed_bytes
        match = re.search(r"Writing at 0x([0-9A-Fa-f]+)", line, re.IGNORECASE)
        if not match:
            return
        address = int(match.group(1), 16)
        completed = 0
        for start, end, size in ordered_ranges:
            if address >= end:
                completed += size
            elif address >= start:
                completed += address - start
                break
            else:
                break
        completed = min(image_bytes, max(0, completed))
        # Bound event/audit volume while retaining smooth, measured progress.
        threshold = max(256 * 1024, image_bytes // 50)
        if progress and completed - last_streamed_bytes >= threshold:
            last_streamed_bytes = completed
            progress("FLASHING", completed, total_bytes)

    run(command, line_callback=write_line)
    completed_bytes = image_bytes
    if progress:
        progress("FLASHING", completed_bytes, total_bytes)

    if verify_with_stub:
        for name, offset in layout:
            source = sources[name]
            verify_flash_range(esptool=esptool, port=port, name=name, offset=offset,
                               source=source)
            completed_bytes += source.stat().st_size
            if progress:
                progress("READBACK_VERIFYING", completed_bytes, total_bytes)
        return

    with tempfile.TemporaryDirectory(prefix="zone-lite-flash-readback-") as directory:
        temporary = Path(directory)
        for name, offset in layout:
            source = sources[name]
            readback = temporary / name
            run(
                [
                    esptool,
                    "--chip",
                    "esp32s3",
                    "--port",
                    port,
                    "--before",
                    "default_reset",
                    "--after",
                    "no_reset",
                    "--no-stub",
                    "read_flash",
                    hex(offset),
                    hex(source.stat().st_size),
                    str(readback),
                ]
            )
            if sha256(readback) != sha256(source):
                raise RuntimeError(f"Flash readback verification failed for {name}")
            completed_bytes += source.stat().st_size
            if progress:
                progress("READBACK_VERIFYING", completed_bytes, total_bytes)


def verify_flash_range(*, esptool: str, port: str, name: str, offset: int, source: Path) -> None:
    """Compare one flashed range with its source by the stub's on-chip MD5."""
    command = [
        esptool,
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--before",
        "default_reset",
        "--after",
        "no_reset",
        "verify_flash",
        hex(offset),
        str(source),
    ]
    try:
        output = run(command)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Flash verification failed for {name} at {hex(offset)}") from error
    if "digest matched" not in output.lower():
        raise RuntimeError(f"Flash verification failed for {name} at {hex(offset)}")


def flash_managed_and_verify(
    *,
    esptool: str,
    port: str,
    signed_package: Path,
    provision_nvs: Path,
    progress: FlashProgress | None = None,
) -> None:
    """Reprovision only NVS and the signed factory recovery app.

    Storage/outbox, OTA slots, bootloader, partition table and security fuses
    are deliberately not erased or rewritten for a known managed device.
    """
    layout = (("provision.bin", 0x11000), ("zone-lite-signed.bin", 0x20000))
    flash_sources_and_verify(
        esptool=esptool,
        port=port,
        sources={
            "provision.bin": provision_nvs,
            "zone-lite-signed.bin": signed_package / "zone-lite-signed.bin",
        },
        layout=layout,
        progress=progress,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--signed-package", type=Path, required=True)
    parser.add_argument("--provisioning-manifest", type=Path, required=True)
    parser.add_argument("--provision-nvs", type=Path, required=True)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--confirm-efuse-burn-for", required=True)
    parser.add_argument("--confirm-secure-boot-for", required=True)
    parser.add_argument("--esptool", default=shutil.which("esptool.py") or "esptool.py")
    parser.add_argument("--espefuse", default=shutil.which("espefuse.py") or "espefuse.py")
    args = parser.parse_args()

    mac = read_mac(args.esptool, args.port)
    if normalize_mac(args.confirm_efuse_burn_for) != mac:
        raise SystemExit("HMAC eFuse approval does not match the attached ESP")
    if normalize_mac(args.confirm_secure_boot_for) != mac:
        raise SystemExit("Secure Boot approval does not match the attached ESP")
    validate_inputs(
        signed_package=args.signed_package,
        provisioning_manifest=args.provisioning_manifest,
        provision_nvs=args.provision_nvs,
        hmac_key=args.hmac_key,
        mac=mac,
        expected_git_sha=args.expected_git_sha,
    )

    with tempfile.TemporaryDirectory(prefix="zone-lite-efuse-summary-") as directory:
        ensure_nvs_hmac_key(
            espefuse=args.espefuse,
            port=args.port,
            mac=mac,
            key_path=args.hmac_key,
            summary_path=Path(directory) / "efuse-summary.json",
            confirmation=mac,
            trust_existing=False,
            split_root_recovery=False,
        )
    flash_and_verify(
        esptool=args.esptool,
        port=args.port,
        signed_package=args.signed_package,
        provision_nvs=args.provision_nvs,
    )
    print(
        f"Prepared and flash-verified Zone Lite device {mac}; "
        "first boot is intentionally pending"
    )


if __name__ == "__main__":
    main()

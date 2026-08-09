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
import subprocess
import tempfile
from typing import Callable

from provision_zone_lite import ensure_nvs_hmac_key, normalize_mac, read_mac


FLASH_LAYOUT = (
    ("bootloader-signed.bin", 0x0),
    ("partition-table.bin", 0x10000),
    ("provision.bin", 0x11000),
    ("ota_data_initial.bin", 0x17000),
    ("zone-lite-signed.bin", 0x20000),
)

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

    provisioning = json.loads(provisioning_manifest.read_text(encoding="utf-8"))
    aad = provisioning.get("aad", {})
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
    flash_sources_and_verify(
        esptool=esptool,
        port=port,
        sources=sources,
        layout=FLASH_LAYOUT,
        progress=progress,
    )


def flash_sources_and_verify(
    *,
    esptool: str,
    port: str,
    sources: dict[str, Path],
    layout: tuple[tuple[str, int], ...],
    progress: FlashProgress | None = None,
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
        f"Prepared and readback-verified Zone Lite device {mac}; "
        "first boot is intentionally pending"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build, provision, flash, and verify one Zone Lite ESP32-S3.

Secrets are accepted only through environment variables or a caller-owned JSON
file. Generated CSV/NVS material lives in a TemporaryDirectory and is destroyed
after the read-back hash has been verified.
"""

from __future__ import annotations

import argparse
import hmac
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import base64


HKDF_SALT = b"state-life-zone-lite-onboarding-v1"
NVS_HMAC_SALT = b"state-life-zone-lite-nvs-hmac-v1"
MAC_PATTERN = re.compile(r"MAC:\s*([0-9a-f:]{17})", re.IGNORECASE)
NVS_PARTITION_SIZE = 0x6000
NVS_PARTITION_OFFSET = "0x11000"
SECURE_SDKCONFIG_VALUES = (
    "CONFIG_NVS_ENCRYPTION=y",
    "CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y",
    "CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0",
    "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
    "CONFIG_SECURE_BOOT=y",
    "CONFIG_SECURE_BOOT_V2_ENABLED=y",
)
SECURE_BUILD_DEFINES = (
    "#define CONFIG_NVS_ENCRYPTION 1",
    "#define CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC 1",
    "#define CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID 0",
    "#define CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE 1",
    "#define CONFIG_SECURE_BOOT 1",
    "#define CONFIG_SECURE_BOOT_V2_ENABLED 1",
)


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout


def normalize_mac(value: str) -> str:
    compact = "".join(character for character in value.lower() if character in "0123456789abcdef")
    if len(compact) != 12:
        raise ValueError("Could not normalize the ESP Wi-Fi MAC address")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def derive_bootstrap_secret(mac: str, fleet_root: str) -> str:
    normalized = normalize_mac(mac)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=HKDF_SALT,
        info=f"zone-lite:{normalized}".encode("ascii"),
    ).derive(fleet_root.encode("utf-8"))
    return base64.urlsafe_b64encode(derived).decode("ascii")


def derive_nvs_hmac_key(mac: str, fleet_root: str) -> bytes:
    normalized = normalize_mac(mac)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=NVS_HMAC_SALT,
        info=f"zone-lite-nvs:{normalized}".encode("ascii"),
    ).derive(fleet_root.encode("utf-8"))


def load_config(path: Path) -> dict:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("Provisioning JSON must contain an object")
    fleet_root = os.environ.get("ADD_FLEET_ROOT_SECRET")
    if not fleet_root:
        raise ValueError(
            "Set ADD_FLEET_ROOT_SECRET explicitly before provisioning; "
            "ADD_PII_LOOKUP_KEY is never a safe provisioning fallback"
        )
    values["fleet_root"] = fleet_root
    values["nvs_hmac_root"] = (
        os.environ.get("ZONE_LITE_NVS_HMAC_ROOT_SECRET") or fleet_root
    )
    return values


def validate_root_selection(
    *,
    mac: str,
    fleet_root: str,
    nvs_hmac_root: str,
    recovery_confirmation: str | None,
    trust_existing: bool,
) -> bool:
    """Require an exact-MAC ceremony before using separate onboarding/NVS roots.

    Normal provisioning derives both domains from ADD_FLEET_ROOT_SECRET. A split
    is supported only to recover a device whose unreadable HMAC eFuse was
    previously derived from the wrong-but-known root. It must never become the
    accidental default for a new eFuse burn.
    """
    roots_match = hmac.compare_digest(
        hashlib.sha256(fleet_root.encode("utf-8")).digest(),
        hashlib.sha256(nvs_hmac_root.encode("utf-8")).digest(),
    )
    if roots_match:
        if recovery_confirmation:
            raise RuntimeError(
                "--confirm-split-root-recovery-for was supplied but the roots match"
            )
        return False
    if not trust_existing:
        raise RuntimeError(
            "Split-root recovery is allowed only with --trust-existing-derived-hmac"
        )
    try:
        confirmed_mac = normalize_mac(recovery_confirmation or "")
    except ValueError:
        confirmed_mac = ""
    if confirmed_mac != mac:
        raise RuntimeError(
            "Split-root recovery requires explicit approval for this exact ESP. "
            f"Re-run with --confirm-split-root-recovery-for {mac}."
        )
    return True


def read_mac(esptool: str, port: str) -> str:
    output = run([esptool, "--port", port, "chip_id"])
    match = MAC_PATTERN.search(output)
    if not match:
        raise RuntimeError("esptool did not report a Wi-Fi MAC address")
    return normalize_mac(match.group(1))


def nvs_rows(config: dict, bootstrap_secret: str) -> list[list[object]]:
    required = (
        "wifi_ssid",
        "wifi_password",
        "zone_device_id",
        "zone_id",
        "zone_name",
        "ords_base_url",
        "ords_username",
        "ords_password",
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing provisioning values: {', '.join(missing)}")
    values: list[tuple[str, str, str, object]] = [
        ("zone_cfg", "namespace", "", ""),
        ("provisioned", "data", "u8", 1),
        ("wifi_ssid", "data", "string", config["wifi_ssid"]),
        ("wifi_pass", "data", "string", config["wifi_password"]),
        ("zkt_port", "data", "u16", int(config.get("zkt_port", 4370))),
        ("zkt_key", "data", "u32", int(config.get("zkt_comm_key", 0))),
        ("zkt_ip", "data", "string", config.get("zkt_preferred_ip", "0.0.0.0")),
        ("zkt_serial", "data", "string", config.get("zkt_expected_serial", "")),
        ("zone_dev", "data", "string", config["zone_device_id"]),
        ("zone_id", "data", "string", config["zone_id"]),
        ("zone_name", "data", "string", config["zone_name"]),
        ("ords_url", "data", "string", config["ords_base_url"]),
        ("ords_user", "data", "string", config["ords_username"]),
        ("ords_pass", "data", "string", config["ords_password"]),
        ("rec_enable", "data", "u8", int(bool(config.get("zkt_recovery_enabled", True)))),
        ("rec_fails", "data", "u32", int(config.get("zkt_recovery_failures", 2))),
        ("rec_cool", "data", "u32", int(config.get("zkt_recovery_cooldown_ms", 1_800_000))),
        ("reboot_wait", "data", "u32", int(config.get("zkt_reboot_wait_ms", 90_000))),
        ("tel_port", "data", "u16", int(config.get("zkt_telnet_port", 23))),
        ("tel_user", "data", "string", config.get("zkt_telnet_username", "")),
        ("tel_pass", "data", "string", config.get("zkt_telnet_password", "")),
        ("tel_banner", "data", "string", config.get("zkt_telnet_banner", "Linux")),
        ("tel_cmd", "data", "string", config.get("zkt_telnet_command", "reboot")),
        ("add_enabled", "data", "u8", 1),
        (
            "add_onboard",
            "data",
            "string",
            config.get("add_onboard_url", "https://autoattn.slichealth.com/device/v2/onboard"),
        ),
        ("add_ws", "data", "string", ""),
        ("boot_secret", "data", "string", bootstrap_secret),
        ("conn_id", "data", "string", ""),
        ("dev_token", "data", "string", ""),
    ]
    return [[key, kind, encoding, value] for key, kind, encoding, value in values]


def find_nvs_generator(idf_path: Path) -> Path:
    candidate = idf_path / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"
    if not candidate.is_file():
        raise FileNotFoundError(f"NVS partition generator not found under {idf_path}")
    return candidate


def validate_secure_build_config(project: Path) -> None:
    """Refuse provisioning unless the effective image uses HMAC-backed NVS.

    sdkconfig.defaults is intentionally not sufficient evidence: an ignored,
    previously generated sdkconfig can override those defaults. Checking both
    the resolved sdkconfig and the generated build header ties this gate to the
    image that is about to be flashed.
    """
    sdkconfig_path = project / "sdkconfig"
    build_header_path = project / "build" / "config" / "sdkconfig.h"
    if not sdkconfig_path.is_file() or not build_header_path.is_file():
        raise RuntimeError(
            "Secure build configuration is missing; run a clean idf.py build before provisioning"
        )
    sdkconfig = sdkconfig_path.read_text(encoding="utf-8")
    build_header = build_header_path.read_text(encoding="utf-8")
    missing_values = [value for value in SECURE_SDKCONFIG_VALUES if value not in sdkconfig]
    missing_defines = [value for value in SECURE_BUILD_DEFINES if value not in build_header]
    if missing_values or missing_defines:
        raise RuntimeError(
            "Refusing to provision an image without HMAC-backed NVS encryption; "
            "remove the stale generated sdkconfig, rebuild from sdkconfig.defaults, and retry"
        )


def efuse_summary(espefuse: str, port: str, destination: Path) -> dict:
    run(
        [
            espefuse,
            "--chip",
            "esp32s3",
            "--port",
            port,
            "summary",
            "--format",
            "json",
            "--file",
            str(destination),
        ]
    )
    return json.loads(destination.read_text(encoding="utf-8"))


def ensure_nvs_hmac_key(
    *,
    espefuse: str,
    port: str,
    mac: str,
    key_path: Path,
    summary_path: Path,
    confirmation: str | None,
    trust_existing: bool,
    split_root_recovery: bool,
) -> None:
    summary = efuse_summary(espefuse, port, summary_path)
    block = summary.get("BLOCK_KEY0", {})
    purpose = summary.get("KEY_PURPOSE_0", {}).get("value")
    raw = str(block.get("raw_value", ""))
    is_empty = raw in {"", "0x" + ("0" * 64)} and bool(block.get("writeable", False))
    if is_empty and purpose == "USER":
        if split_root_recovery:
            raise RuntimeError(
                "Split-root recovery is forbidden for an empty eFuse; provision new devices "
                "with one explicit ADD_FLEET_ROOT_SECRET"
            )
        try:
            confirmed_mac = normalize_mac(confirmation or "")
        except ValueError:
            confirmed_mac = ""
        if confirmed_mac != mac:
            raise RuntimeError(
                "Secure NVS requires one irreversible HMAC eFuse burn. "
                f"Re-run with --confirm-efuse-burn-for {mac} after explicit approval."
            )
        run(
            [
                espefuse,
                "--chip",
                "esp32s3",
                "--port",
                port,
                "--do-not-confirm",
                "burn_key",
                "BLOCK_KEY0",
                str(key_path),
                "HMAC_UP",
            ]
        )
        verified = efuse_summary(espefuse, port, summary_path)
        if verified.get("KEY_PURPOSE_0", {}).get("value") != "HMAC_UP":
            raise RuntimeError("HMAC eFuse purpose verification failed after burn")
        return
    if purpose != "HMAC_UP":
        raise RuntimeError("eFuse BLOCK_KEY0 is not available for Zone Lite HMAC NVS encryption")
    if not trust_existing:
        raise RuntimeError(
            "BLOCK_KEY0 already contains an unreadable HMAC key. Refusing to assume it was "
            "derived by this fleet; pass --trust-existing-derived-hmac only for a previously "
            "provisioned Zone Lite device."
        )


def firmware_flash_arguments(
    project: Path,
    esptool: str,
    port: str,
    nvs_binary: Path,
    signed_bootloader: Path,
    signed_app: Path,
) -> list[str]:
    build = project / "build"
    values = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    extra = values["extra_esptool_args"]
    command = [
        esptool,
        "--chip",
        str(extra.get("chip", "esp32s3")),
        "--port",
        port,
        "--before",
        str(extra.get("before", "default_reset")),
        "--after",
        str(extra.get("after", "hard_reset")),
        "write_flash",
        *[str(item) for item in values.get("write_flash_args", [])],
    ]
    for offset, filename in values["flash_files"].items():
        source = build / filename
        if str(filename).endswith("bootloader.bin"):
            source = signed_bootloader
        elif str(filename).endswith("zone_lite.bin"):
            source = signed_app
        command.extend([offset, str(source)])
    command.extend([NVS_PARTITION_OFFSET, str(nvs_binary)])
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--idf-path", type=Path, default=os.environ.get("IDF_PATH"))
    parser.add_argument("--esptool", default=shutil.which("esptool.py") or "esptool.py")
    parser.add_argument("--espefuse", default=shutil.which("espefuse.py") or "espefuse.py")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-firmware-flash", action="store_true")
    parser.add_argument("--confirm-efuse-burn-for")
    parser.add_argument("--confirm-secure-boot-for")
    parser.add_argument("--signed-bootloader", type=Path)
    parser.add_argument("--signed-app", type=Path)
    parser.add_argument("--trust-existing-derived-hmac", action="store_true")
    parser.add_argument("--confirm-split-root-recovery-for")
    args = parser.parse_args()
    if not args.idf_path:
        raise SystemExit("Pass --idf-path or export IDF_PATH")
    if not args.skip_firmware_flash:
        if normalize_mac(args.confirm_secure_boot_for or "") != read_mac(args.esptool, args.port):
            raise SystemExit(
                "Secure Boot is irreversible. Re-run with --confirm-secure-boot-for set to the exact detected MAC."
            )
        if not args.signed_bootloader or not args.signed_bootloader.is_file():
            raise SystemExit("Pass the independently verified three-key signed bootloader with --signed-bootloader.")
        if not args.signed_app or not args.signed_app.is_file():
            raise SystemExit("Pass the independently verified signed Zone Lite image with --signed-app.")

    project = Path(__file__).resolve().parents[1]
    config = load_config(args.config)
    mac = read_mac(args.esptool, args.port)
    fleet_root = config.pop("fleet_root")
    nvs_hmac_root = config.pop("nvs_hmac_root")
    split_root_recovery = validate_root_selection(
        mac=mac,
        fleet_root=fleet_root,
        nvs_hmac_root=nvs_hmac_root,
        recovery_confirmation=args.confirm_split_root_recovery_for,
        trust_existing=args.trust_existing_derived_hmac,
    )
    bootstrap_secret = derive_bootstrap_secret(mac, fleet_root)
    nvs_hmac_key = derive_nvs_hmac_key(mac, nvs_hmac_root)
    generator = find_nvs_generator(args.idf_path)

    if not args.skip_build:
        run(["idf.py", "build"], cwd=project)
    validate_secure_build_config(project)
    with tempfile.TemporaryDirectory(prefix="zone-lite-provision-") as directory:
        temporary = Path(directory)
        csv_path = temporary / "provision.csv"
        binary_path = temporary / "provision.bin"
        readback_path = temporary / "readback.bin"
        hmac_key_path = temporary / "hmac-key.bin"
        nvs_key_path = temporary / "nvs-xts-key.bin"
        summary_path = temporary / "efuse-summary.json"
        hmac_key_path.write_bytes(nvs_hmac_key)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["key", "type", "encoding", "value"])
            writer.writerows(nvs_rows(config, bootstrap_secret))
        run(
            [
                sys.executable,
                str(generator),
                "generate-key",
                "--key_protect_hmac",
                "--kp_hmac_inputkey",
                str(hmac_key_path),
                "--keyfile",
                str(nvs_key_path),
                "--outdir",
                str(temporary),
            ]
        )
        run(
            [
                sys.executable,
                str(generator),
                "encrypt",
                str(csv_path),
                str(binary_path),
                hex(NVS_PARTITION_SIZE),
                "--inputkey",
                str(nvs_key_path),
            ]
        )
        ensure_nvs_hmac_key(
            espefuse=args.espefuse,
            port=args.port,
            mac=mac,
            key_path=hmac_key_path,
            summary_path=summary_path,
            confirmation=args.confirm_efuse_burn_for,
            trust_existing=args.trust_existing_derived_hmac,
            split_root_recovery=split_root_recovery,
        )
        if args.skip_firmware_flash:
            run(
                [
                    args.esptool,
                    "--chip",
                    "esp32s3",
                    "--port",
                    args.port,
                    "write_flash",
                    NVS_PARTITION_OFFSET,
                    str(binary_path),
                ]
            )
        else:
            run(
                firmware_flash_arguments(
                    project,
                    args.esptool,
                    args.port,
                    binary_path,
                    args.signed_bootloader,
                    args.signed_app,
                )
            )
        run(
            [
                args.esptool,
                "--chip",
                "esp32s3",
                "--port",
                args.port,
                "read_flash",
                NVS_PARTITION_OFFSET,
                hex(NVS_PARTITION_SIZE),
                str(readback_path),
            ]
        )
        expected = hashlib.sha256(binary_path.read_bytes()).digest()
        actual = hashlib.sha256(readback_path.read_bytes()).digest()
        if expected != actual:
            raise RuntimeError("Provisioning partition read-back hash did not match")
    print(f"Provisioned and verified Zone Lite device {mac}")


if __name__ == "__main__":
    main()

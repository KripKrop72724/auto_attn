#!/usr/bin/env python3
"""Build an ADD-side encrypted-NVS package without exporting fleet secrets."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from provision_zone_lite import (
    NVS_PARTITION_OFFSET,
    NVS_PARTITION_SIZE,
    derive_bootstrap_secret,
    derive_nvs_hmac_key,
    find_nvs_generator,
    normalize_mac,
    nvs_rows,
)
from provisioning_envelope import REQUEST_ID_PATTERN, encrypt_for_recipient


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HEX_PSK_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")
RFC1918_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def required_text(values: dict, key: str, maximum: int) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"Invalid or missing provisioning field: {key}")
    return value


def validate_request(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("Provisioning request must be a JSON object")
    values = dict(values)
    request_id = required_text(values, "request_id", 96)
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("Invalid provisioning request ID")
    values["target_mac"] = normalize_mac(required_text(values, "target_mac", 32))
    zone_device_id = required_text(values, "zone_device_id", 31)
    zone_id = required_text(values, "zone_id", 64)
    if not IDENTIFIER_PATTERN.fullmatch(zone_device_id):
        raise ValueError("Invalid provisioning field: zone_device_id")
    if not IDENTIFIER_PATTERN.fullmatch(zone_id):
        raise ValueError("Invalid provisioning field: zone_id")
    zone_name = required_text(values, "zone_name", 120)
    if zone_name != zone_name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in zone_name):
        raise ValueError("Invalid provisioning field: zone_name")
    required_text(values, "wifi_ssid", 32)
    wifi_password = required_text(values, "wifi_password", 64)
    password_bytes = len(wifi_password.encode("utf-8"))
    if not HEX_PSK_PATTERN.fullmatch(wifi_password) and not 8 <= password_bytes <= 63:
        raise ValueError("Invalid Wi-Fi password")
    required_text(values, "recipient_public_key_b64", 128)
    port = int(values.get("zkt_port", 4370))
    comm_key = int(values.get("zkt_comm_key", 0))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid ZKT port")
    if not 0 <= comm_key <= 0xFFFFFFFF:
        raise ValueError("Invalid ZKT communication key")
    values["zkt_port"] = port
    values["zkt_comm_key"] = comm_key
    preferred_ip = required_text(values, "zkt_preferred_ip", 15)
    if preferred_ip != "0.0.0.0":
        try:
            address = ipaddress.IPv4Address(preferred_ip)
        except ipaddress.AddressValueError as exc:
            raise ValueError("Invalid ZKT preferred IP") from exc
        if not any(address in network for network in RFC1918_NETWORKS):
            raise ValueError("ZKT preferred IP must be RFC1918 unicast")
        values["zkt_preferred_ip"] = str(address)
    if values.get("zkt_expected_serial"):
        required_text(values, "zkt_expected_serial", 64)
    return values


def load_request(path: Path) -> dict:
    return validate_request(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--idf-path", type=Path, required=True)
    args = parser.parse_args()
    request = load_request(args.request)
    fleet_root = os.environ.get("ADD_FLEET_ROOT_SECRET")
    nvs_root = os.environ.get("ZONE_LITE_NVS_HMAC_ROOT_SECRET") or fleet_root
    required_environment = {
        "ADD_FLEET_ROOT_SECRET": fleet_root,
        "ADD_ORDS_BASE_URL": os.environ.get("ADD_ORDS_BASE_URL"),
        "ADD_ORDS_USERNAME": os.environ.get("ADD_ORDS_USERNAME"),
        "ADD_ORDS_PASSWORD": os.environ.get("ADD_ORDS_PASSWORD"),
    }
    missing = [name for name, value in required_environment.items() if not value]
    if missing:
        raise ValueError(f"Protected ADD environment is missing: {', '.join(missing)}")

    config = dict(request)
    config.update(
        ords_base_url=required_environment["ADD_ORDS_BASE_URL"],
        ords_username=required_environment["ADD_ORDS_USERNAME"],
        ords_password=required_environment["ADD_ORDS_PASSWORD"],
        add_onboard_url="https://autoattn.slichealth.com/device/v2/onboard",
    )
    bootstrap_secret = derive_bootstrap_secret(request["target_mac"], fleet_root)
    hmac_key = derive_nvs_hmac_key(request["target_mac"], nvs_root)
    generator = find_nvs_generator(args.idf_path)
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError("Provisioning output directory must be empty")

    with tempfile.TemporaryDirectory(prefix="zone-lite-package-") as directory:
        temporary = Path(directory)
        csv_path = temporary / "provision.csv"
        binary_path = temporary / "provision.bin"
        hmac_path = temporary / "hmac-key.bin"
        nvs_key_path = temporary / "nvs-xts-key.bin"
        hmac_path.write_bytes(hmac_key)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["key", "type", "encoding", "value"])
            writer.writerows(nvs_rows(config, bootstrap_secret))
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "generate-key",
                "--key_protect_hmac",
                "--kp_hmac_inputkey",
                str(hmac_path),
                "--keyfile",
                str(nvs_key_path),
                "--outdir",
                str(temporary),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                sys.executable,
                str(generator),
                "encrypt",
                str(csv_path),
                str(binary_path),
                hex(NVS_PARTITION_SIZE),
                "--inputkey",
                str(nvs_key_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        plaintext = binary_path.read_bytes()
        if len(plaintext) != NVS_PARTITION_SIZE:
            raise RuntimeError("Generated NVS partition has the wrong size")
        aad = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "target_mac": request["target_mac"],
            "zone_id": request["zone_id"],
            "zone_device_id": request["zone_device_id"],
            "nvs_offset": NVS_PARTITION_OFFSET,
            "nvs_size": NVS_PARTITION_SIZE,
            "nvs_sha256": hashlib.sha256(plaintext).hexdigest(),
        }
        ciphertext, envelope = encrypt_for_recipient(
            plaintext,
            request["recipient_public_key_b64"],
            aad,
        )
        hmac_aad = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "target_mac": request["target_mac"],
            "purpose": "zone-lite-nvs-hmac-efuse-key",
            "key_size": len(hmac_key),
        }
        hmac_ciphertext, hmac_envelope = encrypt_for_recipient(
            hmac_key,
            request["recipient_public_key_b64"],
            hmac_aad,
        )

    ciphertext_path = args.output / "provision.bin.enc"
    ciphertext_path.write_bytes(ciphertext)
    hmac_ciphertext_path = args.output / "hmac-key.bin.enc"
    hmac_ciphertext_path.write_bytes(hmac_ciphertext)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "aad": aad,
        "envelope": envelope,
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "hmac_key": {
            "aad": hmac_aad,
            "envelope": hmac_envelope,
            "ciphertext_sha256": hashlib.sha256(hmac_ciphertext).hexdigest(),
        },
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Created envelope-encrypted provisioning package request={request['request_id']} "
        f"target={request['target_mac']}"
    )


if __name__ == "__main__":
    main()

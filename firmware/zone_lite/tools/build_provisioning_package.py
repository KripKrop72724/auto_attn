#!/usr/bin/env python3
"""Build an ADD-side encrypted-NVS package without exporting fleet secrets."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
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


ZONE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def required_text(values: dict, key: str, maximum: int) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"Invalid or missing provisioning field: {key}")
    return value


def load_request(path: Path) -> dict:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("Provisioning request must be a JSON object")
    request_id = required_text(values, "request_id", 96)
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("Invalid provisioning request ID")
    values["target_mac"] = normalize_mac(required_text(values, "target_mac", 32))
    for key in ("zone_device_id", "zone_id", "zone_name"):
        if not ZONE_PATTERN.fullmatch(required_text(values, key, 64)):
            raise ValueError(f"Invalid provisioning field: {key}")
    required_text(values, "wifi_ssid", 32)
    required_text(values, "wifi_password", 64)
    required_text(values, "recipient_public_key_b64", 128)
    port = int(values.get("zkt_port", 4370))
    comm_key = int(values.get("zkt_comm_key", 0))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid ZKT port")
    if not 0 <= comm_key <= 0xFFFFFFFF:
        raise ValueError("Invalid ZKT communication key")
    values["zkt_port"] = port
    values["zkt_comm_key"] = comm_key
    required_text(values, "zkt_preferred_ip", 45)
    if values.get("zkt_expected_serial"):
        required_text(values, "zkt_expected_serial", 64)
    return values


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

    ciphertext_path = args.output / "provision.bin.enc"
    ciphertext_path.write_bytes(ciphertext)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "aad": aad,
        "envelope": envelope,
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
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

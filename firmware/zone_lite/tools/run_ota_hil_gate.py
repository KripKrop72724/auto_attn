#!/usr/bin/env python3
"""Execute a site-owned OTA rig command and enforce its evidence contract."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


REQUIRED_CHECKS = (
    "baseline_attendance_preserved",
    "download_power_loss_resumed",
    "first_boot_power_loss_rolled_back",
    "new_image_confirmed",
    "identity_queue_preserved",
    "legacy_device_unaffected",
    "wifi_setup_auto_recovery_opened",
    "wifi_setup_manual_button_opened",
    "wifi_setup_wrong_password_rejected",
    "wifi_setup_failed_candidate_restored",
    "wifi_setup_success_persisted_after_reboot",
    "wifi_setup_surface_isolated",
    "wifi_setup_attendance_regression_free",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    completed = subprocess.run(args.command, shell=True, timeout=35 * 60, check=False)
    if completed.returncode:
        raise SystemExit(f"HIL rig command failed with exit code {completed.returncode}")
    if not args.evidence.is_file():
        raise SystemExit(f"HIL rig did not produce {args.evidence}")

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    missing = [name for name in REQUIRED_CHECKS if evidence.get("checks", {}).get(name) is not True]
    if missing:
        raise SystemExit("HIL evidence is missing passing checks: " + ", ".join(missing))
    if not evidence.get("device_serial") or not evidence.get("firmware_sha256"):
        raise SystemExit("HIL evidence must identify the device and tested firmware hash")
    print("OTA and Wi-Fi setup hardware gate passed with complete power-loss, isolation, and regression evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

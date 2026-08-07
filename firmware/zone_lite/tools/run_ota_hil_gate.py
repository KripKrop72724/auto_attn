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
    "reconciliation_nonzero_offset_verified",
    "reconciliation_disconnect_resumed",
    "reconciliation_power_cycle_resumed",
    "reconciliation_boundary_divergence_blocked",
    "reconciliation_live_punch_preserved",
    "reconciliation_full_storage_command_succeeded",
    "reconciliation_tail_audit_advanced",
    "reconciliation_tail_invalid_first_advanced",
    "reconciliation_tail_invalid_middle_advanced",
    "reconciliation_tail_invalid_final_advanced",
    "reconciliation_tail_multiple_malformed_accounted",
    "reconciliation_tail_ack_loss_replayed_exactly",
    "reconciliation_tail_poison_followers_preserved",
    "reconciliation_source_exception_visible_in_add",
    "reconciliation_source_cursor_parity_exact",
    "reconciliation_source_chain_continuous",
    "reconciliation_no_legacy_full_scan_after_certificate",
    "reconciliation_stream_v2_advertised",
    "reconciliation_partial_final_1_committed",
    "reconciliation_partial_final_31_committed",
    "reconciliation_partial_final_99_committed",
    "reconciliation_source_probe_transient_resumed",
    "reconciliation_source_probe_stable_epoch_created",
    "reconciliation_source_probe_unstable_held",
    "reconciliation_recovery_prefix_preserved",
    "reconciliation_four_chunks_one_prepare",
    "reconciliation_free_data_before_network_wait",
    "reconciliation_ack_cursor_chain_validated",
    "reconciliation_stale_assignment_rejected",
    "reconciliation_live_event_interleaving_recovered",
    "reconciliation_heap_stable",
    "reconciliation_24h_soak_stable",
    "admin_lease_duration_started_after_grant",
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
    if evidence.get("zone_id") != "ZONE-KARACHI-01":
        raise SystemExit("Reconciliation stream-v2 promotion requires the production Karachi canary")
    baseline_rate = float(evidence.get("baseline_records_per_second") or 0)
    stream_v2_rate = float(evidence.get("stream_v2_records_per_second") or 0)
    if baseline_rate <= 0 or stream_v2_rate / baseline_rate < 4.0:
        raise SystemExit(
            "Karachi stream-v2 throughput must be at least 4x the preserved 2.3.0 baseline"
        )
    print(
        "OTA, Wi-Fi setup, and ADD-owned reconciliation hardware gates passed "
        "with poison-record advancement, exact replay, complete power-loss, isolation, "
        "partial-tail completion, source-epoch recovery, resume, 24-hour stability, "
        "and >=4x Karachi evidence"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

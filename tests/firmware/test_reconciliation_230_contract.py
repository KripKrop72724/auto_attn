from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ZONE = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
CONNECTOR = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
PROJECT = (ROOT / "firmware/zone_lite/CMakeLists.txt").read_text()
HIL_GATE = (ROOT / "firmware/zone_lite/tools/run_ota_hil_gate.py").read_text()


def test_230_uses_bounded_verified_range_resume_and_add_checkpoints():
    assert "project(zone_lite VERSION 2.3.0)" in PROJECT
    assert "CMD_READ_BUFFER_CHUNK" in ZONE
    assert "zk_prepare_bounded_buffer" in ZONE
    assert "zk_read_bounded_range" in ZONE
    assert "SOURCE_FIRST_ANCHOR_DIVERGED" in ZONE
    assert "SOURCE_COMMITTED_BOUNDARY_DIVERGED" in ZONE
    assert '"reconcile_chunk"' in ZONE
    assert '"reconcile_source_manifest"' in ZONE
    assert "committed_predecessor_digest" in CONNECTOR


def test_add_is_command_authority_when_optional_flash_cache_is_full():
    assert "ADD is the durable command authority" in CONNECTOR
    assert "COMMAND_JOURNAL_FAILED" not in CONNECTOR
    assert "if (!queue_command_if_idle(&command))" in CONNECTOR
    assert '.format_if_mount_failed = false' in ZONE


def test_certified_baseline_switches_to_bounded_append_tail_audits():
    assert "ADD_SOURCE_COVERAGE_CERTIFIED" in ZONE
    assert "process_add_incremental_tail" in ZONE
    assert '"CURRENT_RECONCILE"' in ZONE
    assert "g_add_source_coverage_cursor = end" in ZONE
    assert "CURRENT_TAIL_CHECKPOINT_RETRY" in ZONE


def test_admin_lease_duration_starts_after_verified_terminal_elevation():
    assert "command.duration_seconds > 0 && command.duration_seconds <= 600" in ZONE
    assert "epoch_now() + lease_seconds" in ZONE


def test_physical_hil_gate_requires_reconciliation_fault_and_resume_evidence():
    for check in (
        "reconciliation_nonzero_offset_verified",
        "reconciliation_disconnect_resumed",
        "reconciliation_power_cycle_resumed",
        "reconciliation_boundary_divergence_blocked",
        "reconciliation_live_punch_preserved",
        "reconciliation_full_storage_command_succeeded",
        "reconciliation_tail_audit_advanced",
        "reconciliation_no_legacy_full_scan_after_certificate",
        "admin_lease_duration_started_after_grant",
    ):
        assert f'"{check}"' in HIL_GATE

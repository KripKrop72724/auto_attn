from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "apps/add_backend/migrations/versions/20260827_0021_attendance_repair.py"
CONTRACT = ROOT / "deploy/add/oracle/20260827_identity_repair_contract.sql"
PREFLIGHT = ROOT / "deploy/add/oracle/20260827_identity_repair_preflight.sql"
DOWNSTREAM = ROOT / "deploy/add/oracle/20260827_downstream_adapter_contract.sql"
TRUTH_API = ROOT / "deploy/add/oracle/slic_zkt_truth_api.sql"
DEPLOY_SCRIPT = ROOT / "deploy/add/deploy.ps1"
DEPLOY_WORKFLOW = ROOT / ".github/workflows/add-deploy.yml"


def test_additive_migration_contains_the_durable_repair_domain_once() -> None:
    source = MIGRATION.read_text()
    assert 'down_revision = "20260825_0020"' in source
    for table in (
        "add_attendance_repair_jobs",
        "add_attendance_repair_targets",
        "add_attendance_repair_cohorts",
        "add_attendance_repair_items",
        "add_attendance_identity_revisions",
        "add_oracle_identity_repair_receipts",
        "add_attendance_repair_events",
        "add_attendance_repair_oracle_slots",
        "add_attendance_repair_worker_heartbeat",
        "add_audit_chain_head",
    ):
        assert f'if "{table}" not in tables:' in source
    assert source.count('sa.Column("event_uid", sa.String(128), nullable=False)') == 1
    assert "effective_identity_revision_id" in source
    assert "identity_content_status" in source
    assert "identity_downstream_confirmed_at" in source
    assert '"preparation_attempt_count"' in source
    assert 'sa.Column("oracle_attempt_count"' in source
    assert 'sa.Column("downstream_attempt_count"' in source
    assert "ck_add_attendance_repair_oracle_slot_id" in source


def test_oracle_identity_repair_contract_is_add_only_and_non_destructive() -> None:
    source = CONTRACT.read_text().lower()
    for endpoint in (
        "raw-captures/identity-repairs/capabilities",
        "raw-captures/identity-repairs/check",
        "raw-captures/identity-repairs",
        "raw-captures/identity-repairs/status",
    ):
        assert endpoint in source
    assert "c_add_api_username" in source
    assert "c_fleet_api_username" not in source
    assert "c_api_username" not in source
    assert "delete from hr_raw_attn_capture_events" not in source
    assert "set employee_name = item.employee_name" in source
    assert "cnic = item.cnic" in source
    assert "datasync = 0" in source
    assert "operation_payload_digest" in source
    assert "content_precondition_mismatch" in source
    assert "review_required" in source
    assert "stale_old_identity_absent" in source
    assert "l_downstream_observed_at >= receipt.created_at" in source
    assert "identity_repair_previous_body" in source
    assert "automatic package restoration also failed" in source
    assert "whenever sqlerror exit failure rollback" in source
    assert "while l_offset <= l_length loop" in source
    assert "where event_uid = item.event_uid" in source
    assert "select count(*) into l_before_count from hr_raw_attn_capture_events" not in source
    # Oracle 19c cannot invoke these package-local PL/SQL helpers from SQL.
    assert "select sha256(" not in source
    assert "value bool_json(" not in source
    assert "l_token := sha256(l_material)" in source
    assert "l_ready_json := bool_json(l_ready)" in source
    check_body, repair_and_status = source.rsplit("procedure post_repair", maxsplit=1)
    repair_body = repair_and_status.split("procedure post_status", maxsplit=1)[0]
    assert "l_before_count := l_count" not in check_body
    assert "l_before_count := l_count" in repair_body


def test_oracle_rollout_implements_verified_production_downstream_semantics() -> None:
    preflight = PREFLIGHT.read_text().lower()
    downstream = DOWNSTREAM.read_text().lower()
    assert "duplicate event uids block" in preflight
    assert "single-column unique event_uid index is required" in preflight
    assert "al32utf8 is required" in preflight
    assert "objects referencing datasync" in preflight
    assert "hr_raw_attn_capture_events is read" in downstream
    assert "hr_employee is read only" in downstream
    assert "hr_employee_attendance is the sole downstream table" in downstream
    assert "procedure assert_repairable" in downstream
    assert "downstream_protected_attendance_day" in downstream
    assert "downstream_manual_attendance_day" in downstream
    assert "marked_by='biometric'" in downstream
    assert "delete from hr_employee_attendance" in downstream
    assert "delete from hr_raw_attn_capture_events" not in downstream
    assert re.search(r"\bupdate\s+hr_employee(?:\s|$)", downstream) is None
    assert re.search(r"\binsert\s+into\s+hr_employee(?:\s|$)", downstream) is None
    assert "update hr_leave" not in downstream
    assert "update hr_payroll" not in downstream
    assert "no_attendance_data_mutated_by_install=true" in downstream
    assert "stale_old_identity_absent" in downstream
    assert "slic_zkt_repair_downstream_status" in downstream


def test_existing_full_history_reconcile_delete_paths_remain_gated() -> None:
    source = TRUTH_API.read_text().lower()
    gated_deletes = re.findall(
        r"delete\s+from\s+hr_raw_attn_capture_events\s+d\s+where\s+1\s*=\s*0",
        source,
    )
    assert len(gated_deletes) == 2


def test_production_deploy_controls_both_repair_feature_gates() -> None:
    script = DEPLOY_SCRIPT.read_text()
    workflow = DEPLOY_WORKFLOW.read_text()
    for setting in (
        "ATTENDANCE_REPAIR_PREVIEW_ENABLED",
        "ATTENDANCE_REPAIR_EXECUTION_ENABLED",
    ):
        assert (
            f"ADD_DEPLOY_{setting}: ${{{{ vars.ADD_{setting} }}}}"
            in workflow
        )
        pattern = re.compile(
            rf'\$environment\["ADD_{setting}"\]\s*=\s*if\s*\('
            rf'\s*\$env:ADD_DEPLOY_{setting}\s+-eq\s+"true"\s*\)'
            rf'\s*\{{\s*"true"\s*\}}\s*else\s*\{{\s*"false"\s*\}}',
            re.DOTALL,
        )
        assert pattern.search(script)
    assert "Attendance repair execution requires attendance repair preview" in script

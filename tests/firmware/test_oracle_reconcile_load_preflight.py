from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class OracleReconcileLoadPreflight(unittest.TestCase):
    def test_replayed_truth_window_is_database_noop(self) -> None:
        package = (
            ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
        ).read_text(encoding="utf-8")
        body_start = package.index("create or replace package body")
        reconcile_start = package.index("procedure post_reconcile", body_start)
        reconcile = package[
            reconcile_start : package.index("end post_reconcile;", reconcile_start)
        ]

        change_guard = "if v_inserted + v_corrected + v_deleted > 0 then"
        helper = "slic_zkt_recompute_daily_flags(p_body);"
        idempotent_datasync = "where nvl(d.datasync, 0) <> 0"
        self.assertIn(change_guard, reconcile)
        self.assertIn(helper, reconcile)
        self.assertLess(reconcile.index(change_guard), reconcile.index(helper))
        self.assertIn(idempotent_datasync, reconcile)
        self.assertEqual(
            reconcile.count(
                "delete from hr_raw_attn_capture_events d\n         where 1 = 0"
            ),
            2,
        )

    def test_firmware_delegates_truth_before_direct_oracle_fallback(self) -> None:
        firmware = (
            ROOT / "firmware" / "zone_lite" / "main" / "zone_lite.c"
        ).read_text(encoding="utf-8")
        delegation_start = firmware.index(
            "bool window_add_ok = add_enqueue_reconcile_events("
        )
        delegation = firmware[
            delegation_start : firmware.index(
                "reconcile_dump_release(data);",
                delegation_start,
            )
        ]

        self.assertIn("zone_config_get()->add_enabled &&", delegation)
        self.assertIn('"ORACLE_RECONCILE_DELEGATED_TO_ADD"', delegation)
        self.assertIn("window_truth_ok = oracle_send_reconcile(", delegation)
        self.assertLess(
            delegation.index('"ORACLE_RECONCILE_DELEGATED_TO_ADD"'),
            delegation.index("window_truth_ok = oracle_send_reconcile("),
        )

    def test_live_migration_is_ddl_only_and_self_restoring(self) -> None:
        migration = (
            ROOT
            / "deploy"
            / "add"
            / "oracle"
            / "20260730_bound_noop_reconcile_cpu_non_destructive.sql"
        ).read_text(encoding="utf-8")
        normalized = migration.lower()

        self.assertIn("restore_original", normalized)
        self.assertIn("dbms_metadata.get_ddl", normalized)
        self.assertIn("attempted boolean := false", normalized)
        self.assertIn("execute immediate p", normalized)
        self.assertIn("execute immediate o", normalized)
        self.assertIn("where 1 = 0", normalized)
        self.assertNotIn("q'~", normalized)
        self.assertNotIn(
            "\ninsert into hr_raw_attn_capture_events",
            normalized,
        )
        self.assertNotIn(
            "\nupdate hr_raw_attn_capture_events",
            normalized,
        )


if __name__ == "__main__":
    unittest.main()

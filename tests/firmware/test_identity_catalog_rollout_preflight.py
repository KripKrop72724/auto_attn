from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "add_backend"))

from zk_add.identity_catalog import (  # noqa: E402
    IDENTITY_CATALOG_CHUNK_ROWS,
    identity_catalog_messages,
)


class IdentityCatalogRolloutPreflight(unittest.TestCase):
    def test_peshawar_sized_catalog_is_complete_ordered_and_bounded(self) -> None:
        rows = [
            {
                "uid": str(index + 1),
                "user_id": f"employee-{index + 1}",
                "display_name": f"Employee {index + 1}",
                "cnic": f"{index + 1:013d}",
                "shift_worker": False,
            }
            for index in range(731)
        ]

        messages = identity_catalog_messages({"rows": rows})
        begin, *middle, commit = messages

        self.assertEqual(begin["type"], "identity_catalog_begin")
        self.assertEqual(begin["rows_count"], 731)
        self.assertEqual(commit["type"], "identity_catalog_commit")
        self.assertEqual(commit["rows_count"], 731)
        self.assertEqual(commit["catalog_id"], begin["catalog_id"])
        self.assertEqual(len(middle), 12)

        rebuilt: list[dict] = []
        for chunk in middle:
            self.assertEqual(chunk["type"], "identity_catalog_chunk")
            self.assertEqual(chunk["catalog_id"], begin["catalog_id"])
            self.assertEqual(chunk["offset"], len(rebuilt))
            self.assertLessEqual(len(chunk["rows"]), IDENTITY_CATALOG_CHUNK_ROWS)
            rebuilt.extend(chunk["rows"])
        self.assertEqual(rebuilt, rows)

    def test_esp_activation_contract_does_not_depend_on_vfs_access(self) -> None:
        connector = (
            ROOT / "firmware" / "zone_lite" / "main" / "add_connector.c"
        ).read_text(encoding="utf-8")

        self.assertNotIn("access(ADD_IDENTITY_CATALOG_PATH", connector)
        self.assertIn("backup_result == 0", connector)
        self.assertIn("errno != ENOENT", connector)
        self.assertIn("ADD_IDENTITY_CATALOG_BACKUP_PATH", connector)
        self.assertIn("activate_identity_catalog(ADD_IDENTITY_CATALOG_STAGE_PATH)", connector)


if __name__ == "__main__":
    unittest.main()

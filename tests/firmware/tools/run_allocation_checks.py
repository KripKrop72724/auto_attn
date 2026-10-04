"""Run every pinned-cJSON fault harness; extra Python argv do not run scripts."""
from pathlib import Path
import subprocess
import sys

for name in ("check_json_allocations.py", "check_blocked_owner.py", "check_command_journal.py", "check_command_owner.py", "check_catalog_storage.py",
             "check_catalog_tombstones.py", "check_catalog_owner.py", "check_catalog_read_owner.py", "check_diagnostics_allocations.py", "check_custody_ack.py",
             "check_source_records.py", "check_source_wire.py", "check_ota_progress.py"):
    subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)

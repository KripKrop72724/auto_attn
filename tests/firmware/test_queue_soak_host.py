"""Short regression for the production-component soak harness; not a 24h receipt."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_queue_soak_harness(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "queue-soak"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-I", str(firmware), str(ROOT / "tests/firmware/soak/queue_soak.c"),
                    *[str(firmware / name) for name in
                      ("durable_queue.c", "storage_budget.c", "delivery_scheduler.c")],
                    "-o", str(executable)], check=True)
    data = tmp_path / "data"
    data.mkdir()
    result = subprocess.run([str(executable), "2", "1000"], cwd=data,
                            capture_output=True, text=True, check=True, timeout=20)
    assert "PASS seconds=2" in result.stdout

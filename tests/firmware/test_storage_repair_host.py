"""ZKT 2.7.5: the custody-first boot storage repair.

The decisions that need no filesystem and the metadata mirror run with plain
cc in every test shard."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]


def test_plan_and_mirror(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "storage-repair-plan"
    subprocess.run([compiler, *FLAGS, "-I", str(MAIN), str(ROOT / "tests/firmware/storage_repair_plan_host.c"),
                    *(str(MAIN / name) for name in ("storage_repair_plan.c", "storage_repair_mirror.c",
                                                     "legacy_queue.c", "durable_queue.c", "reliability.c",
                                                     "zkt_clock.c")),
                    "-o", str(executable)], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "storage repair plan host tests passed" in result.stdout

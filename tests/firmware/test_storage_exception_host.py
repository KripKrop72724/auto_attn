"""ZKT 2.7.7: the confirm-first storage exception's decision (storage_exception.c).

The gate on Peshawar-02's 21 October report and each of its terms alone, the
stable window, the record, the round outcomes, the bounded restart policy and
the OTA floor run with plain cc under ASan and UBSan in every test shard. The
engine's own report feeds the same module in the SPIFFS harness
(tools/check_spiffs_repair.py, reclaim-p02field)."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-sanitize-recover=all"]


def test_storage_exception_decisions(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "storage-exception"
    subprocess.run([compiler, *FLAGS, "-I", str(MAIN), str(ROOT / "tests/firmware/storage_exception_host.c"),
                    str(MAIN / "storage_exception.c"), "-o", str(executable)], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "storage exception host tests passed" in result.stdout


def test_the_decision_has_no_esp_idf_dependency() -> None:
    for name in ("storage_exception.c", "storage_exception.h"):
        includes = [line for line in (MAIN / name).read_text().splitlines() if line.startswith("#include")]
        assert includes and all(line.split()[1] in {'<errno.h>', '<stdbool.h>', '<stddef.h>', '<stdint.h>',
                                                     '<stdio.h>', '<stdlib.h>', '<string.h>',
                                                     '"storage_exception.h"'} for line in includes), includes

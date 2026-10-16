"""ZKT 2.7.5: the custody-first boot storage repair.

The decisions that need no filesystem and the metadata mirror run with plain
cc in every test shard. The engine runs against ESP-IDF 5.5.3's own SPIFFS with
power cuts (tools/check_spiffs_repair.py); CI's firmware-production job runs it
in espressif/idf:v5.5.3, and here it runs wherever the ESP-IDF sources are."""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]


def _tool():
    spec = importlib.util.spec_from_file_location("check_spiffs_repair",
                                                  ROOT / "tests/firmware/tools/check_spiffs_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_repair_against_esp_idf_spiffs_with_power_cuts(tmp_path: Path) -> None:
    tool = _tool()
    spiffs = tool.spiffs_source_dir()
    if spiffs is None:
        pytest.skip("ESP-IDF 5.5.3 SPIFFS sources are required (CI runs this in espressif/idf:v5.5.3)")
    binary = tool.build(spiffs, tmp_path)
    output = tool.run(binary, ["--seeds=2", "--organic=2", "--ticks=1200"])
    assert "cuts inside the repair" in output and "mirror equivalence passed" in output
    assert "0 committed rows lost" in output


def test_ci_runs_the_repair_harness_in_the_esp_idf_image() -> None:
    workflow = (ROOT / ".github/workflows/add-ci.yml").read_text()
    job = workflow[workflow.index("  firmware-production:"):workflow.index("  firmware-journal:")]
    step = job[job.index("Repair power-cut damage against ESP-IDF's own SPIFFS"):]
    step = step[:step.index("      - name:", 10)]
    assert "espressif/idf:v5.5.3" in step
    assert "python tests/firmware/tools/check_spiffs_repair.py" in step

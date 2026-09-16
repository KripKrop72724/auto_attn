"""Execute the production C primitives with memory/UB instrumentation."""
from pathlib import Path
import os
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("component", ["reliability", "durable_queue", "legacy_queue", "delivery_scheduler", "uid_cache", "storage_budget"])
def test_reliability_c_regressions(tmp_path: Path, component: str) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    firmware = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "reliability-test"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware),
        str(firmware / f"{component}.c"),
        *([str(firmware / "durable_queue.c")] if component == "legacy_queue" else []),
        str(ROOT / f"tests/firmware/{component}_host.c"), "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable), "queue-"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "regression tests passed" in result.stdout

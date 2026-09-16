"""Fault each actual production queue I/O call, then reopen the durable state."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_segmented_queue_io_faults(tmp_path: Path):
    compiler = shutil.which("cc")
    assert compiler
    executable = tmp_path / "faults"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(ROOT / "firmware/zone_lite/main"),
        str(ROOT / "tests/firmware/durable_queue_fault_host.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)

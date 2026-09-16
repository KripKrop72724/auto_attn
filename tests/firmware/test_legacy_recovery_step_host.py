"""Bound actual legacy prefix verification and retry its failed I/O slice."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_recovery_yields_and_retries_without_advancing_source(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "recovery"
    subprocess.run(
        [
            shutil.which("cc"),
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-g",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-I",
            str(firmware),
            str(ROOT / "tests/firmware/legacy_recovery_step_host.c"),
            str(firmware / "durable_queue.c"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], cwd=tmp_path, check=True)

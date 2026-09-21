"""Validate real pending segments without accepting counts as recovery evidence."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_durable_queue_bounded_integrity_audit(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "audit"
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
            str(ROOT / "tests/firmware/durable_queue_audit_host.c"),
            str(firmware / "durable_queue.c"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], cwd=tmp_path, check=True)

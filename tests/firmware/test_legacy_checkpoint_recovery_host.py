"""Recover a cursor only after exact original checkpoint custody."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_checkpoint_custody_precedes_replay_and_never_retires_file_content(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "checkpoint-recovery"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(main), str(ROOT / "tests/firmware/legacy_checkpoint_recovery_host.c"),
        str(main / "legacy_queue.c"), str(main / "durable_queue.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)

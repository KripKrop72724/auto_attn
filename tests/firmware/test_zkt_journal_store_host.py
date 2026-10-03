"""Fault actual journal filesystem calls and recover the retained bytes."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_journal_storage_faults_and_custody(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    binary = tmp_path / "journal-store"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_journal_store_host.c"),
                    str(main / "zkt_journal_codec.c"), str(main / "durable_queue.c"),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=90)

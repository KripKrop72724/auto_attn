"""Raw capture must commit before ACK, even with partial frame persistence."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_raw_capture_bounds_fragment_recovery_and_timeouts(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    binary = tmp_path / "journal-capture"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_journal_capture_host.c"),
                    *(str(main / source) for source in ["zkt_journal_codec.c", "durable_queue.c",
                        "zkt_storage_mailbox.c", "zkt_journal_capture.c"]),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)

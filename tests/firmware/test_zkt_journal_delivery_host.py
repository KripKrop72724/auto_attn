"""Exercise delivery against actual journal files and the bounded mailbox."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_journal_delivery_replay_timeouts_and_recovery(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    binary = tmp_path / "journal-delivery"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_journal_delivery_host.c"),
                    *(str(main / source) for source in ["zkt_journal_codec.c", "durable_queue.c",
                        "zkt_storage_mailbox.c", "zkt_custody_wire.c", "zkt_journal_delivery.c"]),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)

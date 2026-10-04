"""Journal runtime and OTA evidence must use the actual boot-mounted storage."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_boot_mount_journal_start_and_downgrade_scan_share_namespace(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    gateway = (main / "zone_lite.c").read_text()
    runtime = (main / "zkt_journal_runtime.c").read_text()
    constants = "\n".join(line for line in gateway.splitlines() if line.startswith(tuple(
        f"#define {name} " for name in ("STORAGE_BASE", "PENDING_PATH", "BLOCKED_PATH", "ACKED_PATH"))))
    actual = constants + "\n" + gateway[gateway.index("static bool g_queue_store_ready;"):
                                        gateway.index("static const char *oracle_capture_type(")]
    actual += runtime[runtime.index("static bool start_owner("):runtime.index("static bool owner_health(")]
    (tmp_path / "storage_namespace_actual.inc").write_text(actual)
    binary = tmp_path / "storage-namespace"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_storage_namespace_host.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)

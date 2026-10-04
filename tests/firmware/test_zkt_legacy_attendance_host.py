"""Preserve Oracle/blocked generations with the actual copied owner adapters."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_attendance_legacy_owner_faults_and_checkpoint_compatibility(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    source = (main / "zone_lite.c").read_text()
    actual = source[source.index("static bool append_line_policy("):source.index("static bool extract_event_uid(")]
    actual += source[source.index("static bool file_has_nonempty_line("):
                     source.index("static bool restore_pending_backup_if_needed(")]
    actual += source[source.index("static void oracle_drain_owned_pending("):
                     source.index("\n#endif", source.index("static void oracle_drain_owned_pending("))]
    (tmp_path / "legacy_delivery_actual.inc").write_text(actual)
    for header in ("nvs.h", "esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    executable = tmp_path / "attendance-legacy"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_legacy_attendance_host.c"),
                    *(str(main / name) for name in ("zkt_segmented_store.c", "zkt_segmented_client.c",
                                                   "durable_queue.c", "reliability.c")),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)

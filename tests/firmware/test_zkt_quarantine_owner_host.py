"""Execute retained quarantine recovery and receipt-before-retirement routing."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_quarantine_custody_storage_owner_and_family_paths(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "zone_lite.c").read_text()
    actual = source[source.index("static int legacy_pending_load(void *context, lq_checkpoint_t *checkpoint)\n{"):
                    source.index("static void oracle_drain_pending(bool live_first)")]
    actual += source[source.index("static legacy_queue_t g_legacy_quarantine[3];"):
                     source.index("static void ords_uploader_task(void *arg)")]
    (tmp_path / "quarantine_actual.inc").write_text(actual)
    for header in ("esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    for owner in (0, 1):
        executable = tmp_path / f"quarantine-{owner}"
        directory = tmp_path / f"family-{owner}"
        directory.mkdir()
        subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        f"-DZONE_LITE_QUEUE_OWNER={owner}", "-I", str(tmp_path),
                        "-I", str(ROOT / "tests/firmware"), "-I", str(main),
                        str(ROOT / "tests/firmware/zkt_quarantine_owner_host.c"),
                        *(str(main / name) for name in ("zkt_segmented_store.c", "zkt_segmented_client.c",
                                                       "durable_queue.c")),
                        "-o", str(executable)], check=True)
        subprocess.run([str(executable)], cwd=directory, check=True, timeout=30)

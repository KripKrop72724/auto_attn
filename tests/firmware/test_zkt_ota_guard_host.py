"""Run the actual pre-erase OTA adapter with orphan, timeout and reader faults."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_ota_reader_interlock_and_bounded_owner_checks(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ["esp_app_desc.h", "esp_timer.h", "nvs.h", "freertos/FreeRTOS.h", "freertos/task.h"]:
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_reader_platform_host.h"\n'
                        '#define pdMS_TO_TICKS(ms) (ms)\n'
                        'int64_t esp_timer_get_time(void);\nvoid vTaskDelay(unsigned ms);\n')
    executable = tmp_path / "ota-guard"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-Dopendir=guard_opendir", "-Dreaddir=guard_readdir", "-Dclosedir=guard_closedir",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_ota_guard_host.c"), str(main / "zkt_ota_guard.c"),
                    str(main / "zkt_journal_compat.c"), str(main / "durable_queue.c"),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)

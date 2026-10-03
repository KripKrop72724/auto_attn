"""Execute the owner checkpoint port and retained caller with injected NVS faults."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_owner_uncertain_commits_and_retained_timeouts(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ["esp_timer.h", "esp_app_desc.h", "nvs.h",
                   "freertos/FreeRTOS.h", "freertos/task.h"]:
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "runtime-owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_runtime_checkpoint_host.c"),
                    str(main / "zkt_runtime_checkpoint.c"), str(main / "durable_queue.c"),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)

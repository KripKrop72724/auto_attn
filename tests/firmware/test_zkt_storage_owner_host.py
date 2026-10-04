"""Fault key/counter/checkpoint persistence and exercise bounded owner scheduling."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def compile_and_run(tmp_path, name, sources):
    main = ROOT / "firmware/zone_lite/main"
    binary = tmp_path / name
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / f"tests/firmware/{name}_host.c"),
                    *(str(main / source) for source in sources), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)


def test_journal_key_counter_and_checkpoint_faults(tmp_path):
    compile_and_run(tmp_path, "zkt_journal_state",
                    ["zkt_journal_state.c", "zkt_journal_codec.c", "durable_queue.c"])


def test_storage_mailbox_saturation_timeout_and_fairness(tmp_path):
    compile_and_run(tmp_path, "zkt_storage_mailbox", ["zkt_storage_mailbox.c", "durable_queue.c"])


def test_storage_task_retains_timed_out_capture_and_drains_at_capacity(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ["esp_heap_caps.h", "esp_random.h", "esp_timer.h", "esp_app_desc.h", "nvs.h",
                   "freertos/FreeRTOS.h", "freertos/semphr.h", "freertos/task.h",
                   "mbedtls/platform_util.h"]:
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "storage-owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    *(f'-DZC_{key}_PATH="catalog.{key.lower()}"' for key in ("ACTIVE", "COMMIT", "BACKUP", "TEMP", "STAGE")),
                    *(f'-DZC_COMMAND_{key}_PATH="commands.{key.lower()}"' for key in ("ACTIVE", "COMMIT", "BACKUP", "TEMP", "STAGE")),
                    '-DZI_PROCESSED_PATH="processed.txt"', '-DZI_CANCELLED_PATH="cancelled.txt"',
                    str(fixture / "zkt_storage_owner_host.c"),
                    *(str(main / name) for name in ["zkt_storage_owner.c", "zkt_storage_mailbox.c", "zkt_runtime_checkpoint.c", "zkt_lease_store.c",
                        "zkt_journal_state.c", "zkt_journal_store.c", "zkt_journal_codec.c", "durable_queue.c",
                        "zkt_custody_wire.c", "zkt_catalog_store.c", "file_transaction.c", "zkt_command_id_store.c",
                        "zkt_command_id_client.c", "zkt_segmented_store.c", "zkt_segmented_client.c"]),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)
    for scenario in ["--checkpoint", "--recovery-full", "--runtime-corrupt-journal",
                     "--authority-before", "--authority-after", "--authority-readback", "--authority-bridge"]:
        directory = tmp_path / scenario.removeprefix("--")
        directory.mkdir()
        subprocess.run([str(binary), scenario], cwd=directory, check=True, timeout=30)

"""Retained 8 KiB queues through copied, bounded storage-owner requests."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_segmented_owner_preserves_bytes_and_retains_uncertain_replies(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ("esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "segmented-owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_segmented_owner_host.c"),
                    *(str(main / name) for name in ("zkt_segmented_store.c", "zkt_segmented_client.c", "durable_queue.c")),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_actual_queue_entrypoints_route_without_storage_fallback(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "queue_store.c").read_text()
    actual = source[source.index("bool qs_generation("):source.index("static int load(")]
    actual += source[source.index("dq_result_t qs_append_with_policy("):source.index("qs_health_t qs_health(")]
    (tmp_path / "segmented_routing_actual.inc").write_text(actual)
    binary = tmp_path / "segmented-routing"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_segmented_routing_host.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)

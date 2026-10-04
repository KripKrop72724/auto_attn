"""Execute actual ADD flat-file adapters through the copied storage protocol."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_add_legacy_owner_preserves_generations_and_custody_under_faults(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    source = (main / "add_connector.c").read_text()
    types = source[source.index("typedef struct {\n    const char *path;"):
                   source.index("typedef struct {\n    char receipt_id[40];")]
    (tmp_path / "add_legacy_types_actual.inc").write_text(types)
    actual = source[source.index("static bool write_outbox_cursor("):source.index("static off_t load_outbox_cursor(")]
    actual += source[source.index("static bool compact_outbox_locked("):source.index("static bool advance_outbox_locked(")]
    actual += source[source.index("#if defined(ZONE_LITE_QUEUE_OWNER)", source.index("static bool read_outbox_row_locked(")):
                     source.index("static bool attendance_payload_is_live(")]
    (tmp_path / "add_legacy_owner_actual.inc").write_text(actual)
    for header in ("esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "add-legacy-owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_add_legacy_owner_host.c"),
                    *(str(main / name) for name in ("zkt_segmented_store.c", "zkt_segmented_client.c",
                                                   "legacy_queue.c", "durable_queue.c", "reliability.c")),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_actual_add_producers_and_delivery_use_owner_without_direct_fallback(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "add_connector.c").read_text()
    types = source[source.index("typedef struct {\n    const char *path;"):
                   source.index("typedef struct {\n    char receipt_id[40];")]
    (tmp_path / "add_legacy_types_actual.inc").write_text(types)
    actual = source[source.index("static bool add_connector_enqueue_validated_line_with_policy("):
                    source.index("static bool add_connector_enqueue_validated_line(")]
    actual += source[source.index("bool add_connector_enqueue_attendance_bulk("):
                     source.index("static bool read_legacy_delivery(")]
    actual += source[source.index("static bool read_legacy_delivery("):
                     source.index("static void outbox_task(void *arg)\n{")]
    (tmp_path / "add_legacy_routing_actual.inc").write_text(actual)
    for owner_enabled in (0, 1):
        binary = tmp_path / f"add-legacy-routing-{owner_enabled}"
        directory = tmp_path / f"family-{owner_enabled}"
        directory.mkdir()
        subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        f"-DZONE_LITE_QUEUE_OWNER={owner_enabled}", "-I", str(tmp_path), "-I", str(main),
                        str(ROOT / "tests/firmware/zkt_add_legacy_routing_host.c"),
                        str(main / "reliability.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], cwd=directory, check=True, timeout=30)

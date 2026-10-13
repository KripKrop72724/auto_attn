"""ZKT 2.7.4: a read-only inventory of the attendance store reports which
files the filesystem cannot read back (13FL, 13 October 2026)."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware/zone_lite"


def test_inventory_maps_unreadable_bytes_without_writing(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "storage-inventory"
    store = tmp_path / "store"
    store.mkdir()
    subprocess.run([
        compiler, "-std=gnu11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(ROOT / "tests/firmware/cjson_subset"), "-I", str(FIRMWARE / "main"),
        "-include", str(ROOT / "tests/firmware/storage_inventory_host/fault.h"),
        str(FIRMWARE / "main/storage_inventory_core.c"),
        "-o", str(tmp_path / "core.o"), "-c",
    ], check=True)
    subprocess.run([
        compiler, "-std=gnu11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(ROOT / "tests/firmware/cjson_subset"), "-I", str(FIRMWARE / "main"),
        str(ROOT / "tests/firmware/storage_inventory_host.c"), str(tmp_path / "core.o"),
        "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable), str(store)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "storage inventory host tests passed" in result.stdout


def test_scan_starts_after_storage_and_feeds_the_heartbeat() -> None:
    app = (FIRMWARE / "main/zone_lite.c").read_text()
    body = app[app.index("void app_main(void)"):]
    assert body.index("boot_report_stage(BOOT_STAGE_STORAGE_INIT_RETURNED);") < body.index("storage_inventory_start();")
    connector = (FIRMWARE / "main/add_connector.c").read_text()
    assert "!storage_inventory_append(storage)) goto failed;" in connector
    component = (FIRMWARE / "main/CMakeLists.txt").read_text()
    assert '"storage_inventory_core.c" "storage_inventory.c"' in component

"""ZKT 2.7.1 boot report: why the ESP last reset and how the previous boot ended."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware/zone_lite"
sys.path.insert(0, str(ROOT / "apps" / "add_backend"))

from zk_add.schemas import BootPanic, BootReport, FirmwareDiagnostics  # noqa: E402


def test_reports_survive_resets_and_add_accepts_each_one(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "boot-report"
    subprocess.run([
        compiler, "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(ROOT / "tests/firmware/boot_report_host"), "-I", str(ROOT / "tests/firmware/cjson_subset"),
        "-I", str(FIRMWARE / "main"),
        str(ROOT / "tests/firmware/boot_report_host.c"), str(ROOT / "tests/firmware/cjson_subset/cJSON.c"),
        "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boot report host tests passed" in result.stdout
    reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(reports) == 11
    sent, sent_panic = set(), set()
    for report in reports:
        BootReport.model_validate(report)
        FirmwareDiagnostics.model_validate({"boot_report": report})
        sent |= report.keys()
        sent_panic |= report.get("panic", {}).keys()
    # Every field ADD accepts is one the firmware sends.
    assert sent == set(BootReport.model_fields)
    assert sent_panic == set(BootPanic.model_fields)


def test_boot_hooks_mark_each_stage_where_it_completes() -> None:
    component = (FIRMWARE / "main/CMakeLists.txt").read_text()
    assert '"boot_report.c"' in component
    assert '"-Wl,--wrap=esp_panic_handler"' in component
    assert '"-Wl,--wrap=_esp_error_check_failed"' in component
    app = (FIRMWARE / "main/zone_lite.c").read_text()
    body = app[app.index("void app_main(void)"):]
    assert body.split("{", 1)[1].lstrip().startswith("boot_report_init();")
    markers = ("esp_err_t ret = nvs_flash_init();", "boot_report_stage(BOOT_STAGE_NVS_READY);",
               "    storage_init();\n#endif\n    boot_report_stage(BOOT_STAGE_STORAGE_INIT_RETURNED);",
               "if (!wait_for_wifi()) {", "boot_report_stage(BOOT_STAGE_NETWORK_READY);")
    positions = [body.index(marker) for marker in markers]
    assert positions == sorted(positions)
    connector = (FIRMWARE / "main/add_connector.c").read_text()
    connected = connector[connector.index("case WEBSOCKET_EVENT_CONNECTED:"):]
    assert connected.split("\n", 2)[1].strip() == "boot_report_stage(BOOT_STAGE_ADD_CONNECTED);"
    assert "!platform_memory_append(memory) || !boot_report_append(diagnostics)) goto failed;" in connector

"""ZKT 2.7 platform memory and scheduling profile, and its heartbeat evidence."""
from pathlib import Path
import os
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware/zone_lite"


def test_platform_memory_evidence_and_json_allocation(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "platform-memory"
    subprocess.run([
        compiler, "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(ROOT / "tests/firmware/platform_memory_host"), "-I", str(FIRMWARE / "main"),
        str(FIRMWARE / "main/platform_memory.c"), str(ROOT / "tests/firmware/platform_memory_host.c"),
        "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "platform memory host tests passed" in result.stdout


def test_heartbeat_memory_keys_match_add_schema() -> None:
    harness = (ROOT / "tests/firmware/platform_memory_host.c").read_text()
    schema = (ROOT / "apps/add_backend/zk_add/schemas.py").read_text()
    block = schema[schema.index("class MemoryDiagnostics(BaseModel):"):]
    block = block[:block.index("\n\n\n")]
    accepted = set(re.findall(r"^    (\w+):", block, re.M))
    sent = set(re.findall(r'\\"(\w+)\\":', harness.split("expected_prefix =")[1].split(";")[0]))
    sent -= {"bytes", "caps", "name", "free_bytes"}  # nested row fields
    assert sent == accepted, (sent ^ accepted)


def test_zkt_profile_moves_tls_and_scheduling_off_internal_ram() -> None:
    profile = (FIRMWARE / "sdkconfig.zkt.defaults").read_text()
    for line in ("CONFIG_FREERTOS_HZ=1000", "CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y",
                 "# CONFIG_MBEDTLS_HARDWARE_AES is not set", "# CONFIG_MBEDTLS_DEFAULT_MEM_ALLOC is not set",
                 "# CONFIG_ESP_WIFI_IRAM_OPT is not set", "# CONFIG_ESP_WIFI_RX_IRAM_OPT is not set",
                 "# CONFIG_MBEDTLS_SSL_KEEP_PEER_CERTIFICATE is not set", "CONFIG_FREERTOS_USE_TRACE_FACILITY=y"):
        assert line in profile.splitlines(), line
    project = (FIRMWARE / "CMakeLists.txt").read_text()
    assert 'set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.zkt.defaults")' in project
    assert 'set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.hikvision.defaults")' in project


def test_no_zero_tick_waits_and_queues_live_in_psram() -> None:
    for path in (FIRMWARE / "main").glob("*.c"):
        text = path.read_text()
        # With a positive millisecond argument every wait is now a real tick.
        assert not re.search(r"vTaskDelay\(\s*0\s*\)|pdMS_TO_TICKS\(\s*0\s*\)", text), path.name
    connector = (FIRMWARE / "main/add_connector.c").read_text()
    assert "xQueueCreateWithCaps(depth, item_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)" in connector
    for queue in ("s_commands", "s_config_commands", "s_reconcile_assignments", "s_source_coverage",
                  "s_inbound_messages"):
        assert re.search(rf"{queue} = psram_queue\(", connector), queue
    assert "!platform_memory_append(memory)) goto failed;" in connector
    app = (FIRMWARE / "main/zone_lite.c").read_text()
    body = app[app.index("void app_main(void)"):]
    assert body.index("platform_memory_init();") < body.index("cJSON") if "cJSON" in body else True
    assert body.split("{", 1)[1].lstrip().startswith("platform_memory_init();")

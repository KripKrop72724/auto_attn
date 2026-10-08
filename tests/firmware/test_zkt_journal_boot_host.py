from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_journal_startup_and_recovery_state_machine(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "journal-boot"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_journal_boot_host.c"),
                    str(main / "zkt_journal_boot.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)


@pytest.mark.parametrize("bridge_version", ["2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.23"])
def test_actual_runtime_security_version_and_writer_build_gates(tmp_path, bridge_version):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ("esp_app_desc.h", "esp_ota_ops.h", "esp_secure_boot.h", "esp_err.h", "sdkconfig.h"):
        (tmp_path / header).write_text('#include "zkt_reader_platform_host.h"\n')
    (tmp_path / "esp_timer.h").write_text('#include <stdint.h>\nint64_t esp_timer_get_time(void);\n')
    (tmp_path / "freertos").mkdir()
    (tmp_path / "freertos/FreeRTOS.h").write_text('''#pragma once
typedef void *SemaphoreHandle_t;
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
''')
    (tmp_path / "freertos/semphr.h").write_text('''#pragma once
#include "FreeRTOS.h"
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
void xSemaphoreGive(SemaphoreHandle_t);
''')
    (tmp_path / "cJSON.h").write_text('''#pragma once
typedef struct { int unused; } cJSON;
cJSON *cJSON_CreateObject(void);
cJSON *cJSON_AddBoolToObject(cJSON *,const char *,int);
cJSON *cJSON_AddStringToObject(cJSON *,const char *,const char *);
cJSON *cJSON_AddNumberToObject(cJSON *,const char *,double);
int cJSON_AddItemToObject(cJSON *,const char *,cJSON *);
void cJSON_Delete(cJSON *);
''')
    for encrypted, writer in ((1, 1), (1, 0), (0, 1), (0, 0)):
        executable = tmp_path / f"runtime-{encrypted}-{writer}"
        subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                        f'-DZJ_BRIDGE_VERSION="{bridge_version}"', f"-DCONFIG_NVS_ENCRYPTION={encrypted}", f"-DZONE_LITE_JOURNAL_WRITES={writer}",
                        "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                        str(fixture / "zkt_journal_runtime_host.c"), str(main / "zkt_journal_boot.c"),
                        "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True, timeout=30)

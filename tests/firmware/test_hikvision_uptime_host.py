"""Guard integer uptime against cJSON_SetNumberValue macro cast precedence."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_heartbeat_uptime_remains_an_integer_on_the_wire(tmp_path):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    cjson = idf / "components/json/cJSON"
    if not (cjson / "cJSON.c").is_file():
        pytest.skip("Pinned ESP-IDF cJSON source is required")
    source = (ROOT / "firmware/zone_lite/main/hikvision_runtime.c").read_text()
    start = source.index('    cJSON *uptime = cJSON_GetObjectItemCaseSensitive(payload, "uptime_seconds");')
    body = source[start:source.index("\n}", start)]
    harness = r'''
#include "cJSON.h"
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
static int64_t esp_timer_get_time(void){return 123999999;}
int main(void){
 cJSON *payload=cJSON_CreateObject();
 cJSON_AddNumberToObject(payload,"uptime_seconds",122);
 /* PRODUCTION */
 char *wire=cJSON_PrintUnformatted(payload);
 assert(wire && !strcmp(wire,"{\"uptime_seconds\":123}"));
 free(wire);cJSON_Delete(payload);return 0;
}
'''
    unit = tmp_path / "uptime.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "uptime"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-deprecated-declarations", "-fsanitize=address,undefined", "-I", str(cjson), str(unit),
                    str(cjson / "cJSON.c"), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)

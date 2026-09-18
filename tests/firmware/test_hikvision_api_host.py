import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_hikvision_bounded_history_and_verified_writes(tmp_path):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    cjson = idf / "components/json/cJSON"
    if not (cjson / "cJSON.c").is_file():
        pytest.skip("ESP-IDF cJSON source required for the firmware API qualification harness")
    (tmp_path / "esp_http_client.h").write_text(
        "#pragma once\ntypedef enum {HTTP_METHOD_GET, HTTP_METHOD_POST, HTTP_METHOD_PUT} esp_http_client_method_t;\n"
    )
    (tmp_path / "esp_random.h").write_text("#include <stdint.h>\nuint32_t esp_random(void);\n")
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "hik-api"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-Wno-deprecated-declarations", "-fsanitize=address,undefined", "-I", str(tmp_path), "-I", str(main), "-I", str(cjson),
        str(main / "hikvision_api.c"), str(cjson / "cJSON.c"),
        str(ROOT / "tests/firmware/hikvision_api_host.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True, timeout=60)

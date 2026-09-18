import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_verified_profile_commands_and_retries(tmp_path):
    idf = Path(os.environ.get('IDF_PATH', Path.home() / 'esp/esp-idf-v5.5.3'))
    cjson = idf / 'components/json/cJSON'
    if not (cjson / 'cJSON.c').is_file():
        pytest.skip('Pinned ESP-IDF cJSON source required')
    (tmp_path / 'esp_http_client.h').write_text('#pragma once\ntypedef enum {HTTP_METHOD_GET,HTTP_METHOD_POST,HTTP_METHOD_PUT} esp_http_client_method_t;\n')
    (tmp_path / 'esp_random.h').write_text('#include <stdint.h>\nuint32_t esp_random(void);\n')
    (tmp_path / 'esp_err.h').write_text('#pragma once\ntypedef int esp_err_t;\n')
    (tmp_path / 'mbedtls').mkdir()
    (tmp_path / 'mbedtls/sha256.h').write_text('#include <stddef.h>\nint mbedtls_sha256(const unsigned char *,size_t,unsigned char [32],int);\n')
    main = ROOT / 'firmware/zone_lite/main'
    exe = tmp_path / 'commands'
    subprocess.run([shutil.which('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-deprecated-declarations', '-fsanitize=address,undefined',
                    '-I', str(tmp_path), '-I', str(main), '-I', str(cjson),
                    str(main / 'hikvision_api.c'), str(main / 'hikvision_commands.c'),
                    str(cjson / 'cJSON.c'), str(ROOT / 'tests/firmware/hikvision_commands_host.c'),
                    '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True, timeout=60)

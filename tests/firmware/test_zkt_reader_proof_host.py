"""Fault durable reader proof and reject unsafe writer/rollback identities."""
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("bridge_version", ["2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23"])
def test_reader_proof_persistence_and_exact_rollback(tmp_path, bridge_version):
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "reader-proof"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", f'-DZJ_BRIDGE_VERSION="{bridge_version}"',
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/zkt_reader_proof_host.c"),
                    str(main / "zkt_journal_compat.c"), str(main / "durable_queue.c"),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)
    # An independent byte fixture prevents both C encode/decode moving offsets
    # together unnoticed between the signed bridge and candidate artifacts.
    expected = bytearray(192)
    expected[:8] = b"ZJREAD01"
    struct.pack_into("<10I", expected, 8, 1, 192, 1, 63, 15, 1, 1, 0x520000, 0x280000, 0)
    expected[48] = 11
    expected[80] = 22
    expected[112] = 33
    expected[128] = 44
    struct.pack_into("<Q", expected, 160, 1)
    expected[168:174] = bridge_version.encode()
    struct.pack_into("<I", expected, 188, zlib.crc32(expected[:188]))
    assert (tmp_path / "reader-proof.bin").read_bytes() == bytes(expected)


@pytest.mark.parametrize("bridge_version", ["2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23"])
def test_platform_facts_and_nvs_failures(tmp_path, bridge_version):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ["esp_app_desc.h", "esp_ota_ops.h", "esp_partition.h",
                   "esp_secure_boot.h", "esp_timer.h", "mbedtls/sha256.h", "nvs.h", "sdkconfig.h"]:
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_reader_platform_host.h"\n')
    for encrypted, anti_rollback, capture in ((1, 0, 1), (0, 0, 1), (1, 1, 1), (1, 0, 0)):
        executable = tmp_path / f"reader-platform-{encrypted}-{anti_rollback}-{capture}"
        subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", f'-DZJ_BRIDGE_VERSION="{bridge_version}"',
                        f"-DCONFIG_NVS_ENCRYPTION={encrypted}",
                        f"-DCONFIG_BOOTLOADER_APP_ANTI_ROLLBACK={anti_rollback}",
                        f"-DZONE_LITE_JOURNAL_WRITES={capture}",
                        "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                        str(fixture / "zkt_reader_platform_host.c"),
                        str(main / "zkt_reader_platform.c"), str(main / "zkt_journal_compat.c"),
                        str(main / "durable_queue.c"), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)

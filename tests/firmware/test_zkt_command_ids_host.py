"""Fault real command receipt files and the retained storage-owner client."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_command_receipts_bounded_reads_replay_and_storage_faults(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    flags = [shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
             "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
             "-I", str(main)]
    obj = tmp_path / "ids.o"
    subprocess.run([*flags, *(f"-D{name}=zi_{name}" for name in
                    ("fopen", "fread", "fwrite", "fflush", "fsync", "fclose", "fstat", "fseek")),
                    "-c", str(main / "zkt_command_id_store.c"), "-o", str(obj)], check=True)
    binary = tmp_path / "command-ids"
    subprocess.run([*flags, str(ROOT / "tests/firmware/zkt_command_id_store_host.c"), str(obj),
                    "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_command_receipt_client_retains_timeouts_and_isolates_ids(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ("esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "command-client"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main),
                    str(fixture / "zkt_command_id_client_host.c"), str(main / "zkt_command_id_client.c"),
                    str(main / "zkt_command_id_store.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_production_command_callers_use_family_specific_receipt_owner(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    gateway = (main / "zone_lite.c").read_text()
    connector = (main / "add_connector.c").read_text()
    definitions = "\n".join(line for source in (gateway, connector) for line in source.splitlines()
                            if line.startswith(tuple(f"#define {name} " for name in (
                                "STORAGE_BASE", "PROCESSED_COMMANDS_PATH", "CANCELLED_COMMANDS_PATH",
                                "COMMAND_RECEIPT_CACHE_BYTES", "COMMAND_ID_MAX_BYTES",
                                "ADD_CANCELLED_COMMANDS_PATH", "ADD_COMMAND_RECEIPT_CACHE_BYTES"))))
    actual = gateway[gateway.index("static rel_id_result_t command_was_processed("):
                     gateway.index("static bool temp_admin_evidence_ready(")]
    actual += connector[connector.index("static bool append_cancelled_command("):
                        connector.index("static bool parse_command_object(")]
    (tmp_path / "command_id_actual.inc").write_text(definitions + "\n" + actual)
    for family in (0, 1):
        binary = tmp_path / f"command-adapter-{family}"
        subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", f"-DZONE_LITE_HIKVISION={family}",
                        "-I", str(tmp_path), "-I", str(main),
                        str(ROOT / "tests/firmware/zkt_command_id_adapter_host.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)

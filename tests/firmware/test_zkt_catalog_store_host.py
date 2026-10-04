from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_owner_tokens_steps_deadlines_and_faults(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    flags = [shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
             "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
             "-I", str(main)]
    obj = tmp_path / "catalog.o"
    subprocess.run([*flags, *(f"-D{name}=zc_{name}" for name in
                    ("fopen", "fwrite", "fflush", "fsync", "fclose")), "-c",
                    str(main / "zkt_catalog_store.c"), "-o", str(obj)], check=True)
    executable = tmp_path / "catalog"
    subprocess.run([*flags, str(ROOT / "tests/firmware/zkt_catalog_store_host.c"), str(obj),
                    str(main / "file_transaction.c"), str(main / "durable_queue.c"),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=60)


def test_catalog_client_retains_timed_out_operation_until_collected(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "catalog-client"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(main), str(ROOT / "tests/firmware/zkt_catalog_client_host.c"),
                    str(main / "zkt_catalog_client.c"), str(main / "zkt_storage_mailbox.c"),
                    str(main / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)

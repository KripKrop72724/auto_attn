from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_file_transaction_steps_bound_reads_and_recover_at_every_boundary(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    flags = [shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
             "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main)]
    source = tmp_path / "file_transaction.o"
    subprocess.run([*flags, *(f"-D{name}=fw_{name}" for name in ("fopen", "fseek", "fread", "fflush", "fsync", "fclose", "rename", "remove")),
                    "-c", str(main / "file_transaction.c"), "-o", str(source)], check=True)
    executable = tmp_path / "file-transactions"
    subprocess.run([*flags, str(ROOT / "tests/firmware/file_transaction_steps_host.c"), str(source),
                    str(main / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=60)

"""Run actual shared health reporting and legacy I/O error capture."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_scoped_legacy_incidents_clear_only_on_the_affected_lane_and_operation(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "queue_store.c").read_text()
    actual = source[source.index("static void restart_verification("):source.index("bool qs_local_admit_locked(")]
    actual += source[source.index("void qs_local_end_legacy("):source.index("static bool lock(")]
    actual += source[source.index("qs_health_t qs_local_health_locked("):]
    (tmp_path / "legacy_health_actual.inc").write_text(actual)
    binary = tmp_path / "health"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(tmp_path),
                    "-I", str(main), str(ROOT / "tests/firmware/legacy_storage_health_host.c"),
                    str(main / "legacy_storage_health.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_legacy_io_failure_keeps_the_first_error_and_is_not_a_stale_token(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    binary = tmp_path / "errors"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(ROOT / "tests/firmware/legacy_queue_error_host.c"),
                    str(main / "durable_queue.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)

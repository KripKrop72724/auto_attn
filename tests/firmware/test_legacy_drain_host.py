"""Exercise the production read/send/commit orchestration with faulting ports."""
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_actual_legacy_drain_preserves_records_and_releases_lock(tmp_path: Path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    start = source.index("static void oracle_drain_pending(bool live_first)")
    end = source.index("static void ords_uploader_task(", start)
    harness = (ROOT / "tests/firmware/legacy_drain_host.c").read_text()
    unit = tmp_path / "drain.c"
    unit.write_text(harness.replace("/* INSERT_PRODUCTION_DRAIN */", source[start:end]))
    compiler = shutil.which("cc")
    assert compiler
    executable = tmp_path / "drain"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "legacy_queue.c"), str(firmware / "durable_queue.c"),
        "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)

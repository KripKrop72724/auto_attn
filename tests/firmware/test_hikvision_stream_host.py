from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_hikvision_stream_memory_and_fragmentation(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "hikvision-stream"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-g", "-I", str(main),
        str(main / "hikvision_stream.c"),
        str(ROOT / "tests/firmware/hikvision_stream_host.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)

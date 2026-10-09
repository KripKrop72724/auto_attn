"""Complete a stopped SPIFFS removal against ESP-IDF's own SPIFFS on the host."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
SPIFFS = Path(os.environ["IDF_PATH"]) / "components/spiffs/spiffs/src"
SOURCES = ["spiffs_nucleus.c", "spiffs_gc.c", "spiffs_hydrogen.c", "spiffs_cache.c", "spiffs_check.c"]

with tempfile.TemporaryDirectory(prefix="spiffs-orphan-") as temporary:
    binary = Path(temporary) / "spiffs-orphan"
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-fsanitize=address,undefined",
             "-fno-omit-frame-pointer"]
    objects = []
    for source in SOURCES:
        # Upstream SPIFFS is compiled as ESP-IDF compiles it, warnings relaxed.
        output = Path(temporary) / (source + ".o")
        # Its index tables are unaligned by design (SPIFFS_ALIGNED_OBJECT_INDEX_TABLES 0).
        subprocess.run([shutil.which("cc"), *flags, "-fno-sanitize=alignment", "-w",
                        "-I", str(ROOT / "tests/firmware/spiffs_host"),
                        "-I", str(SPIFFS), "-c", str(SPIFFS / source), "-o", str(output)], check=True)
        objects.append(str(output))
    subprocess.run([shutil.which("cc"), *flags, "-Wall", "-Wextra", "-Werror",
                    "-I", str(ROOT / "tests/firmware/spiffs_host"), "-I", str(SPIFFS), "-I", str(MAIN),
                    str(ROOT / "tests/firmware/spiffs_orphan_host.c"), str(MAIN / "storage_orphan_core.c"),
                    str(MAIN / "storage_recovery_core.c"), str(MAIN / "reliability.c"), *objects,
                    "-o", str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "spiffs orphan tests passed" in result.stdout
print("SPIFFS orphan completion verified against ESP-IDF SPIFFS")

"""Unknown/partial legacy custody inventory cannot be reported as empty."""
from pathlib import Path
import shutil
import subprocess


def test_complete_legacy_inventory_and_fault_invalidation(tmp_path):
    root = Path(__file__).resolve().parents[2]
    binary = tmp_path / "legacy-inventory"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(root / "firmware/zone_lite/main"),
        str(root / "tests/firmware/zkt_legacy_inventory_host.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)

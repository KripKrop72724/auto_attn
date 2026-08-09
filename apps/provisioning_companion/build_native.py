from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).parent / "src" / "add_provisioning_companion" / "app.py"
TOOL_ENTRY = Path(__file__).parent / "tool_entry.py"
TOOLS = ROOT / "firmware" / "zone_lite" / "tools"


def run(*arguments: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", *arguments], check=True)


def main() -> None:
    output = Path(__file__).parent / "dist"
    build = Path(__file__).parent / "build"
    for path in (output, build):
        if path.exists():
            shutil.rmtree(path)
    sidecars = []
    for executable in ("esptool", "espefuse"):
        if not (shutil.which(executable) or shutil.which(f"{executable}.py")):
            raise RuntimeError(f"Pinned {executable} entry point is unavailable")
        run(
            "--onefile",
            "--name",
            executable,
            "--distpath",
            str(output / "sidecars"),
            "--workpath",
            str(build / executable),
            str(TOOL_ENTRY),
        )
        sidecars.append(output / "sidecars" / f"{executable}{'.exe' if os.name == 'nt' else ''}")
    run(
        "--windowed",
        "--onedir",
        "--name",
        "ADD Provisioning Companion",
        "--paths",
        str(Path(__file__).parent / "src"),
        "--add-data",
        f"{TOOLS}{os.pathsep}firmware_tools",
        "--distpath",
        str(output),
        "--workpath",
        str(build / "companion"),
        str(SOURCE),
    )
    if sys.platform == "darwin":
        destination = output / "ADD Provisioning Companion.app" / "Contents" / "MacOS"
    else:
        destination = output / "ADD Provisioning Companion"
    destination.mkdir(parents=True, exist_ok=True)
    for sidecar in sidecars:
        shutil.copy2(sidecar, destination / sidecar.name)


if __name__ == "__main__":
    main()

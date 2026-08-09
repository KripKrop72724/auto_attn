"""PyInstaller entry point for the pinned Espressif command sidecars."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    executable = Path(sys.executable).stem.lower()
    if "espefuse" in executable:
        from espefuse import main as tool_main
    else:
        from esptool import main as tool_main
    tool_main()


if __name__ == "__main__":
    main()

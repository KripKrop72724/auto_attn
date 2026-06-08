from __future__ import annotations

import os
from pathlib import Path

from zk_hr_enrollment import BRAND_NAME


DEFAULT_COMM_KEY = 1979
SECRET_RELATIVE_PATH = Path(BRAND_NAME) / "HR Enrollment" / "secrets" / "comm_key.txt"


class CommKeyConfigError(ValueError):
    """Raised when the hidden comm-key override file is present but invalid."""


def comm_key_secret_path() -> Path:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return program_data / SECRET_RELATIVE_PATH


def read_comm_key(secret_path: Path | None = None) -> int:
    path = secret_path or comm_key_secret_path()
    if not path.exists():
        return DEFAULT_COMM_KEY
    value = path.read_text(encoding="utf-8").strip()
    try:
        key = int(value)
    except ValueError as exc:
        raise CommKeyConfigError(
            f"The hidden comm-key file is invalid. Ask IT to fix {path}."
        ) from exc
    if key < 0:
        raise CommKeyConfigError(
            f"The hidden comm-key file must contain a non-negative integer. Ask IT to fix {path}."
        )
    return key


from __future__ import annotations

import os
from pathlib import Path

from zk_hr_enrollment import BRAND_NAME


DEFAULT_COMM_KEY = 1979
SECRET_RELATIVE_PATH = Path(BRAND_NAME) / "HR Enrollment" / "secrets" / "comm_key.txt"


class CommKeyConfigError(ValueError):
    """Kept for compatibility with older builds that supported comm-key overrides."""


def comm_key_secret_path() -> Path:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return program_data / SECRET_RELATIVE_PATH


def read_comm_key(secret_path: Path | None = None) -> int:
    """Return the fixed State Life ZKT comm key.

    ``secret_path`` is accepted for compatibility with older builds, but hidden
    overrides are intentionally ignored so this HR app always uses 1979.
    """
    _ = secret_path
    return DEFAULT_COMM_KEY

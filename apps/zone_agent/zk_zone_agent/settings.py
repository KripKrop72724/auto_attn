from __future__ import annotations

import os
import platform
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "ZKZoneAgent"
    return Path.cwd() / "local-data" / "zone-agent"


class ZoneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZK_ZONE_", env_file=".env", extra="ignore")

    data_dir: Path = default_data_dir()
    database_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 7860
    disable_workers: bool = False
    scan_timeout_seconds: float = 0.45
    scan_concurrency: int = 128
    clock_check_interval_seconds: int = 5
    drift_threshold_seconds: int = 120
    jump_threshold_seconds: int = 15
    critical_drift_seconds: int = 300
    time_sync_interval_seconds: int = 30
    heartbeat_interval_seconds: int = 15

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "zone-agent.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.sqlite_path}"


settings = ZoneSettings()

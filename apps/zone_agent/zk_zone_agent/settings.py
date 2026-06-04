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
    default_timezone: str = "Asia/Karachi"
    production_head_office_url: str = "https://head-office-production.up.railway.app"
    default_ords_base_url: str = "https://eclaim2.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
    allow_dev_head_office_urls: bool = False
    disable_workers: bool = False
    auto_discovery_enabled: bool = True
    auto_discovery_interval_seconds: int = 300
    auto_discovery_startup_delay_seconds: int = 5
    manual_rescan_min_interval_seconds: int = 10
    scan_port: int = 4370
    scan_timeout_seconds: float = 0.45
    scan_concurrency: int = 128
    scan_max_hosts_per_subnet: int = 254
    scan_include_public_subnets: bool = False
    clock_check_interval_seconds: int = 5
    zkt_client_timeout_seconds: float = 10
    device_user_refresh_timeout_seconds: float = 120
    device_user_update_timeout_seconds: float = 180
    bulk_user_update_timeout_seconds: float = 7200
    device_user_io_retry_attempts: int = 3
    device_user_io_retry_delay_seconds: float = 0.75
    live_poll_reconcile_enabled: bool = True
    live_poll_reconcile_interval_seconds: int = 5
    drift_threshold_seconds: int = 120
    jump_threshold_seconds: int = 15
    critical_drift_seconds: int = 300
    time_sync_interval_seconds: int = 30
    heartbeat_interval_seconds: int = 15
    bruteforce_enabled: bool = False
    bruteforce_default_timeout_seconds: float = 0.75
    bruteforce_global_max_workers: int = 0
    bruteforce_safe_fast_workers: int = 1
    bruteforce_aggressive_workers: int = 2
    bruteforce_hard_per_device_workers: int = 8
    bruteforce_chunk_size: int = 25

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "zone-agent.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.sqlite_path}"


settings = ZoneSettings()

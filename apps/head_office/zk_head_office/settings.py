from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class HeadOfficeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZK_HEAD_", env_file=".env", extra="ignore")

    data_dir: Path = Path.cwd() / "local-data" / "head-office"
    database_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080
    enrollment_key: str = "ABC-123"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "head-office.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.sqlite_path}"


settings = HeadOfficeSettings()

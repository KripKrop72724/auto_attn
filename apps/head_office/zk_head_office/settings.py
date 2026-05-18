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
    allow_legacy_registration: bool = False
    require_admin_auth: bool = False
    admin_password_hash: str | None = None
    session_secret: str | None = None
    admin_cookie_secure: bool = False
    display_timezone: str = "Asia/Karachi"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "head-office.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return normalize_database_url(self.database_url)
        return f"sqlite:///{self.sqlite_path}"


def normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


settings = HeadOfficeSettings()

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AddSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADD_", env_file=".env.add", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8096
    data_dir: Path = Path.cwd() / "local-data" / "add"
    database_url: str | None = None
    redis_url: str = "redis://redis:6379/0"
    auto_create_schema: bool = False

    admin_username: str = "StateHealthAdmin"
    admin_password_hash: str | None = None
    admin_cookie_secure: bool = True
    admin_session_idle_seconds: int = 30 * 60
    admin_session_absolute_seconds: int = 8 * 60 * 60
    admin_step_up_seconds: int = 5 * 60

    pii_fernet_key: str | None = None
    pii_lookup_key: str | None = None

    heartbeat_interval_seconds: int = 15
    offline_after_seconds: int = 45
    connector_command_poll_seconds: int = 3
    reconcile_interval_seconds: int = 15 * 60
    user_integrity_interval_seconds: int = 6 * 60 * 60
    log_retention_days: int = 14
    telemetry_retention_days: int = 30
    session_retention_days: int = 90

    ords_base_url: str | None = None
    ords_username: str | None = None
    ords_password: str | None = None
    ords_timeout_seconds: int = 20

    bootstrap_connector_id: str | None = None
    bootstrap_connector_token: str | None = None
    bootstrap_hardware_id: str | None = None
    bootstrap_zone_id: str = "ZONE-SLICTOWER-3FL"
    bootstrap_zone_name: str = "ZONE-SLICTOWER-3FL"
    bootstrap_device_id: str = "1"
    bootstrap_expected_serial: str | None = None

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "add.db"

    @property
    def resolved_database_url(self) -> str:
        value = self.database_url or f"sqlite:///{self.sqlite_path}"
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    def require_production_secrets(self) -> None:
        missing = []
        if not self.admin_password_hash:
            missing.append("ADD_ADMIN_PASSWORD_HASH")
        if not self.pii_fernet_key:
            missing.append("ADD_PII_FERNET_KEY")
        if not self.pii_lookup_key:
            missing.append("ADD_PII_LOOKUP_KEY")
        if missing:
            raise RuntimeError(f"Missing required ADD secrets: {', '.join(missing)}")


settings = AddSettings()

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CHECKIN_NOTIFY_PATH = "/ords/slic_hrm/ESS_APIS/attendance_checkin_notify"
# Public ORDS host, reachable from outside the organisation network.
CHECKIN_NOTIFY_FALLBACK_URL = "https://eclaim2.slichealth.com" + CHECKIN_NOTIFY_PATH


class AddSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADD_", env_file=".env.add", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8096
    data_dir: Path = Path.cwd() / "local-data" / "add"
    database_url: str | None = None
    database_pool_size: int = Field(default=20, ge=5, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=100)
    database_pool_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_pool_recycle_seconds: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    database_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=5 * 60 * 1000)
    database_lock_timeout_ms: int = Field(default=5_000, ge=500, le=60_000)
    redis_url: str = "redis://redis:6379/0"
    auto_create_schema: bool = False

    admin_username: str = "StateHealthAdmin"
    admin_password_hash: str | None = None
    admin_cookie_secure: bool = True
    admin_session_idle_seconds: int = 30 * 60
    admin_session_absolute_seconds: int = 8 * 60 * 60
    admin_step_up_seconds: int = 5 * 60
    slow_request_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    event_loop_lag_warning_seconds: float = Field(default=1.0, ge=0.1, le=30.0)

    pii_fernet_key: str | None = None
    pii_lookup_key: str | None = None
    fleet_root_secret: str | None = None
    onboarding_signature_skew_seconds: int = 300
    onboarding_token_overlap_seconds: int = 10 * 60
    public_device_ws_url: str = "wss://autoattn.slichealth.com/device/v2/stream"

    heartbeat_interval_seconds: int = 15
    offline_after_seconds: int = 45
    offline_startup_grace_seconds: int = Field(default=90, ge=0, le=15 * 60)
    esp_offline_alert_after_seconds: int = Field(default=120, ge=45, le=60 * 60)
    esp_offline_alert_window_seconds: int = Field(default=600, ge=60, le=24 * 60 * 60)
    # Derived device health (zk_add/device_health.py). The owner enforces the
    # derived lifecycle on deploy; false is the kill switch that hands
    # lifecycle and last_error back to the legacy writers on the next heartbeat.
    device_health_derived_enabled: bool = True
    journal_startup_grace_seconds: int = Field(default=600, ge=60, le=60 * 60)
    terminal_disconnected_warning_seconds: int = Field(default=300, ge=60, le=60 * 60)
    terminal_stabilizing_warning_seconds: int = Field(default=600, ge=60, le=60 * 60)
    terminal_maintenance_budget_seconds: int = Field(default=600, ge=60, le=60 * 60)
    terminal_link_down_alert_seconds: int = Field(default=900, ge=300, le=24 * 60 * 60)
    message_rejection_current_seconds: int = Field(default=300, ge=30, le=60 * 60)
    message_failure_warning_count: int = Field(default=3, ge=1, le=100)
    message_failure_warning_seconds: int = Field(default=300, ge=30, le=60 * 60)
    heartbeat_failure_degraded_seconds: int = Field(default=120, ge=30, le=60 * 60)
    heartbeat_stale_degraded_seconds: int = Field(default=90, ge=45, le=60 * 60)
    led_boot_time_hold_uptime_seconds: int = Field(default=180, ge=30, le=60 * 60)
    restart_loop_window_seconds: int = Field(default=1800, ge=300, le=24 * 60 * 60)
    restart_loop_boots: int = Field(default=4, ge=3, le=50)
    # A 2.6.15 LED latch from these lock-contention sources, with zero I/O
    # errors and verified storage in the same sample, is shown at this tier.
    # The alert stays OPEN, HIGH and gating either way.
    device_health_latched_led_tier: Literal["DEGRADED", "WARNING"] = "WARNING"
    device_health_latched_led_sources: str = "2.6.15:add_connector.c:3764,2.6.15:add_connector.c:3787"
    # The stranded-alert cleanup still needs a signed preview, a password
    # step-up and a typed confirmation; false disables its apply step.
    device_health_cleanup_apply_enabled: bool = True
    device_health_cleanup_preview_seconds: int = Field(default=900, ge=60, le=900)
    connector_command_poll_seconds: int = 3
    reconcile_interval_seconds: int = 15 * 60
    user_integrity_interval_seconds: int = 30
    identity_snapshot_gate_enabled: bool = False
    identity_snapshot_capture_tolerance_seconds: int = 5
    # A legacy reconnect/startup dump row without a saved terminal record can
    # be released on its authenticated batch and retained CNIC interval
    # (AUTHENTICATED_RECONCILE_BATCH). This is the kill switch: false
    # withdraws that proof everywhere. No automatic release, running owner
    # releases pause, no new one can be approved, and the pre-send check
    # holds rows it released that Oracle has not yet acknowledged.
    attendance_dump_identity_release_enabled: bool = True
    # Automatic release at a later roster snapshot runs only for these zone
    # IDs (comma-separated; "*" for every zone; empty: none, the default).
    # It admits a punch only when both the punch and ADD's hold of it are
    # within the window (0 hours: off) and ADD's complete roster snapshots
    # just before and after the punch are at most the gap apart. At most the
    # batch cap per connector per snapshot, and the hourly cap per connector
    # in any hour. Everything else needs the owner's audited backlog
    # release, which uses the same proof.
    # Owner decision (22 Oct 2026): automatic release in every zone.
    attendance_dump_identity_auto_release_zones: str = "*"
    attendance_dump_identity_auto_release_hours: int = Field(default=72, ge=0, le=14 * 24)
    attendance_dump_identity_auto_release_batch: int = Field(default=100, ge=0, le=500)
    attendance_dump_identity_auto_release_hourly_cap: int = Field(default=200, ge=0, le=5000)
    attendance_dump_identity_auto_release_max_snapshot_gap_minutes: int = Field(
        default=60, ge=1, le=24 * 60
    )
    # The owner's backlog release locks and checks this many rows per worker
    # transaction, and queues no more while this many of its released rows
    # still wait for Oracle.
    attendance_identity_backlog_batch_size: int = Field(default=25, ge=1, le=200)
    attendance_identity_backlog_in_flight_limit: int = Field(default=100, ge=1, le=1000)
    # Held full-history, dump and 15-minute reconcile rows a source chunk
    # links to their saved record are released in the chunk's transaction,
    # at most this many per chunk (0: none); the next roster snapshot
    # releases the rest. Current-reconcile rows are always released there.
    attendance_chunk_history_release_limit: int = Field(default=25, ge=0, le=100)
    identity_repair_sla_seconds: int = 60
    reconciliation_enabled: bool = False
    reconciliation_self_healing_enabled: bool = False
    reconciliation_device_concurrency: int = Field(default=6, ge=1, le=6)
    reconciliation_chunk_records: int = 100
    reconciliation_history_backlog_pause: int = 10_000
    reconciliation_history_backlog_resume: int = 5_000
    reconciliation_assignment_seconds: int = 5
    reconciliation_v2_assignment_seconds: int = Field(default=180, ge=30, le=600)
    reconciliation_v2_credit_records: int = Field(default=400, ge=100, le=2000)
    reconciliation_v2_max_chunks: int = Field(default=4, ge=1, le=20)
    reconciliation_stale_seconds: int = 45
    # Employee attendance repair is deliberately split into preview and
    # execution gates.  Production can validate frozen membership/evidence
    # before the first Oracle mutation is possible.
    attendance_repair_preview_enabled: bool = False
    attendance_repair_execution_enabled: bool = False
    attendance_repair_legacy_admission_enabled: bool = False
    attendance_repair_preview_seconds: int = Field(default=15 * 60, ge=60, le=15 * 60)
    attendance_repair_max_employees: int = Field(default=500, ge=1, le=500)
    attendance_repair_max_events: int = Field(default=250_000, ge=1, le=250_000)
    attendance_repair_oracle_batch_size: int = Field(default=100, ge=1, le=100)
    attendance_repair_oracle_concurrency: int = Field(default=2, ge=1, le=2)
    attendance_repair_lease_seconds: int = Field(default=90, ge=30, le=10 * 60)
    attendance_repair_retry_limit: int = Field(default=12, ge=1, le=50)
    attendance_repair_source_max_age_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    attendance_repair_ords_username: str | None = None
    attendance_repair_ords_password: str | None = None
    # Recovery is independently dark-launched.  Read-only previews can be
    # enabled before any batch is permitted to change delivery state.
    attendance_safe_repair_preview_enabled: bool = False
    attendance_safe_repair_execution_enabled: bool = False
    attendance_safe_repair_allowed_connectors: str = ""
    attendance_safe_repair_batch_size: int = Field(default=100, ge=1, le=100)
    attendance_force_release_preview_enabled: bool = False
    attendance_force_release_execution_enabled: bool = False
    attendance_force_release_allowed_connectors: str = ""
    attendance_force_release_allowed_families: str = "zkt"
    attendance_recovery_preview_enabled: bool = False
    attendance_recovery_execution_enabled: bool = False
    attendance_source_correction_enabled: bool = False
    attendance_recovery_allowed_zones: str = ""
    attendance_recovery_preview_seconds: int = Field(default=15 * 60, ge=60, le=15 * 60)
    attendance_recovery_batch_size: int = Field(default=100, ge=1, le=500)
    attendance_recovery_max_items: int = Field(default=5_000, ge=1, le=50_000)
    attendance_recovery_max_correction_items: int = Field(default=500, ge=1, le=5_000)
    attendance_recovery_lease_seconds: int = Field(default=90, ge=30, le=10 * 60)
    log_retention_days: int = 14
    telemetry_retention_days: int = 30
    session_retention_days: int = 90

    ords_base_url: str | None = None
    ords_username: str | None = None
    ords_password: str | None = None
    ords_timeout_seconds: int = 20
    ords_delivery_batch_size: int = 100
    ords_delivery_concurrency: int = 8
    ords_firmware_audit_batch_size: int = 500
    ords_membership_reverify_seconds: int = 24 * 60 * 60

    # Employee "attendance punched" phone notification (HRM ORDS service),
    # sent only after Oracle confirms the punch. Nothing is sent without the
    # API key (NOT_CONFIGURED). The live mode (OFF / PILOT / ALL), punch policy
    # and pilot allowlist are stored in the database and changed by an
    # administrator at runtime; ADD_CHECKIN_NOTIFY_MODE, _PUNCH_POLICY and
    # _PILOT_ZONE_IDS only seed that row the first time the feature runs. The
    # owner's rollout is ALL zones at once, so ALL is the default.
    # The URL defaults to the host of ADD_ORDS_BASE_URL (the internal ORDS
    # host) plus CHECKIN_NOTIFY_PATH. The fallback (public) host is used only
    # when an attempt on the other host provably never reached a server.
    # See docs/checkin-notifications.md.
    checkin_notify_url: str | None = None
    checkin_notify_fallback_url: str | None = CHECKIN_NOTIFY_FALLBACK_URL
    checkin_notify_api_key: str | None = None
    checkin_notify_cnic_json_type: Literal["number", "string"] = "number"
    checkin_notify_send_checkin_time: bool = True
    checkin_notify_mode: Literal["OFF", "PILOT", "ALL"] = "ALL"
    checkin_notify_punch_policy: Literal["ALL_PUNCHES", "FIRST_PER_DAY"] = "ALL_PUNCHES"
    checkin_notify_pilot_zone_ids: str = ""
    checkin_notify_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    checkin_notify_connect_timeout_seconds: float = Field(default=5.0, ge=0.5, le=30.0)
    checkin_notify_primary_reprobe_seconds: int = Field(default=600, ge=30, le=24 * 60 * 60)
    checkin_notify_stale_window_hours: float = Field(default=12.0, ge=0.25, le=72.0)
    checkin_notify_future_tolerance_seconds: int = Field(default=300, ge=0, le=3600)
    checkin_notify_coalesce_seconds: int = Field(default=120, ge=0, le=3600)
    checkin_notify_retry_window_hours: float = Field(default=6.0, ge=0.05, le=48.0)
    checkin_notify_backoff_initial_seconds: int = Field(default=15, ge=1, le=600)
    checkin_notify_backoff_max_seconds: int = Field(default=1800, ge=5, le=6 * 60 * 60)
    checkin_notify_batch_size: int = Field(default=25, ge=1, le=200)
    checkin_notify_concurrency: int = Field(default=4, ge=1, le=16)
    checkin_notify_rate_per_second: float = Field(default=5.0, ge=0.1, le=50.0)
    checkin_notify_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    checkin_notify_circuit_pause_seconds: int = Field(default=60, ge=1, le=60 * 60)
    checkin_notify_circuit_max_pause_seconds: int = Field(default=900, ge=1, le=6 * 60 * 60)
    checkin_notify_backlog_alert_seconds: int = Field(default=600, ge=60, le=24 * 60 * 60)
    checkin_notify_failure_alert_count: int = Field(default=3, ge=1, le=1000)

    user_command_retry_seconds: int = 30 * 60
    command_redispatch_seconds: int = 20
    firmware_ota_enabled: bool = False
    firmware_hil_enabled: bool = False
    firmware_hil_target_mac: str | None = None
    firmware_hikvision_hil_target_mac: str | None = None
    firmware_hil_targets_json: str | None = None
    firmware_store_path: str = "/firmware"
    firmware_public_base_url: str = "https://autoattn.slichealth.com"
    firmware_download_grant_seconds: int = 15 * 60
    # Kill switch for resuming a stalled 2.6.0-2.6.15 download inside the same
    # deployment (bounded backoff, at most three attempts). Off: every such
    # stop fails the deployment closed and pauses the campaign, as before.
    # The resume has not run on a device yet; enable it for a HIL first.
    firmware_download_resume_enabled: bool = False
    firmware_signing_public_key_pem_b64: str | None = None
    comm_key_management_enabled: bool = False
    comm_key_reveal_enabled: bool = False
    comm_key_secret_fernet_key: str | None = None
    comm_key_operation_seconds: int = Field(
        default=7 * 24 * 60 * 60, ge=900, le=7 * 24 * 60 * 60
    )

    # Physical ESP32-S3 preparation is deliberately independent from OTA. It
    # stays dark until the companion, factory catalog and protected worker are
    # all configured. Secrets in this section never have a fallback.
    provisioning_enabled: bool = False
    provisioning_pairing_secret: str | None = None
    provisioning_internal_token: str | None = None
    provisioning_artifact_path: str = "/provisioning-artifacts"
    provisioning_worker_url: str = "http://add-provisioner:8097"
    provisioning_session_seconds: int = 30 * 60
    provisioning_pairing_seconds: int = 5 * 60
    provisioning_artifact_seconds: int = 15 * 60
    provisioning_companion_min_version: str = "1.0.0"
    provisioning_companion_release_path: str = "/companion-releases"
    provisioning_companion_release_public_key_b64: str | None = None
    provisioning_factory_store_path: str = "/factory-firmware"
    provisioning_public_ws_url: str = "wss://autoattn.slichealth.com/companion/v1/stream"
    provisioning_hardware_profile: str = "esp32s3-16mb-zone-lite-v1"

    @property
    def resolved_checkin_notify_url(self) -> str | None:
        """Explicit URL, else the ORDS host (scheme and authority only) + path."""
        explicit = (self.checkin_notify_url or "").strip()
        if explicit:
            return explicit
        parts = urlsplit((self.ords_base_url or "").strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}{CHECKIN_NOTIFY_PATH}"

    @property
    def resolved_checkin_notify_fallback_url(self) -> str | None:
        """The fallback URL, or None when unset, invalid or the same as the primary."""
        value = (self.checkin_notify_fallback_url or "").strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return None
        return None if value == self.resolved_checkin_notify_url else value

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
        if self.provisioning_enabled:
            if not self.fleet_root_secret:
                missing.append("ADD_FLEET_ROOT_SECRET")
            if not self.provisioning_pairing_secret:
                missing.append("ADD_PROVISIONING_PAIRING_SECRET")
            if not self.provisioning_internal_token:
                missing.append("ADD_PROVISIONING_INTERNAL_TOKEN")
            if not self.provisioning_companion_release_public_key_b64:
                missing.append("ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64")
            if not self.firmware_signing_public_key_pem_b64:
                missing.append("ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64")
        if self.comm_key_management_enabled or self.comm_key_reveal_enabled:
            if not self.comm_key_secret_fernet_key:
                missing.append("ADD_COMM_KEY_SECRET_FERNET_KEY")
            if not self.fleet_root_secret:
                missing.append("ADD_FLEET_ROOT_SECRET")
        if self.attendance_repair_execution_enabled and not self.attendance_repair_preview_enabled:
            raise RuntimeError(
                "ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED requires "
                "ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED."
            )
        if self.attendance_safe_repair_execution_enabled and not self.attendance_safe_repair_preview_enabled:
            raise RuntimeError("Safe attendance repair execution requires repair checks.")
        if self.attendance_force_release_execution_enabled and not self.attendance_force_release_preview_enabled:
            raise RuntimeError("Manual force release requires sync and checks to be enabled.")
        if self.attendance_recovery_execution_enabled and not self.attendance_recovery_preview_enabled:
            raise RuntimeError(
                "ADD_ATTENDANCE_RECOVERY_EXECUTION_ENABLED requires "
                "ADD_ATTENDANCE_RECOVERY_PREVIEW_ENABLED."
            )
        if self.attendance_source_correction_enabled and not self.attendance_recovery_execution_enabled:
            raise RuntimeError(
                "ADD_ATTENDANCE_SOURCE_CORRECTION_ENABLED requires "
                "ADD_ATTENDANCE_RECOVERY_EXECUTION_ENABLED."
            )
        if self.attendance_recovery_preview_enabled and self.attendance_recovery_lease_seconds < self.ords_timeout_seconds * 2:
            raise RuntimeError(
                "ADD_ATTENDANCE_RECOVERY_LEASE_SECONDS must be at least twice "
                "ADD_ORDS_TIMEOUT_SECONDS."
            )
        if self.attendance_repair_preview_enabled:
            if self.attendance_repair_lease_seconds < self.ords_timeout_seconds * 2:
                raise RuntimeError(
                    "ADD_ATTENDANCE_REPAIR_LEASE_SECONDS must be at least twice "
                    "ADD_ORDS_TIMEOUT_SECONDS."
                )
            if not self.ords_base_url:
                missing.append("ADD_ORDS_BASE_URL")
            if not self.attendance_repair_ords_username:
                missing.append("ADD_ATTENDANCE_REPAIR_ORDS_USERNAME")
            if not self.attendance_repair_ords_password:
                missing.append("ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD")
            if (
                self.attendance_repair_ords_username
                and self.attendance_repair_ords_password
                and self.attendance_repair_ords_username == self.ords_username
                and self.attendance_repair_ords_password == self.ords_password
            ):
                raise RuntimeError(
                    "Employee attendance repair must use a dedicated Oracle "
                    "credential, not the connector/fleet credential."
                )
        if missing:
            raise RuntimeError(f"Missing required ADD secrets: {', '.join(missing)}")

    @property
    def effective_fleet_root_secret(self) -> str:
        # Existing production can migrate without a flag day. New deployments
        # must set ADD_FLEET_ROOT_SECRET explicitly; the lookup key fallback is
        # retained only to bind the already-flashed connector during rollout.
        value = self.fleet_root_secret or self.pii_lookup_key
        if not value:
            raise RuntimeError("ADD_FLEET_ROOT_SECRET is required for ESP onboarding.")
        return value

    @property
    def provisioning_fleet_root_secret(self) -> str:
        """Return the explicit root allowed to create new device packages."""
        if not self.provisioning_enabled or not self.fleet_root_secret:
            raise RuntimeError(
                "Physical provisioning is disabled or ADD_FLEET_ROOT_SECRET is not explicit."
            )
        return self.fleet_root_secret


settings = AddSettings()

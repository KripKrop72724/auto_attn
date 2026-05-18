from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

from zk_common.time_utils import ensure_utc, utc_now
from zk_zone_agent.settings import settings

T = TypeVar("T")
SQLITE_BUSY_TIMEOUT_MS = 30_000
SQLITE_LOCK_RETRY_ATTEMPTS = 4
SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS = 0.25
SQLITE_LOCK_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)

    def process_result_value(self, value: datetime | str | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_utc(value)


class Base(DeclarativeBase):
    pass


def utc_column() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class ZoneConfig(Base):
    __tablename__ = "zone_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    zone_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    zone_name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Karachi", nullable=False)
    head_office_url: Mapped[str] = mapped_column(String(500), nullable=False)
    zone_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    setup_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class LocalAdmin(Base):
    __tablename__ = "local_admin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    session_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class AdminWebAuthnCredential(Base):
    __tablename__ = "admin_webauthn_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(Integer, ForeignKey("local_admin.id"), default=1, nullable=False)
    credential_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    aaguid: Mapped[str | None] = mapped_column(String(80))
    credential_device_type: Mapped[str | None] = mapped_column(String(80))
    credential_backed_up: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AdminWebAuthnChallenge(Base):
    __tablename__ = "admin_webauthn_challenges"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("local_admin.id"))
    purpose: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    challenge: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = utc_column()


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=4370, nullable=False)
    comm_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    serial: Mapped[str | None] = mapped_column(String(120), index=True)
    platform: Mapped[str | None] = mapped_column(String(120))
    device_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_clock_status: Mapped[str | None] = mapped_column(String(60))
    last_drift_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class DeviceDiscoveryResult(Base):
    __tablename__ = "device_discovery_results"
    __table_args__ = (UniqueConstraint("ip", "port", name="uq_device_discovery_ip_port"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=4370, index=True, nullable=False)
    subnet: Mapped[str | None] = mapped_column(String(80), index=True)
    interface_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(60), index=True, default="NEEDS_COMM_KEY")
    source: Mapped[str] = mapped_column(String(60), index=True, default="AUTO")
    first_seen: Mapped[datetime] = utc_column()
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    serial: Mapped[str | None] = mapped_column(String(120), index=True)
    platform: Mapped[str | None] = mapped_column(String(120))
    device_name: Mapped[str | None] = mapped_column(String(255))
    configured_device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    updated_at: Mapped[datetime] = utc_column()


class DiscoveryScanRun(Base):
    __tablename__ = "discovery_scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(60), index=True, default="AUTO")
    status: Mapped[str] = mapped_column(String(40), index=True, default="RUNNING")
    started_at: Mapped[datetime] = utc_column()
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    target_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)


class DeviceUser(Base):
    __tablename__ = "device_users"
    __table_args__ = (UniqueConstraint("device_id", "user_id", name="uq_device_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(120), ForeignKey("devices.device_id"))
    uid: Mapped[str | None] = mapped_column(String(100))
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    employee_name: Mapped[str | None] = mapped_column(String(255))
    privilege: Mapped[str | None] = mapped_column(String(100))
    raw_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = utc_column()


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    device_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    employee_name: Mapped[str | None] = mapped_column(String(255))
    device_event_time: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    zone_received_wall_time: Mapped[datetime] = mapped_column(UTCDateTime())
    zone_trusted_time: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    head_office_received_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    trust_status: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    punch: Mapped[str | None] = mapped_column(String(60))
    raw_event: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    device_drift_seconds: Mapped[float | None] = mapped_column(Float)
    device_jump_context_id: Mapped[int | None] = mapped_column(Integer)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_reason: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    sync_status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at: Mapped[datetime] = utc_column()

    def raw_event_dict(self) -> dict:
        return json.loads(self.raw_event or "{}")


class ClockCheck(Base):
    __tablename__ = "clock_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    device_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    device_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    trusted_time: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    windows_wall_time: Mapped[datetime] = mapped_column(UTCDateTime())
    monotonic_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    drift_seconds: Mapped[float | None] = mapped_column(Float)
    expected_device_time: Mapped[datetime | None] = mapped_column(UTCDateTime())
    jump_seconds: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    sync_status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at: Mapped[datetime] = utc_column()


class OutagePeriod(Base):
    __tablename__ = "outage_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    outage_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    start_reason: Mapped[str | None] = mapped_column(Text)
    end_reason: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(120))
    sync_status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at: Mapped[datetime] = utc_column()


class FraudIncident(Base):
    __tablename__ = "fraud_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    incident_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    related_event_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    related_outage_id: Mapped[int | None] = mapped_column(Integer)
    sync_status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at: Mapped[datetime] = utc_column()


class SyncQueue(Base):
    __tablename__ = "sync_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    record_id: Mapped[str | None] = mapped_column(String(100), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at: Mapped[datetime] = utc_column()
    acked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class HeadOfficeTimeSync(Base):
    __tablename__ = "head_office_time_sync"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    local_wall_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    monotonic_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = utc_column()


class AuditLedger(Base):
    __tablename__ = "audit_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    record_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = utc_column()


class ServiceEvent(Base):
    __tablename__ = "service_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    clean_shutdown_marker: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = utc_column()


class CommKeyBruteforceJob(Base):
    __tablename__ = "comm_key_bruteforce_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_candidate_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("device_discovery_results.id"))
    ip: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=4370, nullable=False)
    mode: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, default="PENDING")
    range_start: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    range_end: Mapped[int] = mapped_column(Integer, default=999999, nullable=False)
    current_key: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found_key_encrypted: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=0.75, nullable=False)
    common_keys_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    started_at: Mapped[datetime] = utc_column()
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = utc_column()

    def common_keys(self) -> list[int]:
        return [int(item) for item in json.loads(self.common_keys_json or "[]")]


class CommKeyBruteforceAttempt(Base):
    __tablename__ = "comm_key_bruteforce_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("comm_key_bruteforce_jobs.id"), index=True)
    bucket_start: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_end: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, default="FAILED")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


def create_sqlite_engine(database_url: str | None = None) -> Engine:
    database_url = database_url or settings.resolved_database_url
    is_sqlite = database_url.startswith("sqlite")
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
        future=True,
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = create_sqlite_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def init_db(bind: Engine | None = None) -> None:
    Base.metadata.create_all(bind or engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    source = getattr(exc, "orig", None) or exc
    message = str(source).lower()
    return any(fragment in message for fragment in SQLITE_LOCK_MESSAGES)


def run_session_with_retries(
    operation: Callable[[Session], T],
    *,
    attempts: int = SQLITE_LOCK_RETRY_ATTEMPTS,
    base_delay_seconds: float = SQLITE_LOCK_RETRY_BASE_DELAY_SECONDS,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            with session_scope() as session:
                return operation(session)
        except OperationalError as exc:
            if not is_sqlite_lock_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_delay_seconds * (2**attempt))
    raise RuntimeError("SQLite retry loop exited unexpectedly.")

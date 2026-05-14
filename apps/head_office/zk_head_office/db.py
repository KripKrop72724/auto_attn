from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from zk_common.time_utils import utc_now
from zk_head_office.settings import settings


class Base(DeclarativeBase):
    pass


def utc_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    zone_name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_last4: Mapped[str | None] = mapped_column(String(8))
    token_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_server_time_estimate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_queue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class ZoneHeartbeat(Base):
    __tablename__ = "zone_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    zone_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(50), nullable=False)
    server_time_estimate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    devices_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    pending_queue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = utc_column()


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("zone_id", "device_id", name="uq_head_device_zone_device"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    serial: Mapped[str | None] = mapped_column(String(120), index=True)
    label: Mapped[str | None] = mapped_column(String(255))
    online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_clock_status: Mapped[str | None] = mapped_column(String(60))
    last_drift_seconds: Mapped[float | None] = mapped_column(Float)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class DeviceUser(Base):
    __tablename__ = "device_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    employee_name: Mapped[str | None] = mapped_column(String(255))
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
    device_event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    zone_received_wall_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zone_trusted_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    head_office_received_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    zone_claimed_trust_status: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    head_office_final_trust_status: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    punch: Mapped[str | None] = mapped_column(String(60))
    raw_event: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    device_drift_seconds: Mapped[float | None] = mapped_column(Float)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fraud_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()


class ClockCheck(Base):
    __tablename__ = "clock_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    device_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trusted_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    windows_wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    monotonic_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    drift_seconds: Mapped[float | None] = mapped_column(Float)
    expected_device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jump_seconds: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()


class OutagePeriod(Base):
    __tablename__ = "outage_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    outage_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    start_reason: Mapped[str | None] = mapped_column(Text)
    end_reason: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = utc_column()


class FraudIncident(Base):
    __tablename__ = "fraud_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    incident_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    related_event_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    related_outage_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = utc_column()


class SyncBatch(Base):
    __tablename__ = "sync_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    payload_type: Mapped[str] = mapped_column(String(50), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = utc_column()


class SyncNonce(Base):
    __tablename__ = "sync_nonces"
    __table_args__ = (UniqueConstraint("zone_id", "nonce", name="uq_sync_nonce_zone_nonce"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False)
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = utc_column()


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()


class AuditLedgerReceived(Base):
    __tablename__ = "audit_ledger_received"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    record_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    record_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = utc_column()


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    employee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EmployeeDeviceMapping(Base):
    __tablename__ = "employee_device_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    zone_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    device_user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)


def create_sqlite_engine(database_url: str | None = None) -> Engine:
    database_url = database_url or settings.resolved_database_url
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


engine = create_sqlite_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def init_db(bind: Engine | None = None) -> None:
    target = bind or engine
    Base.metadata.create_all(target)
    _ensure_sqlite_schema(target)


def _ensure_sqlite_schema(bind: Engine) -> None:
    if bind.dialect.name != "sqlite":
        return
    with bind.begin() as connection:
        zone_columns = {
            row["name"]
            for row in connection.execute(text("PRAGMA table_info(zones)")).mappings()
        }
        additions = {
            "token_last4": "VARCHAR(8)",
            "token_issued_at": "DATETIME",
            "token_revoked_at": "DATETIME",
            "token_last_used_at": "DATETIME",
        }
        for column, column_type in additions.items():
            if column not in zone_columns:
                connection.execute(text(f"ALTER TABLE zones ADD COLUMN {column} {column_type}"))


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

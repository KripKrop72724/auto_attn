"""Exercise the production duplicate-CNIC migration against PostgreSQL.

The baseline migration creates current metadata, so ``prepare`` removes the new
conflict column/index after inserting representative legacy rows. Alembic can
then prove that the consolidation migration preserves users and attendance while
quarantining ambiguous CNIC identities.
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from zk_add.models import (
    AttendanceEvent,
    Connector,
    DeviceAlert,
    DeviceUser,
    OrdsOutbox,
    ZKTDevice,
)
from zk_add.time_utils import utc_now


DUPLICATE_LOOKUP = "a" * 64


def database_engine():
    return create_engine(os.environ["ADD_DATABASE_URL"])


def reset() -> None:
    engine = database_engine()
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def prepare() -> None:
    engine = database_engine()
    now = utc_now()
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS uq_add_open_alert_connector_code"))
    with Session(engine) as session:
        connector = Connector(
            connector_id="legacy-duplicate-cnic",
            hardware_id="02:00:00:00:00:01",
            zone_id="MIGRATION-TEST",
            zone_name="Migration Test",
            device_id="1",
            display_name="Migration Test Terminal",
        )
        session.add(connector)
        session.flush()
        session.add_all(
            [
                DeviceAlert(
                    connector_id=connector.id,
                    code="ORDS_DELIVERY_FAILED",
                    severity="WARNING",
                    state="OPEN",
                    message="Legacy retry alert one",
                ),
                DeviceAlert(
                    connector_id=connector.id,
                    code="ORDS_DELIVERY_FAILED",
                    severity="WARNING",
                    state="OPEN",
                    message="Legacy retry alert two",
                ),
            ]
        )
        zkt = ZKTDevice(connector_id=connector.id, serial="MIGRATION-SERIAL")
        session.add(zkt)
        session.flush()
        first = DeviceUser(
            zkt_device_id=zkt.id,
            uid="1",
            user_id="1001",
            machine_name_encrypted="encrypted-machine-one",
            display_name="Legacy One",
            cnic_encrypted="encrypted-cnic-one",
            cnic_lookup_hash=DUPLICATE_LOOKUP,
            cnic_last4="0001",
            identity_conflict_code="DUPLICATE_CNIC",
            lifecycle_state="ACTIVE",
            source="DEVICE_SNAPSHOT",
            observed_at=now,
        )
        second = DeviceUser(
            zkt_device_id=zkt.id,
            uid="2",
            user_id="1002",
            machine_name_encrypted="encrypted-machine-two",
            display_name="Legacy Two",
            cnic_encrypted="encrypted-cnic-two",
            cnic_lookup_hash=DUPLICATE_LOOKUP,
            cnic_last4="0002",
            identity_conflict_code="DUPLICATE_CNIC",
            lifecycle_state="ACTIVE",
            source="DEVICE_SNAPSHOT",
            observed_at=now,
        )
        session.add_all([first, second])
        session.flush()
        attendance = AttendanceEvent(
            event_uid="f" * 64,
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            device_user_id=first.id,
            device_serial=zkt.serial,
            uid=first.uid,
            user_id=first.user_id,
            display_name=first.display_name,
            cnic_encrypted=first.cnic_encrypted,
            cnic_lookup_hash=first.cnic_lookup_hash,
            cnic_last4=first.cnic_last4,
            device_event_time=now,
            captured_at=now,
            source="LIVE",
            raw_event={},
            ords_status="FAILED_RETRYABLE",
        )
        session.add(attendance)
        session.flush()
        session.add(
            OrdsOutbox(
                attendance_event_id=attendance.id,
                status="FAILED_RETRYABLE",
                last_error="legacy response body containing CNIC 3520212345671",
            )
        )
        session.commit()

    with engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS uq_add_user_device_cnic_active"))
        connection.execute(
            text("DROP INDEX IF EXISTS ix_add_device_users_identity_conflict_code")
        )
        connection.execute(
            text("ALTER TABLE add_device_users DROP COLUMN identity_conflict_code")
        )


def verify() -> None:
    engine = database_engine()
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("add_device_users")}
    assert "identity_conflict_code" in columns
    with Session(engine) as session:
        users = session.scalars(
            select(DeviceUser)
            .where(DeviceUser.zkt_device_id == 1)
            .order_by(DeviceUser.uid)
        ).all()
        attendance = session.scalars(select(AttendanceEvent)).all()
        ords_rows = session.scalars(select(OrdsOutbox)).all()
        alerts = session.scalars(
            select(DeviceAlert).where(DeviceAlert.code == "DUPLICATE_USER_CNIC")
        ).all()
        retry_alerts = session.scalars(
            select(DeviceAlert).where(DeviceAlert.code == "ORDS_DELIVERY_FAILED")
        ).all()
        assert len(users) == 2
        assert len(attendance) == 1
        assert len(ords_rows) == 1
        assert len(alerts) == 1
        assert len(retry_alerts) == 2
        assert sum(row.state == "OPEN" for row in retry_alerts) == 1
        assert sum(row.state == "RESOLVED" for row in retry_alerts) == 1
        assert alerts[0].state == "OPEN"
        assert alerts[0].details == {"affected_users": 2}
        assert [row.cnic_encrypted for row in users] == [
            "encrypted-cnic-one",
            "encrypted-cnic-two",
        ]
        assert {row.identity_conflict_code for row in users} == {"DUPLICATE_CNIC"}
        assert attendance[0].device_user_id == users[0].id
        assert attendance[0].ords_status == "FAILED_RETRYABLE"
        assert ords_rows[0].last_error == "LEGACY_ERROR_REDACTED"

    with engine.connect() as connection:
        definition = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexname='uq_add_user_device_cnic_active'"
            )
        )
        alert_definition = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='public' "
                "AND indexname='uq_add_open_alert_connector_code'"
            )
        )
    assert definition is not None
    assert "identity_conflict_code IS NULL" in definition
    assert alert_definition is not None
    assert "WHERE" in alert_definition
    assert "state" in alert_definition
    assert "OPEN" in alert_definition


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("reset", "prepare", "verify"))
    args = parser.parse_args()
    {"reset": reset, "prepare": prepare, "verify": verify}[args.mode]()


if __name__ == "__main__":
    main()

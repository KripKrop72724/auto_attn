"""Consolidate ADD/Zone Lite onboarding, durable users, and protected payloads.

Revision ID: 20260713_0002
Revises: 20260710_0001
Create Date: 2026-07-13

This revision is intentionally idempotent because the original baseline migration
creates the current SQLAlchemy metadata on a fresh installation. Existing production
databases receive the expand/backfill/contract operations below.
"""

from __future__ import annotations

import json
from uuid import uuid4

from alembic import op
from cryptography.fernet import Fernet
import sqlalchemy as sa
from sqlalchemy import inspect, text

from zk_add.models import IdentityTombstone, OnboardingNonce
from zk_add.settings import settings


revision = "20260713_0002"
down_revision = "20260710_0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _constraints(table: str) -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def _indexes(table: str) -> set[str]:
    return {
        item["name"] for item in inspect(op.get_bind()).get_indexes(table) if item.get("name")
    }


def _foreign_keys(table: str) -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_foreign_keys(table)
        if item.get("name")
    }


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _fernet() -> Fernet:
    if not settings.pii_fernet_key:
        raise RuntimeError("ADD_PII_FERNET_KEY is required for the protected-data migration.")
    return Fernet(settings.pii_fernet_key.encode())


def _encrypt_json(fernet: Fernet, value: object) -> str:
    material = json.dumps(value or {}, separators=(",", ":"), sort_keys=True)
    return fernet.encrypt(material.encode()).decode()


def upgrade() -> None:
    bind = op.get_bind()

    _add("add_connectors", sa.Column("onboarding_generation", sa.Integer(), nullable=True))
    _add("add_connectors", sa.Column("last_onboarded_at", sa.DateTime(timezone=True)))
    bind.execute(text("UPDATE add_connectors SET onboarding_generation = 0 WHERE onboarding_generation IS NULL"))
    op.alter_column("add_connectors", "onboarding_generation", nullable=False)

    _add("add_connector_credentials", sa.Column("valid_until", sa.DateTime(timezone=True)))
    if "ix_add_connector_credentials_valid_until" not in _indexes("add_connector_credentials"):
        op.create_index(
            "ix_add_connector_credentials_valid_until",
            "add_connector_credentials",
            ["valid_until"],
        )

    _add("add_zkt_devices", sa.Column("certification_fingerprint", sa.String(255)))
    _add("add_zkt_devices", sa.Column("certification_observations", sa.Integer(), nullable=True))
    _add("add_zkt_devices", sa.Column("snapshot_complete", sa.Boolean(), nullable=True))
    _add("add_zkt_devices", sa.Column("writes_disabled_reason", sa.String(160)))
    bind.execute(
        text(
            "UPDATE add_zkt_devices SET certification_observations = 0 "
            "WHERE certification_observations IS NULL"
        )
    )
    bind.execute(
        text("UPDATE add_zkt_devices SET snapshot_complete = false WHERE snapshot_complete IS NULL")
    )
    op.alter_column("add_zkt_devices", "certification_observations", nullable=False)
    op.alter_column("add_zkt_devices", "snapshot_complete", nullable=False)
    if "uq_add_zkt_serial" in _constraints("add_zkt_devices"):
        op.drop_constraint("uq_add_zkt_serial", "add_zkt_devices", type_="unique")

    user_columns = _columns("add_device_users")
    _add("add_device_users", sa.Column("user_key", sa.String(36)))
    _add("add_device_users", sa.Column("machine_name_encrypted", sa.Text()))
    _add("add_device_users", sa.Column("lifecycle_state", sa.String(30)))
    _add("add_device_users", sa.Column("source", sa.String(40)))
    _add("add_device_users", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    _add("add_device_users", sa.Column("deleted_by", sa.String(120)))
    _add("add_device_users", sa.Column("create_audit_id", sa.Integer()))
    _add("add_device_users", sa.Column("update_audit_id", sa.Integer()))
    _add("add_device_users", sa.Column("delete_audit_id", sa.Integer()))
    _add("add_device_users", sa.Column("current_command_id", sa.Integer()))
    fernet = _fernet()
    if "raw_name" in user_columns:
        rows = bind.execute(
            text("SELECT id, raw_name, present FROM add_device_users ORDER BY id")
        ).mappings()
        for row in rows:
            bind.execute(
                text(
                    "UPDATE add_device_users SET user_key=:user_key, "
                    "machine_name_encrypted=:machine_name, lifecycle_state=:state, "
                    "source='DEVICE_SNAPSHOT' WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "user_key": str(uuid4()),
                    "machine_name": fernet.encrypt((row["raw_name"] or "").encode()).decode(),
                    "state": "ACTIVE" if row["present"] else "DELETED",
                },
            )
    else:
        bind.execute(
            text(
                "UPDATE add_device_users SET user_key=COALESCE(user_key, CAST(id AS VARCHAR)), "
                "lifecycle_state=COALESCE(lifecycle_state, 'ACTIVE'), "
                "source=COALESCE(source, 'DEVICE_SNAPSHOT')"
            )
        )
    op.alter_column("add_device_users", "user_key", nullable=False)
    op.alter_column("add_device_users", "lifecycle_state", nullable=False)
    op.alter_column("add_device_users", "source", nullable=False)
    indexes = _indexes("add_device_users")
    if "ix_add_device_users_user_key" not in indexes:
        op.create_index(
            "ix_add_device_users_user_key", "add_device_users", ["user_key"], unique=True
        )
    if "uq_add_user_device_uid" in _constraints("add_device_users"):
        op.drop_constraint("uq_add_user_device_uid", "add_device_users", type_="unique")
    if "uq_add_user_device_user_id" in _constraints("add_device_users"):
        op.drop_constraint("uq_add_user_device_user_id", "add_device_users", type_="unique")
    indexes = _indexes("add_device_users")
    if "uq_add_user_device_uid_active" not in indexes:
        op.create_index(
            "uq_add_user_device_uid_active",
            "add_device_users",
            ["zkt_device_id", "uid"],
            unique=True,
            postgresql_where=text("lifecycle_state = 'ACTIVE'"),
        )
    if "uq_add_user_device_user_id_active" not in indexes:
        op.create_index(
            "uq_add_user_device_user_id_active",
            "add_device_users",
            ["zkt_device_id", "user_id"],
            unique=True,
            postgresql_where=text("lifecycle_state = 'ACTIVE'"),
        )
    if "uq_add_user_device_cnic_active" not in indexes:
        op.create_index(
            "uq_add_user_device_cnic_active",
            "add_device_users",
            ["zkt_device_id", "cnic_lookup_hash"],
            unique=True,
            postgresql_where=text(
                "lifecycle_state = 'ACTIVE' AND cnic_lookup_hash IS NOT NULL"
            ),
        )
    if "raw_name" in _columns("add_device_users"):
        op.drop_column("add_device_users", "raw_name")

    foreign_keys = _foreign_keys("add_device_users")
    for name, column, remote in (
        ("fk_add_user_create_audit", "create_audit_id", "add_audit_events.id"),
        ("fk_add_user_update_audit", "update_audit_id", "add_audit_events.id"),
        ("fk_add_user_delete_audit", "delete_audit_id", "add_audit_events.id"),
        ("fk_add_user_current_command", "current_command_id", "add_device_commands.id"),
    ):
        if name not in foreign_keys:
            remote_table, remote_column = remote.split(".")
            op.create_foreign_key(
                name, "add_device_users", remote_table, [column], [remote_column]
            )

    _add("add_attendance_events", sa.Column("device_user_id", sa.Integer()))
    bind.execute(text("UPDATE add_attendance_events SET raw_event = CAST('{}' AS JSON)"))
    if "fk_add_attendance_device_user" not in _foreign_keys("add_attendance_events"):
        op.create_foreign_key(
            "fk_add_attendance_device_user",
            "add_attendance_events",
            "add_device_users",
            ["device_user_id"],
            ["id"],
        )
    if "raw_name" in _columns("add_attendance_events"):
        op.drop_column("add_attendance_events", "raw_name")

    command_columns = _columns("add_device_commands")
    _add("add_device_commands", sa.Column("payload_encrypted", sa.Text()))
    _add("add_device_commands", sa.Column("expected_state_encrypted", sa.Text()))
    _add("add_device_commands", sa.Column("desired_state_encrypted", sa.Text()))
    _add("add_device_commands", sa.Column("payload_summary", sa.JSON()))
    if "payload" in command_columns:
        rows = bind.execute(
            text(
                "SELECT id, payload, expected_state, desired_state FROM add_device_commands ORDER BY id"
            )
        ).mappings()
        for row in rows:
            payload = row["payload"] or {}
            summary = {
                key: value
                for key, value in payload.items()
                if key in {"user_key", "uid", "user_id", "lease_id", "duration_seconds", "reason"}
            }
            bind.execute(
                text(
                    "UPDATE add_device_commands SET payload_encrypted=:payload, "
                    "expected_state_encrypted=:expected, desired_state_encrypted=:desired, "
                    "payload_summary=CAST(:summary AS JSON) WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "payload": _encrypt_json(fernet, payload),
                    "expected": _encrypt_json(fernet, row["expected_state"]),
                    "desired": _encrypt_json(fernet, row["desired_state"]),
                    "summary": json.dumps(summary),
                },
            )
    bind.execute(
        text(
            "UPDATE add_device_commands SET payload_summary = CAST('{}' AS JSON) "
            "WHERE payload_summary IS NULL"
        )
    )
    for name in ("payload_encrypted", "expected_state_encrypted", "desired_state_encrypted"):
        op.alter_column("add_device_commands", name, nullable=False)
    if "payload" in _columns("add_device_commands"):
        op.drop_column("add_device_commands", "payload")
        op.drop_column("add_device_commands", "expected_state")
        op.drop_column("add_device_commands", "desired_state")

    if "payload" in _columns("add_ords_outbox"):
        op.drop_column("add_ords_outbox", "payload")

    OnboardingNonce.__table__.create(bind=bind, checkfirst=True)
    IdentityTombstone.__table__.create(bind=bind, checkfirst=True)

    connector_columns = _columns("add_connectors")
    if "activation_hash" in connector_columns:
        op.drop_column("add_connectors", "activation_hash")


def downgrade() -> None:
    raise RuntimeError(
        "This protected-data contract migration is not automatically reversible; restore the pre-deploy backup."
    )

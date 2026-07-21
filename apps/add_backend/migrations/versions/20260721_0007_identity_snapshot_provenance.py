"""Add stable identity snapshot provenance and verified attendance repair state.

Revision ID: 20260721_0007
Revises: 20260714_0006
Create Date: 2026-07-21

All changes are additive. Existing users, attendance, commands, and outboxes are untouched.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260721_0007"
down_revision = "20260714_0006"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def _unique_constraints(table: str) -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_unique_constraints(table)
        if item.get("name")
    }


def _foreign_key_columns(table: str) -> set[tuple[str, ...]]:
    return {
        tuple(item.get("constrained_columns") or [])
        for item in inspect(op.get_bind()).get_foreign_keys(table)
    }


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _add_index(table: str, name: str, columns: list[str]) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns)


def upgrade() -> None:
    if "add_device_user_snapshots" not in _tables():
        op.create_table(
            "add_device_user_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
            sa.Column("snapshot_id", sa.String(length=100), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("complete", sa.Boolean(), nullable=False),
            sa.Column("stable", sa.Boolean(), nullable=False),
            sa.Column("reason", sa.String(length=80), nullable=False),
            sa.Column("user_count", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("zkt_device_id", "revision", name="uq_add_user_snapshot_revision"),
        )
    if "uq_add_user_snapshot_revision" not in _unique_constraints("add_device_user_snapshots"):
        op.create_unique_constraint(
            "uq_add_user_snapshot_revision",
            "add_device_user_snapshots",
            ["zkt_device_id", "revision"],
        )
    for name, columns in (
        ("ix_add_device_user_snapshots_zkt_device_id", ["zkt_device_id"]),
        ("ix_add_device_user_snapshots_snapshot_id", ["snapshot_id"]),
        ("ix_add_device_user_snapshots_state_hash", ["state_hash"]),
        ("ix_add_device_user_snapshots_stable", ["stable"]),
        ("ix_add_device_user_snapshots_observed_at", ["observed_at"]),
    ):
        _add_index("add_device_user_snapshots", name, columns)

    for column in (
        sa.Column("identity_snapshot_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("identity_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("identity_snapshot_state_hash", sa.String(length=64), nullable=True),
        sa.Column("identity_snapshot_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_snapshot_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_snapshot_stable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_identity_change_at", sa.DateTime(timezone=True), nullable=True),
    ):
        _add_column("add_zkt_devices", column)
    if ("identity_snapshot_id",) not in _foreign_key_columns("add_zkt_devices"):
        op.create_foreign_key(
            "fk_add_zkt_identity_snapshot",
            "add_zkt_devices",
            "add_device_user_snapshots",
            ["identity_snapshot_id"],
            ["id"],
        )
    for name, columns in (
        ("ix_add_zkt_devices_identity_snapshot_id", ["identity_snapshot_id"]),
        ("ix_add_zkt_devices_identity_snapshot_state_hash", ["identity_snapshot_state_hash"]),
        ("ix_add_zkt_devices_identity_snapshot_observed_at", ["identity_snapshot_observed_at"]),
    ):
        _add_index("add_zkt_devices", name, columns)

    _add_column("add_device_users", sa.Column("snapshot_revision", sa.Integer(), nullable=True))
    _add_index("add_device_users", "ix_add_device_users_snapshot_revision", ["snapshot_revision"])

    for column in (
        sa.Column("identity_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("identity_terminal_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("identity_resolution_status", sa.String(length=50), nullable=False, server_default="WAITING_FOR_SNAPSHOT"),
        sa.Column("identity_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_repaired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_repair_reason", sa.String(length=120), nullable=True),
    ):
        _add_column("add_attendance_events", column)
    if ("identity_snapshot_id",) not in _foreign_key_columns("add_attendance_events"):
        op.create_foreign_key(
            "fk_add_attendance_identity_snapshot",
            "add_attendance_events",
            "add_device_user_snapshots",
            ["identity_snapshot_id"],
            ["id"],
        )
    for name, columns in (
        ("ix_add_attendance_events_identity_snapshot_id", ["identity_snapshot_id"]),
        ("ix_add_attendance_events_identity_terminal_fingerprint", ["identity_terminal_fingerprint"]),
        ("ix_add_attendance_events_identity_resolution_status", ["identity_resolution_status"]),
    ):
        _add_index("add_attendance_events", name, columns)


def downgrade() -> None:
    raise RuntimeError("Identity snapshot provenance is retained; restore a pre-deploy backup.")

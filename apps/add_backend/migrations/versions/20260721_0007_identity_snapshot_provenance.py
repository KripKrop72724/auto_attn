"""Add stable identity snapshot provenance and verified attendance repair state.

Revision ID: 20260721_0007
Revises: 20260714_0006
Create Date: 2026-07-21

All changes are additive. Existing users, attendance, commands, and outboxes are untouched.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_0007"
down_revision = "20260714_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.create_index("ix_add_device_user_snapshots_zkt_device_id", "add_device_user_snapshots", ["zkt_device_id"])
    op.create_index("ix_add_device_user_snapshots_snapshot_id", "add_device_user_snapshots", ["snapshot_id"])
    op.create_index("ix_add_device_user_snapshots_state_hash", "add_device_user_snapshots", ["state_hash"])
    op.create_index("ix_add_device_user_snapshots_stable", "add_device_user_snapshots", ["stable"])
    op.create_index("ix_add_device_user_snapshots_observed_at", "add_device_user_snapshots", ["observed_at"])

    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_id", sa.Integer(), nullable=True))
    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_state_hash", sa.String(length=64), nullable=True))
    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_received_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("add_zkt_devices", sa.Column("identity_snapshot_stable", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("add_zkt_devices", sa.Column("last_identity_change_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_add_zkt_identity_snapshot", "add_zkt_devices", "add_device_user_snapshots", ["identity_snapshot_id"], ["id"])
    op.create_index("ix_add_zkt_devices_identity_snapshot_id", "add_zkt_devices", ["identity_snapshot_id"])
    op.create_index("ix_add_zkt_devices_identity_snapshot_state_hash", "add_zkt_devices", ["identity_snapshot_state_hash"])
    op.create_index("ix_add_zkt_devices_identity_snapshot_observed_at", "add_zkt_devices", ["identity_snapshot_observed_at"])

    op.add_column("add_device_users", sa.Column("snapshot_revision", sa.Integer(), nullable=True))
    op.create_index("ix_add_device_users_snapshot_revision", "add_device_users", ["snapshot_revision"])

    op.add_column("add_attendance_events", sa.Column("identity_snapshot_id", sa.Integer(), nullable=True))
    op.add_column("add_attendance_events", sa.Column("identity_terminal_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("add_attendance_events", sa.Column("identity_resolution_status", sa.String(length=50), nullable=False, server_default="WAITING_FOR_SNAPSHOT"))
    op.add_column("add_attendance_events", sa.Column("identity_resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("add_attendance_events", sa.Column("identity_repaired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("add_attendance_events", sa.Column("identity_repair_reason", sa.String(length=120), nullable=True))
    op.create_foreign_key("fk_add_attendance_identity_snapshot", "add_attendance_events", "add_device_user_snapshots", ["identity_snapshot_id"], ["id"])
    op.create_index("ix_add_attendance_identity_snapshot_id", "add_attendance_events", ["identity_snapshot_id"])
    op.create_index("ix_add_attendance_identity_terminal_fingerprint", "add_attendance_events", ["identity_terminal_fingerprint"])
    op.create_index("ix_add_attendance_identity_resolution_status", "add_attendance_events", ["identity_resolution_status"])


def downgrade() -> None:
    raise RuntimeError("Identity snapshot provenance is retained; restore a pre-deploy backup.")

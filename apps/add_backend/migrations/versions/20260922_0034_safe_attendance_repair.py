"""Retain identity observations and durable national attendance repair evidence.

Revision ID: 20260922_0034
Revises: 20260922_0033
"""

from alembic import op
import sqlalchemy as sa

revision = "20260922_0034"
down_revision = "20260922_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "add_attendance_identity_history" not in tables:
        op.create_table(
            "add_attendance_identity_history",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("zkt_device_id", sa.Integer(), nullable=False),
            sa.Column("device_user_id", sa.Integer(), nullable=False),
            sa.Column("terminal_serial", sa.String(length=120), nullable=False),
            sa.Column("user_id", sa.String(length=100), nullable=False),
            sa.Column("uid", sa.String(length=40), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=True),
            sa.Column("cnic_encrypted", sa.Text(), nullable=False),
            sa.Column("cnic_lookup_hash", sa.String(length=64), nullable=False),
            sa.Column("display_name_encrypted", sa.Text(), nullable=True),
            sa.Column("first_snapshot_id", sa.Integer(), nullable=False),
            sa.Column("last_snapshot_id", sa.Integer(), nullable=False),
            sa.Column("last_revision", sa.Integer(), nullable=False),
            sa.Column("observed_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_until", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed", sa.Boolean(), nullable=False),
            sa.Column("revoked", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["device_user_id"],
                ["add_device_users.id"],
            ),
            sa.ForeignKeyConstraint(
                ["first_snapshot_id"],
                ["add_device_user_snapshots.id"],
            ),
            sa.ForeignKeyConstraint(
                ["last_snapshot_id"],
                ["add_device_user_snapshots.id"],
            ),
            sa.ForeignKeyConstraint(
                ["zkt_device_id"],
                ["add_zkt_devices.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_add_identity_history_match",
            "add_attendance_identity_history",
            ["zkt_device_id", "terminal_serial", "user_id", "uid"],
            unique=False,
        )

    if "add_attendance_safe_repair_tasks" not in tables:
        op.create_table(
            "add_attendance_safe_repair_tasks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("connector_id", sa.Integer(), nullable=False),
            sa.Column("terminal_serial", sa.String(length=120), nullable=True),
            sa.Column("hardware_id", sa.String(length=120), nullable=False),
            sa.Column("high_water_id", sa.Integer(), nullable=False),
            sa.Column("cursor", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("checked_count", sa.Integer(), nullable=False),
            sa.Column("evidence_digest", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["connector_id"],
                ["add_connectors.id"],
            ),
            sa.ForeignKeyConstraint(
                ["job_id"],
                ["add_attendance_recovery_jobs.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "connector_id", name="uq_add_safe_repair_task_device"),
        )
        op.create_index(
            op.f("ix_add_attendance_safe_repair_tasks_connector_id"),
            "add_attendance_safe_repair_tasks",
            ["connector_id"],
            unique=False,
        )
        op.create_index(
            op.f("ix_add_attendance_safe_repair_tasks_job_id"),
            "add_attendance_safe_repair_tasks",
            ["job_id"],
            unique=False,
        )
        op.create_index(
            "ix_add_safe_repair_task_schedule",
            "add_attendance_safe_repair_tasks",
            ["status", "updated_at"],
            unique=False,
        )

    if "add_attendance_safe_repair_decisions" not in tables:
        op.create_table(
            "add_attendance_safe_repair_decisions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("attendance_event_id", sa.Integer(), nullable=False),
            sa.Column("actor", sa.String(length=120), nullable=False),
            sa.Column("proof", sa.JSON(), nullable=False),
            sa.Column("prior_state", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["attendance_event_id"],
                ["add_attendance_events.id"],
            ),
            sa.ForeignKeyConstraint(
                ["item_id"],
                ["add_attendance_recovery_items.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("item_id"),
        )
        op.create_index(
            op.f("ix_add_attendance_safe_repair_decisions_attendance_event_id"),
            "add_attendance_safe_repair_decisions",
            ["attendance_event_id"],
            unique=False,
        )

    if "add_attendance_delivery_schedule" not in tables:
        op.create_table(
            "add_attendance_delivery_schedule",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("priority_served", sa.Integer(), nullable=False),
            sa.Column("last_connector_id", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )



def downgrade() -> None:
    # Operational evidence survives an application rollback.
    pass

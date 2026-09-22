"""Add restart-safe historical attendance delivery sweep telemetry.

Revision ID: 20260922_0032
Revises: 20260921_0031
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260922_0032"
down_revision = "20260921_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "add_attendance_delivery_sweeps" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "add_attendance_delivery_sweeps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(30), nullable=False, server_default="IDLE"),
        sa.Column("lease_owner", sa.String(120)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_event_id", sa.Integer()),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repaired_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outbox_created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_add_attendance_delivery_sweeps_state",
        "add_attendance_delivery_sweeps",
        ["state"],
    )
    op.create_index(
        "ix_add_attendance_delivery_sweeps_lease_until",
        "add_attendance_delivery_sweeps",
        ["lease_until"],
    )
    op.create_index(
        "ix_add_attendance_delivery_sweeps_last_event_id",
        "add_attendance_delivery_sweeps",
        ["last_event_id"],
    )


def downgrade() -> None:
    # Preserve operational evidence during rollback; the table is additive.
    pass

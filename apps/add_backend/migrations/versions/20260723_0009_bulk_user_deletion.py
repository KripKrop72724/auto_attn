"""Add durable bulk user deletion jobs.

Revision ID: 20260723_0009
Revises: 20260721_0008
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260723_0009"
down_revision = "20260721_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The historical initial revision creates the current metadata snapshot on a
    # fresh database. Production upgrades arrive here without these tables.
    # Supporting both paths keeps fresh CI migrations and in-place upgrades safe.
    if "add_user_deletion_jobs" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "add_user_deletion_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="QUEUED"),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("canceled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expired_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "connector_id",
            "idempotency_key",
            name="uq_add_user_deletion_job_idempotency",
        ),
    )
    for column in ("connector_id", "zkt_device_id", "actor", "status", "expires_at"):
        op.create_index(
            f"ix_add_user_deletion_jobs_{column}",
            "add_user_deletion_jobs",
            [column],
        )
    op.create_index(
        "ix_add_user_deletion_jobs_job_id",
        "add_user_deletion_jobs",
        ["job_id"],
        unique=True,
    )

    op.create_table(
        "add_user_deletion_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("add_user_deletion_jobs.id"),
            nullable=False,
        ),
        sa.Column(
            "device_user_id",
            sa.Integer(),
            sa.ForeignKey("add_device_users.id"),
            nullable=False,
        ),
        sa.Column("user_key", sa.String(36), nullable=False),
        sa.Column("uid", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("display_name_encrypted", sa.Text(), nullable=False),
        sa.Column("expected_row_version", sa.Integer(), nullable=False),
        sa.Column("expected_identity_fingerprint", sa.String(64)),
        sa.Column("expected_state_fingerprint", sa.String(64)),
        sa.Column("status", sa.String(40), nullable=False, server_default="PENDING"),
        sa.Column(
            "current_command_id",
            sa.Integer(),
            sa.ForeignKey("add_device_commands.id"),
        ),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "user_key", name="uq_add_user_deletion_item_user"),
    )
    for column in (
        "job_id",
        "device_user_id",
        "status",
        "current_command_id",
        "error_code",
    ):
        op.create_index(
            f"ix_add_user_deletion_items_{column}",
            "add_user_deletion_items",
            [column],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Bulk deletion job and audit state is retained; restore a pre-deploy backup."
    )

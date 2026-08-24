"""Add durable, per-item attendance batch settlement.

Revision ID: 20260824_0019
Revises: 20260810_0018
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260824_0019"
down_revision = "20260810_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_attendance_batch_receipts" not in tables:
        op.create_table(
            "add_attendance_batch_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("receipt_id", sa.String(36), nullable=False, unique=True),
            sa.Column(
                "connector_id",
                sa.Integer(),
                sa.ForeignKey("add_connectors.id"),
                nullable=False,
            ),
            sa.Column(
                "zkt_device_id",
                sa.Integer(),
                sa.ForeignKey("add_zkt_devices.id"),
                nullable=False,
            ),
            sa.Column("batch_id", sa.String(120), nullable=False),
            sa.Column("payload_digest", sa.String(64), nullable=False),
            sa.Column("reported_payload_digest", sa.String(128)),
            sa.Column("outcome", sa.String(50), nullable=False),
            sa.Column("item_count", sa.Integer(), nullable=False),
            sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("observation_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "connector_id",
                "batch_id",
                "payload_digest",
                name="uq_add_attendance_batch_receipt_identity",
            ),
        )
        for column in (
            "receipt_id",
            "connector_id",
            "zkt_device_id",
            "batch_id",
            "payload_digest",
            "outcome",
        ):
            op.create_index(
                f"ix_add_attendance_batch_receipts_{column}",
                "add_attendance_batch_receipts",
                [column],
            )

    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_attendance_batch_items" not in tables:
        op.create_table(
            "add_attendance_batch_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "receipt_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_batch_receipts.id"),
                nullable=False,
            ),
            sa.Column("item_index", sa.Integer(), nullable=False),
            sa.Column("disposition", sa.String(30), nullable=False),
            sa.Column("event_uid", sa.String(128)),
            sa.Column(
                "attendance_event_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_events.id"),
            ),
            sa.Column("payload_digest", sa.String(64), nullable=False),
            sa.Column("error_code", sa.String(120)),
            sa.Column("error_path", sa.String(255)),
            sa.Column("validation_summary", sa.JSON(), nullable=False),
            sa.Column("protected_payload", sa.Text()),
            sa.Column(
                "review_state",
                sa.String(30),
                nullable=False,
                server_default="NOT_REQUIRED",
            ),
            sa.Column("reviewed_by", sa.String(120)),
            sa.Column("review_reason", sa.Text()),
            sa.Column("review_idempotency_key", sa.String(120), unique=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "receipt_id",
                "item_index",
                name="uq_add_attendance_batch_item_index",
            ),
        )
        for column in (
            "receipt_id",
            "disposition",
            "event_uid",
            "attendance_event_id",
            "payload_digest",
            "error_code",
            "review_state",
            "reviewed_by",
            "review_idempotency_key",
        ):
            op.create_index(
                f"ix_add_attendance_batch_items_{column}",
                "add_attendance_batch_items",
                [column],
            )


def downgrade() -> None:
    raise RuntimeError(
        "Attendance batch receipts are immutable delivery evidence; restore a "
        "pre-deploy backup instead of dropping them."
    )

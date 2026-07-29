"""Add durable exact-cohort current identity resolutions.

Revision ID: 20260729_0011
Revises: 20260727_0010
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260729_0011"
down_revision = "20260727_0010"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


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


def upgrade() -> None:
    table = "add_historical_current_identity_resolutions"
    if table not in _tables():
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("resolution_id", sa.String(36), nullable=False),
            sa.Column(
                "zkt_device_id",
                sa.Integer(),
                sa.ForeignKey("add_zkt_devices.id"),
                nullable=False,
            ),
            sa.Column(
                "device_user_id",
                sa.Integer(),
                sa.ForeignKey("add_device_users.id"),
                nullable=False,
            ),
            sa.Column("group_token", sa.String(64), nullable=False),
            sa.Column("source_user_id", sa.String(100), nullable=False),
            sa.Column("source_uid", sa.String(40), nullable=False, server_default=""),
            sa.Column("source_cnic_lookup_hash", sa.String(64), nullable=False),
            sa.Column("verified_employee_name", sa.String(255), nullable=False),
            sa.Column("event_count", sa.Integer(), nullable=False),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "zkt_device_id",
                "group_token",
                name="uq_add_historical_current_identity_group",
            ),
            sa.UniqueConstraint(
                "zkt_device_id",
                "idempotency_key",
                name="uq_add_historical_current_identity_idempotency",
            ),
        )

    indexes = _indexes(table)
    unique_constraints = _unique_constraints(table)
    index_specs = (
        ("ix_add_historical_current_identity_resolutions_resolution_id", ["resolution_id"], True),
        ("ix_add_historical_current_identity_resolutions_zkt_device_id", ["zkt_device_id"], False),
        ("ix_add_historical_current_identity_resolutions_device_user_id", ["device_user_id"], False),
        ("ix_add_historical_current_identity_resolutions_group_token", ["group_token"], False),
        ("ix_add_historical_current_identity_resolutions_source_user_id", ["source_user_id"], False),
        ("ix_add_historical_current_identity_resolutions_actor", ["actor"], False),
    )
    for name, columns, unique in index_specs:
        if name not in indexes and name not in unique_constraints:
            op.create_index(name, table, columns, unique=unique)


def downgrade() -> None:
    # Attendance identity evidence is an immutable audit record. Production
    # downgrades must preserve it, so this migration is intentionally forward-only.
    pass

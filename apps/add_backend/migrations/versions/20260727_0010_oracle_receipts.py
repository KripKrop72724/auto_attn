"""Add durable Oracle receipt evidence.

Revision ID: 20260727_0010
Revises: 20260723_0009
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260727_0010"
down_revision = "20260723_0009"
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


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _add_index(table: str, name: str, columns: list[str], *, unique: bool = False) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    _add_column(
        "add_attendance_events",
        sa.Column("oracle_confirmed_at", sa.DateTime(timezone=True)),
    )
    _add_column(
        "add_attendance_events",
        sa.Column("oracle_confirmation_path", sa.String(40)),
    )
    _add_index(
        "add_attendance_events",
        "ix_add_attendance_events_oracle_confirmed_at",
        ["oracle_confirmed_at"],
    )
    _add_index(
        "add_attendance_events",
        "ix_add_attendance_events_oracle_confirmation_path",
        ["oracle_confirmation_path"],
    )

    if "add_oracle_receipts" not in _tables():
        op.create_table(
            "add_oracle_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_uid", sa.String(64), nullable=False),
            sa.Column(
                "connector_id",
                sa.Integer(),
                sa.ForeignKey("add_connectors.id"),
                nullable=False,
            ),
            sa.Column(
                "attendance_event_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_events.id"),
            ),
            sa.Column("confirmation_path", sa.String(40), nullable=False),
            sa.Column(
                "oracle_observed_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "first_received_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "last_received_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "observation_count",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )
    _add_index(
        "add_oracle_receipts",
        "ix_add_oracle_receipts_event_uid",
        ["event_uid"],
        unique=True,
    )
    _add_index(
        "add_oracle_receipts",
        "ix_add_oracle_receipts_connector_id",
        ["connector_id"],
    )
    _add_index(
        "add_oracle_receipts",
        "ix_add_oracle_receipts_attendance_event_id",
        ["attendance_event_id"],
        unique=True,
    )
    _add_index(
        "add_oracle_receipts",
        "ix_add_oracle_receipts_confirmation_path",
        ["confirmation_path"],
    )
    _add_index(
        "add_oracle_receipts",
        "ix_add_oracle_receipts_oracle_observed_at",
        ["oracle_observed_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Oracle receipt evidence is retained; restore a pre-deploy backup."
    )

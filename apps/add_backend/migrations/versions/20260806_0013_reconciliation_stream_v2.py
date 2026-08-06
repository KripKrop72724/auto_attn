"""Add durable reconciliation stream-v2 assignment credits.

Revision ID: 20260806_0013
Revises: 20260806_0012
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260806_0013"
down_revision = "20260806_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    table = "add_reconciliation_jobs"
    existing = {row["name"] for row in inspector.get_columns(table)}
    columns = {
        "active_assignment_id": sa.Column("active_assignment_id", sa.String(36)),
        "credit_start_ordinal": sa.Column("credit_start_ordinal", sa.Integer()),
        "credit_end_ordinal": sa.Column("credit_end_ordinal", sa.Integer()),
        "credit_committed_through": sa.Column("credit_committed_through", sa.Integer()),
        "assignment_granted_at": sa.Column("assignment_granted_at", sa.DateTime(timezone=True)),
        "assignment_expires_at": sa.Column("assignment_expires_at", sa.DateTime(timezone=True)),
        "assignment_accepted_at": sa.Column("assignment_accepted_at", sa.DateTime(timezone=True)),
        "assignment_heartbeat_at": sa.Column("assignment_heartbeat_at", sa.DateTime(timezone=True)),
    }
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)
    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(table)}
    for name in ("active_assignment_id", "assignment_expires_at"):
        index = f"ix_add_reconciliation_jobs_{name}"
        if index not in indexes:
            op.create_index(index, table, [name])


def downgrade() -> None:
    raise RuntimeError(
        "Reconciliation stream-v2 leases are production checkpoint evidence; restore a pre-deploy backup."
    )

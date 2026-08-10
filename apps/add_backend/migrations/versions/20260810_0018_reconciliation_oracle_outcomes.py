"""Track terminal Oracle outcomes without rewriting attendance evidence.

Revision ID: 20260810_0018
Revises: 20260809_0017
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260810_0018"
down_revision = "20260809_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "add_reconciliation_jobs"
    columns = {row["name"] for row in inspect(op.get_bind()).get_columns(table)}
    if "ords_review_count" not in columns:
        op.add_column(
            table,
            sa.Column(
                "ords_review_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    op.execute(
        sa.text(
            "UPDATE add_reconciliation_jobs SET ords_review_count = 0 "
            "WHERE ords_review_count IS NULL"
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "The Oracle outcome counter is additive operational evidence; "
        "restore a pre-deploy backup instead of dropping it."
    )

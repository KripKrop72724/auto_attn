"""Add spare-device inventory classification.

Revision ID: 20260902_0022
Revises: 20260827_0021
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260902_0022"
down_revision = "20260827_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("add_connectors")}
    if "is_spare" not in columns:
        op.add_column(
            "add_connectors",
            sa.Column("is_spare", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    indexes = {index["name"] for index in inspect(op.get_bind()).get_indexes("add_connectors")}
    if "ix_add_connectors_is_spare" not in indexes:
        op.create_index("ix_add_connectors_is_spare", "add_connectors", ["is_spare"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("add_connectors")}
    if "ix_add_connectors_is_spare" in indexes:
        op.drop_index("ix_add_connectors_is_spare", table_name="add_connectors")
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("add_connectors")}
    if "is_spare" in columns:
        op.drop_column("add_connectors", "is_spare")

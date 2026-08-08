"""Add idempotent firmware campaign creation.

Revision ID: 20260809_0016
Revises: 20260807_0015
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260809_0016"
down_revision = "20260807_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "add_firmware_campaigns"
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    columns = {row["name"] for row in inspector.get_columns(table)}
    if "idempotency_key" not in columns:
        op.add_column(table, sa.Column("idempotency_key", sa.String(120), nullable=True))
    op.execute(
        sa.text(
            "UPDATE add_firmware_campaigns "
            "SET idempotency_key = 'legacy:' || campaign_id "
            "WHERE idempotency_key IS NULL"
        )
    )
    unique_names = {
        row.get("name") for row in inspect(op.get_bind()).get_unique_constraints(table)
    }
    with op.batch_alter_table(table) as batch:
        batch.alter_column("idempotency_key", existing_type=sa.String(120), nullable=False)
        if "uq_add_firmware_campaign_actor_idempotency" not in unique_names:
            batch.create_unique_constraint(
                "uq_add_firmware_campaign_actor_idempotency",
                ["actor", "idempotency_key"],
            )


def downgrade() -> None:
    raise RuntimeError(
        "Firmware idempotency records are part of the audit trail; restore a pre-deploy backup."
    )

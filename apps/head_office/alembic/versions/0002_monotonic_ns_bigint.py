"""Use bigint for monotonic clock values."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_monotonic_ns_bigint"
down_revision = "0001_head_office_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "clock_checks",
        "monotonic_ns",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "clock_checks",
        "monotonic_ns",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )

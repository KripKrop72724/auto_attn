"""Redact legacy ORDS response bodies retained before bounded classification.

Revision ID: 20260714_0003
Revises: 20260713_0002
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "20260714_0003"
down_revision = "20260713_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            "UPDATE add_ords_outbox "
            "SET last_error = 'LEGACY_ERROR_REDACTED' "
            "WHERE last_error IS NOT NULL"
        )
    )


def downgrade() -> None:
    # Redacted response text cannot and must not be reconstructed.
    pass

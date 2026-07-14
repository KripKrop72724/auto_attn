"""Add opaque terminal user precondition fingerprints.

Revision ID: 20260714_0006
Revises: 20260714_0005
Create Date: 2026-07-14

The migration only adds nullable columns. It does not rewrite users, terminal
records, biometric templates, commands, or attendance rows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260714_0006"
down_revision = "20260714_0005"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_columns("add_device_users")
    }


def upgrade() -> None:
    columns = _columns()
    if "terminal_identity_fingerprint" not in columns:
        op.add_column(
            "add_device_users",
            sa.Column("terminal_identity_fingerprint", sa.String(length=64), nullable=True),
        )
    if "terminal_state_fingerprint" not in columns:
        op.add_column(
            "add_device_users",
            sa.Column("terminal_state_fingerprint", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Terminal precondition fingerprints are not automatically discarded; "
        "restore a pre-deploy backup."
    )

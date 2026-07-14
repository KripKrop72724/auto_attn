"""Add reversible duplicate-CNIC identity resolutions.

Revision ID: 20260714_0005
Revises: 20260714_0004
Create Date: 2026-07-14

The migration only expands the schema.  It does not rewrite device users or
attendance rows and is safe to apply while the connector remains online.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from zk_add.models import IdentityConflictResolution


revision = "20260714_0005"
down_revision = "20260714_0004"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        item["name"] for item in inspect(op.get_bind()).get_indexes(table) if item.get("name")
    }


def _has_foreign_key(table: str, column: str) -> bool:
    return any(
        column in (item.get("constrained_columns") or [])
        for item in inspect(op.get_bind()).get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if "add_identity_conflict_resolutions" not in _tables():
        IdentityConflictResolution.__table__.create(bind=bind, checkfirst=True)

    if "identity_resolution_id" not in _columns("add_attendance_events"):
        op.add_column(
            "add_attendance_events",
            sa.Column("identity_resolution_id", sa.Integer(), nullable=True),
        )
    if not _has_foreign_key("add_attendance_events", "identity_resolution_id"):
        op.create_foreign_key(
            "fk_add_attendance_identity_resolution",
            "add_attendance_events",
            "add_identity_conflict_resolutions",
            ["identity_resolution_id"],
            ["id"],
        )
    if "ix_add_attendance_events_identity_resolution_id" not in _indexes(
        "add_attendance_events"
    ):
        op.create_index(
            "ix_add_attendance_events_identity_resolution_id",
            "add_attendance_events",
            ["identity_resolution_id"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Identity-resolution provenance is not automatically discarded; restore a pre-deploy backup."
    )

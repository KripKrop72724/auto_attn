"""Enforce one open alert per connector and condition code.

Revision ID: 20260714_0004
Revises: 20260714_0003
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "20260714_0004"
down_revision = "20260714_0003"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_add_open_alert_connector_code"


def _indexes() -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_indexes("add_device_alerts")
        if item.get("name")
    }


def upgrade() -> None:
    op.get_bind().execute(
        text(
            "WITH ranked AS ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY connector_id, code "
            "ORDER BY last_seen_at DESC, id DESC"
            ") AS duplicate_rank "
            "FROM add_device_alerts WHERE state = 'OPEN'"
            ") "
            "UPDATE add_device_alerts "
            "SET state = 'RESOLVED', "
            "resolved_at = COALESCE(resolved_at, CURRENT_TIMESTAMP) "
            "WHERE id IN (SELECT id FROM ranked WHERE duplicate_rank > 1)"
        )
    )
    if INDEX_NAME not in _indexes():
        op.create_index(
            INDEX_NAME,
            "add_device_alerts",
            ["connector_id", "code"],
            unique=True,
            postgresql_where=sa.text("state = 'OPEN'"),
            sqlite_where=sa.text("state = 'OPEN'"),
        )


def downgrade() -> None:
    if INDEX_NAME in _indexes():
        op.drop_index(INDEX_NAME, table_name="add_device_alerts")

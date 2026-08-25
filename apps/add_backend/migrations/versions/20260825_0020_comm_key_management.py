"""Add secure per-connector COMM Key management state.

Revision ID: 20260825_0020
Revises: 20260824_0019
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260825_0020"
down_revision = "20260824_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connector_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("add_connectors")
    }
    if "comm_key_capable" not in connector_columns:
        op.add_column(
            "add_connectors",
            sa.Column("comm_key_capable", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index(
            "ix_add_connectors_comm_key_capable",
            "add_connectors",
            ["comm_key_capable"],
        )
    if "comm_key_revision" not in connector_columns:
        op.add_column(
            "add_connectors",
            sa.Column("comm_key_revision", sa.Integer(), nullable=False, server_default="0"),
        )

    campaign_indexes = {
        index["name"] for index in inspect(op.get_bind()).get_indexes("add_firmware_campaigns")
    }
    if "uq_add_firmware_campaign_active_zone" not in campaign_indexes:
        active_clause = sa.text("status IN ('ACTIVE', 'PAUSED')")
        op.create_index(
            "uq_add_firmware_campaign_active_zone",
            "add_firmware_campaigns",
            ["zone_id"],
            unique=True,
            postgresql_where=active_clause,
            sqlite_where=active_clause,
        )

    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_connector_comm_key_states" not in tables:
        op.create_table(
            "add_connector_comm_key_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
            sa.Column("applied_secret_encrypted", sa.Text()),
            sa.Column("pending_secret_encrypted", sa.Text()),
            sa.Column("applied_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("desired_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(40), nullable=False, server_default="UNKNOWN"),
            sa.Column("mode", sa.String(40)),
            sa.Column("expected_terminal_serial", sa.String(120)),
            sa.Column("last_verified_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(120)),
            sa.Column("updated_by", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("connector_id", name="uq_add_comm_key_state_connector"),
        )
        op.create_index(
            "ix_add_connector_comm_key_states_connector_id",
            "add_connector_comm_key_states",
            ["connector_id"],
            unique=True,
        )
        op.create_index(
            "ix_add_connector_comm_key_states_status",
            "add_connector_comm_key_states",
            ["status"],
        )
        op.create_index(
            "ix_add_connector_comm_key_states_last_error_code",
            "add_connector_comm_key_states",
            ["last_error_code"],
        )

    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_comm_key_operations" not in tables:
        op.create_table(
            "add_comm_key_operations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
            sa.Column("command_id", sa.Integer(), sa.ForeignKey("add_device_commands.id"), unique=True),
            sa.Column("mode", sa.String(40), nullable=False),
            sa.Column("requested_revision", sa.Integer(), nullable=False),
            sa.Column("expected_terminal_serial", sa.String(120), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="QUEUED"),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("error_code", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "connector_id",
                "idempotency_key",
                name="uq_add_comm_key_operation_idempotency",
            ),
        )
        for column in (
            "operation_id", "connector_id", "command_id", "mode", "status", "actor",
            "error_code", "expires_at",
        ):
            op.create_index(
                f"ix_add_comm_key_operations_{column}",
                "add_comm_key_operations",
                [column],
                unique=column == "command_id",
            )


def downgrade() -> None:
    campaign_indexes = {
        index["name"] for index in inspect(op.get_bind()).get_indexes("add_firmware_campaigns")
    }
    if "uq_add_firmware_campaign_active_zone" in campaign_indexes:
        op.drop_index("uq_add_firmware_campaign_active_zone", table_name="add_firmware_campaigns")
    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_comm_key_operations" in tables:
        op.drop_table("add_comm_key_operations")
    if "add_connector_comm_key_states" in tables:
        op.drop_table("add_connector_comm_key_states")
    connector_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("add_connectors")
    }
    if "comm_key_revision" in connector_columns:
        op.drop_column("add_connectors", "comm_key_revision")
    if "comm_key_capable" in connector_columns:
        op.drop_index("ix_add_connectors_comm_key_capable", table_name="add_connectors")
        op.drop_column("add_connectors", "comm_key_capable")

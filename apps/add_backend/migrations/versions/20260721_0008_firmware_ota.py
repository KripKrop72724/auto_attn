"""Add backward-compatible firmware OTA control-plane state."""

from alembic import op
import sqlalchemy as sa

revision = "20260721_0008"
down_revision = "20260721_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        sa.Column("ota_capable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ota_secure_boot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ota_rollback_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ota_partition_layout", sa.String(80)),
        sa.Column("ota_state", sa.String(40), nullable=False, server_default="LEGACY_MANUAL_UPDATE"),
        sa.Column("ota_running_partition", sa.String(40)),
        sa.Column("ota_image_sha256", sa.String(64)),
        sa.Column("ota_signing_key_id", sa.String(80)),
    ]
    for column in columns:
        op.add_column("add_connectors", column)
    op.create_index("ix_add_connectors_ota_capable", "add_connectors", ["ota_capable"])
    op.create_index("ix_add_connectors_ota_state", "add_connectors", ["ota_state"])
    op.create_table(
        "add_firmware_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.String(100), nullable=False, unique=True),
        sa.Column("version", sa.String(80), nullable=False, unique=True),
        sa.Column("git_sha", sa.String(64), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("image_size", sa.BigInteger(), nullable=False),
        sa.Column("signing_key_id", sa.String(80), nullable=False),
        sa.Column("partition_layout", sa.String(80), nullable=False),
        sa.Column("minimum_bootstrap_version", sa.String(80), nullable=False, server_default="2.2.0"),
        sa.Column("storage_name", sa.String(255), nullable=False, unique=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_signature", sa.Text(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="AVAILABLE"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(120)),
    )
    for column, unique in (("release_id", True), ("version", True), ("git_sha", False), ("state", False)):
        op.create_index(f"ix_add_firmware_releases_{column}", "add_firmware_releases", [column], unique=unique)
    op.create_table(
        "add_firmware_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.String(100), nullable=False, unique=True),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("add_firmware_releases.id"), nullable=False),
        sa.Column("zone_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("typed_confirmation", sa.String(80), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("legacy_skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_reason", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column, unique in (("campaign_id", True), ("release_id", False), ("zone_id", False), ("status", False)):
        op.create_index(f"ix_add_firmware_campaigns_{column}", "add_firmware_campaigns", [column], unique=unique)
    op.create_table(
        "add_firmware_deployments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deployment_id", sa.String(100), nullable=False, unique=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("add_firmware_campaigns.id"), nullable=False),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("add_firmware_releases.id"), nullable=False),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="PENDING"),
        sa.Column("previous_version", sa.String(80)),
        sa.Column("target_version", sa.String(80), nullable=False),
        sa.Column("bytes_written", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.Text()),
        sa.Column("offered_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column, unique in (("deployment_id", True), ("campaign_id", False), ("release_id", False),
                           ("connector_id", False), ("status", False), ("error_code", False)):
        op.create_index(f"ix_add_firmware_deployments_{column}", "add_firmware_deployments", [column], unique=unique)
    op.create_table(
        "add_firmware_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deployment_id", sa.Integer(), sa.ForeignKey("add_firmware_deployments.id"), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_add_firmware_events_deployment_id", "add_firmware_events", ["deployment_id"])
    op.create_index("ix_add_firmware_events_state", "add_firmware_events", ["state"])
    op.create_table(
        "add_firmware_download_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("deployment_id", sa.Integer(), sa.ForeignKey("add_firmware_deployments.id"), nullable=False),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    for column, unique in (("token_hash", True), ("deployment_id", False), ("connector_id", False), ("expires_at", False)):
        op.create_index(f"ix_add_firmware_download_grants_{column}", "add_firmware_download_grants", [column], unique=unique)


def downgrade() -> None:
    op.drop_table("add_firmware_download_grants")
    op.drop_table("add_firmware_events")
    op.drop_table("add_firmware_deployments")
    op.drop_table("add_firmware_campaigns")
    op.drop_table("add_firmware_releases")
    op.drop_index("ix_add_connectors_ota_state", table_name="add_connectors")
    op.drop_index("ix_add_connectors_ota_capable", table_name="add_connectors")
    for name in ("ota_signing_key_id", "ota_image_sha256", "ota_running_partition", "ota_state",
                 "ota_partition_layout", "ota_rollback_enabled", "ota_secure_boot", "ota_capable"):
        op.drop_column("add_connectors", name)

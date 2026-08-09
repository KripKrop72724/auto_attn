"""Add physical ESP32 provisioning, companion and terminal-binding state."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20260809_0017"
down_revision = "20260809_0016"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        item["name"]
        for item in inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _add_index(
    table: str,
    name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where: sa.TextClause | None = None,
    sqlite_where: sa.TextClause | None = None,
) -> None:
    if name not in _indexes(table):
        op.create_index(
            name,
            table,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
            sqlite_where=sqlite_where,
        )


def upgrade() -> None:
    zkt_columns = [
        sa.Column(
            "terminal_binding_state",
            sa.String(40),
            nullable=False,
            server_default="SERIAL_CONFIRMATION_REQUIRED",
        ),
        sa.Column("confirmed_serial", sa.String(120)),
        sa.Column("serial_confirmed_by", sa.String(120)),
        sa.Column("serial_confirmed_at", sa.DateTime(timezone=True)),
    ]
    for column in zkt_columns:
        _add_column("add_zkt_devices", column)
    _add_index(
        "add_zkt_devices",
        "ix_add_zkt_devices_terminal_binding_state",
        ["terminal_binding_state"],
    )
    _add_index("add_zkt_devices", "ix_add_zkt_devices_confirmed_serial", ["confirmed_serial"])
    op.execute(
        sa.text(
            "UPDATE add_zkt_devices SET terminal_binding_state = 'CONFIRMED', "
            "confirmed_serial = COALESCE(NULLIF(expected_serial, ''), serial), "
            "serial_confirmed_by = 'MIGRATED_PREEXISTING', "
            "serial_confirmed_at = CURRENT_TIMESTAMP "
            "WHERE (expected_serial IS NOT NULL AND expected_serial <> '') "
            "OR (serial IS NOT NULL AND serial <> '')"
        )
    )

    if "add_provisioning_companions" not in _tables():
        op.create_table(
            "add_provisioning_companions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("companion_id", sa.String(36), nullable=False, unique=True),
            sa.Column("installation_id", sa.String(100), nullable=False, unique=True),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("platform", sa.String(40), nullable=False),
            sa.Column("application_version", sa.String(40), nullable=False),
            sa.Column("pairing_code_hash", sa.String(64)),
            sa.Column("pairing_expires_at", sa.DateTime(timezone=True)),
            sa.Column("paired", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("paired_operator", sa.String(120)),
            sa.Column("paired_at", sa.DateTime(timezone=True)),
            sa.Column("last_contact_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    for column, unique in (
        ("companion_id", True),
        ("installation_id", True),
        ("platform", False),
        ("application_version", False),
        ("pairing_code_hash", False),
        ("pairing_expires_at", False),
        ("paired", False),
        ("revoked", False),
        ("paired_operator", False),
        ("last_contact_at", False),
    ):
        _add_index(
            "add_provisioning_companions",
            f"ix_add_provisioning_companions_{column}",
            [column],
            unique=unique,
        )

    if "add_factory_firmware_bundles" not in _tables():
        op.create_table(
            "add_factory_firmware_bundles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bundle_id", sa.String(100), nullable=False, unique=True),
            sa.Column("hardware_profile", sa.String(80), nullable=False),
            sa.Column("version", sa.String(80), nullable=False),
            sa.Column("git_sha", sa.String(64), nullable=False),
            sa.Column("partition_layout", sa.String(80), nullable=False),
            sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("manifest_signature", sa.Text(), nullable=False),
            sa.Column("signing_key_ids", sa.JSON(), nullable=False),
            sa.Column("setup_password_supplied", sa.Boolean(), nullable=False),
            sa.Column("state", sa.String(30), nullable=False, server_default="HIL_ONLY"),
            sa.Column("storage_prefix", sa.String(255), nullable=False, unique=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.Column("revoked_by", sa.String(120)),
            sa.UniqueConstraint(
                "hardware_profile",
                "version",
                "manifest_sha256",
                name="uq_add_factory_bundle_immutable",
            ),
        )
    for column, unique in (
        ("bundle_id", True),
        ("hardware_profile", False),
        ("version", False),
        ("git_sha", False),
        ("manifest_sha256", True),
        ("state", False),
    ):
        _add_index(
            "add_factory_firmware_bundles",
            f"ix_add_factory_firmware_bundles_{column}",
            [column],
            unique=unique,
        )

    if "add_provisioning_sessions" not in _tables():
        op.create_table(
            "add_provisioning_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.String(36), nullable=False, unique=True),
            sa.Column("operator", sa.String(120), nullable=False),
            sa.Column(
                "companion_id",
                sa.Integer(),
                sa.ForeignKey("add_provisioning_companions.id"),
                nullable=False,
            ),
            sa.Column("hardware_mac", sa.String(17)),
            sa.Column("hardware_classification", sa.String(60)),
            sa.Column("hardware_evidence", sa.JSON(), nullable=False),
            sa.Column("mode", sa.String(40)),
            sa.Column(
                "bundle_id", sa.Integer(), sa.ForeignKey("add_factory_firmware_bundles.id")
            ),
            sa.Column("zone_id", sa.String(64)),
            sa.Column("zone_name", sa.String(120)),
            sa.Column("device_id", sa.String(31)),
            sa.Column("preferred_ip", sa.String(15)),
            sa.Column("zkt_port", sa.Integer()),
            sa.Column("config_digest", sa.String(64)),
            sa.Column("recipient_public_key", sa.Text()),
            sa.Column(
                "state",
                sa.String(50),
                nullable=False,
                server_default="WAITING_FOR_COMPANION",
            ),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("artifact_id", sa.String(100)),
            sa.Column("artifact_sha256", sa.String(64)),
            sa.Column("artifact_expires_at", sa.DateTime(timezone=True)),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id")),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("authorized_at", sa.DateTime(timezone=True)),
            sa.Column("irreversible_started_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "operator", "idempotency_key", name="uq_add_provisioning_session_request"
            ),
        )
    for column, unique in (
        ("session_id", True),
        ("operator", False),
        ("companion_id", False),
        ("hardware_mac", False),
        ("hardware_classification", False),
        ("mode", False),
        ("bundle_id", False),
        ("zone_id", False),
        ("device_id", False),
        ("state", False),
        ("artifact_id", False),
        ("connector_id", False),
        ("expires_at", False),
        ("completed_at", False),
    ):
        _add_index(
            "add_provisioning_sessions",
            f"ix_add_provisioning_sessions_{column}",
            [column],
            unique=unique,
        )
    active_clause = sa.text(
        "state NOT IN ('VERIFIED_ONLINE','SITE_VALIDATION_PENDING','RECOVERY_REQUIRED',"
        "'FAILED','CANCELLED','EXPIRED')"
    )
    _add_index(
        "add_provisioning_sessions",
        "uq_add_provisioning_active_mac",
        ["hardware_mac"],
        unique=True,
        postgresql_where=sa.text(f"hardware_mac IS NOT NULL AND {active_clause.text}"),
        sqlite_where=sa.text(f"hardware_mac IS NOT NULL AND {active_clause.text}"),
    )
    _add_index(
        "add_provisioning_sessions",
        "uq_add_provisioning_active_companion",
        ["companion_id"],
        unique=True,
        postgresql_where=active_clause,
        sqlite_where=active_clause,
    )
    _add_index(
        "add_provisioning_sessions",
        "uq_add_provisioning_active_operator",
        ["operator"],
        unique=True,
        postgresql_where=active_clause,
        sqlite_where=active_clause,
    )

    if "add_provisioning_events" not in _tables():
        op.create_table(
            "add_provisioning_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "session_id",
                sa.Integer(),
                sa.ForeignKey("add_provisioning_sessions.id"),
                nullable=False,
            ),
            sa.Column("sequence", sa.BigInteger(), nullable=False),
            sa.Column("state", sa.String(50), nullable=False),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("source_sequence", sa.BigInteger()),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "session_id", "sequence", name="uq_add_provisioning_event_sequence"
            ),
            sa.UniqueConstraint(
                "session_id",
                "source",
                "source_sequence",
                name="uq_add_provisioning_event_source_sequence",
            ),
        )
    for column in ("session_id", "state", "source"):
        _add_index(
            "add_provisioning_events",
            f"ix_add_provisioning_events_{column}",
            [column],
        )

    if "add_provisioned_device_records" not in _tables():
        op.create_table(
            "add_provisioned_device_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("hardware_mac", sa.String(17), nullable=False, unique=True),
            sa.Column("derivation_version", sa.String(40), nullable=False),
            sa.Column("root_label", sa.String(80), nullable=False),
            sa.Column("efuse_purpose", sa.String(40), nullable=False),
            sa.Column("secure_boot_digests", sa.JSON(), nullable=False),
            sa.Column("hardware_profile", sa.String(80), nullable=False),
            sa.Column("bundle_hashes", sa.JSON(), nullable=False),
            sa.Column(
                "last_session_id",
                sa.Integer(),
                sa.ForeignKey("add_provisioning_sessions.id"),
                nullable=False,
            ),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _add_index(
        "add_provisioned_device_records",
        "ix_add_provisioned_device_records_hardware_mac",
        ["hardware_mac"],
        unique=True,
    )
    _add_index(
        "add_provisioned_device_records",
        "ix_add_provisioned_device_records_hardware_profile",
        ["hardware_profile"],
    )
    _add_index(
        "add_provisioned_device_records",
        "ix_add_provisioned_device_records_last_session_id",
        ["last_session_id"],
    )

    if "add_provisioning_companion_nonces" not in _tables():
        op.create_table(
            "add_provisioning_companion_nonces",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "companion_id",
                sa.Integer(),
                sa.ForeignKey("add_provisioning_companions.id"),
                nullable=False,
            ),
            sa.Column("nonce", sa.String(120), nullable=False),
            sa.Column("request_timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "companion_id", "nonce", name="uq_add_provisioning_companion_nonce"
            ),
        )
    _add_index(
        "add_provisioning_companion_nonces",
        "ix_add_provisioning_companion_nonces_companion_id",
        ["companion_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Provisioning and terminal-binding evidence is immutable; restore a pre-deploy backup."
    )

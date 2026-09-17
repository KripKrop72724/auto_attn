"""Add terminal families without renaming existing device tables."""
from alembic import op
import sqlalchemy as sa

revision = "20260917_0027"
down_revision = "20260916_0026"
branch_labels = None
depends_on = None


def upgrade():
    for name, size, default in (
        ("firmware_family", 16, "zkt"),
        ("terminal_vendor", 16, "zkt"),
        ("terminal_protocol", 24, "zkt_tcp"),
    ):
        op.add_column("add_connectors", sa.Column(
            name, sa.String(size), nullable=False, server_default=default,
        ))
    op.create_table(
        "add_hikvision_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column("terminal_serial", sa.String(120), nullable=False),
        sa.Column("source_epoch", sa.String(64), nullable=False),
        sa.Column("observation_sha256", sa.String(64), nullable=False),
        sa.Column("event_uid", sa.String(64)),
        sa.Column("immutable_digest", sa.String(64)),
        sa.Column("source_event_id", sa.String(32)),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("raw_encrypted", sa.Text(), nullable=False),
        sa.Column("captured_epoch", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connector_id", "observation_sha256", name="uq_add_hikvision_evidence_observation"),
    )
    for field in ("connector_id", "event_uid", "disposition"):
        op.create_index(f"ix_add_hikvision_evidence_{field}", "add_hikvision_evidence", [field])


def downgrade():
    # Family ownership must survive application rollback. Removing it would
    # allow a Hikvision connector to be reinterpreted as a legacy ZKT device.
    raise RuntimeError("Terminal-family migration is additive and cannot be downgraded")

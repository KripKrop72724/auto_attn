"""Preserve protected firmware queue evidence before custody acknowledgement."""
from alembic import op
import sqlalchemy as sa

revision = "20260916_0025"
down_revision = "20260916_0024"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("add_queue_evidence"):
        return
    op.create_table(
        "add_queue_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column("queue", sa.String(40), nullable=False),
        sa.Column("queue_generation", sa.String(80), nullable=False),
        sa.Column("record_id", sa.String(120), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("provenance_digest", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("protected_evidence", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("connector_id", "queue", "queue_generation", "record_id",
                            name="uq_add_queue_evidence_identity"),
    )
    op.create_index("ix_add_queue_evidence_receipt_id", "add_queue_evidence", ["receipt_id"], unique=True)
    op.create_index("ix_add_queue_evidence_connector_id", "add_queue_evidence", ["connector_id"])


def downgrade():
    op.drop_table("add_queue_evidence")

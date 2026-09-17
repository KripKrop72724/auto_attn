"""Separate Hikvision source checkpoints and committed page receipts."""
from alembic import op
import sqlalchemy as sa

revision = "20260917_0029"
down_revision = "20260917_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "add_hikvision_reconciliation_states",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), primary_key=True),
        sa.Column("source_epoch", sa.String(64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "add_hikvision_reconciliation_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
        sa.Column("token", sa.String(36), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.UniqueConstraint("job_id", "token", name="uq_hikvision_job_page_token"),
    )
    op.create_index("ix_add_hikvision_reconciliation_pages_job_id", "add_hikvision_reconciliation_pages", ["job_id"])


def downgrade():
    raise RuntimeError("Committed source evidence must survive application rollback")

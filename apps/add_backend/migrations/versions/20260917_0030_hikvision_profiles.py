"""Encrypted, bounded Hikvision profile snapshot staging."""
from alembic import op
import sqlalchemy as sa

revision = "20260917_0030"
down_revision = "20260917_0029"
branch_labels = None
depends_on = None


def upgrade():
    if "add_hikvision_profile_scans" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "add_hikvision_profile_scans",
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), primary_key=True),
        sa.Column("snapshot_id", sa.String(32), nullable=False),
        sa.Column("phase", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("data_encrypted", sa.Text(), nullable=False),
        sa.Column("last_page_digest", sa.String(64), nullable=False),
    )


def downgrade():
    raise RuntimeError("Profile staging is retained across application rollback")

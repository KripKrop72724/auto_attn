"""ADD-owned Hikvision source classification policy."""
from alembic import op
import sqlalchemy as sa

revision = "20260917_0028"
down_revision = "20260917_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "add_hikvision_policies",
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("terminal_serial", sa.String(120), nullable=False),
        sa.Column("source_epoch", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(80), nullable=False),
        sa.Column("mapping_revision", sa.Integer(), nullable=False),
        sa.Column("success_codes", sa.JSON(), nullable=False),
        sa.Column("excluded_codes", sa.JSON(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Hikvision identity evidence must survive application rollback")

"""Store optional versioned firmware durability diagnostics."""
from alembic import op
import sqlalchemy as sa

revision = "20260916_0024"
down_revision = "20260904_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The repository's initial migration creates current model metadata on a
    # fresh database; existing production databases still require these adds.
    columns = {row["name"] for row in sa.inspect(op.get_bind()).get_columns("add_connectors")}
    if "firmware_diagnostics" not in columns:
        op.add_column("add_connectors", sa.Column("firmware_diagnostics", sa.JSON(), nullable=True))
    if "firmware_diagnostics_at" not in columns:
        op.add_column("add_connectors", sa.Column("firmware_diagnostics_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("add_connectors", "firmware_diagnostics_at")
    op.drop_column("add_connectors", "firmware_diagnostics")

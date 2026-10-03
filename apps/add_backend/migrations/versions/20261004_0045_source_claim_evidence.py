"""Retain a rejected historical interpretation separately from source custody."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0045"
down_revision = "20261003_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "add_terminal_record_manifest"
    columns = {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}
    for name, kind in (
        ("declared_disposition", sa.String(50)),
        ("interpretation_version", sa.String(80)),
        ("protected_source_claim", sa.Text()),
    ):
        if name not in columns:
            op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    # A backend rollback must not discard the original interpretation evidence.
    pass

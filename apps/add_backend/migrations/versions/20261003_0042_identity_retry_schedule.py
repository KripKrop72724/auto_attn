"""Schedule unchanged identity holds instead of continuously rereading them."""
from alembic import op
import sqlalchemy as sa

revision = "20261003_0042"
down_revision = "20260929_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "add_attendance_events"
    inspector = sa.inspect(op.get_bind())
    columns = {row["name"] for row in inspector.get_columns(table)}
    if "identity_checked_revision" not in columns:
        op.add_column(table, sa.Column("identity_checked_revision", sa.Integer(), nullable=True))
    if "identity_retry_after" not in columns:
        op.add_column(table, sa.Column("identity_retry_after", sa.DateTime(timezone=True), nullable=True))
    indexes = {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table)}
    if "ix_add_attendance_events_identity_retry_after" not in indexes:
        op.create_index("ix_add_attendance_events_identity_retry_after", table, ["identity_retry_after"])


def downgrade() -> None:
    # Additive scheduling evidence remains available to the compatibility reader.
    pass

"""Let operators archive finished reconciles and firmware campaigns.

Old reconcile jobs and firmware campaigns crowd the dashboards. Many of them
still carry evidence that later checks read: a reconcile job's coverage and
terminal-record manifest prove where saved punches came from, and a campaign's
deployments prove which image a connector last installed. Archiving hides a
finished row from the dashboards without touching that evidence; only rows no
check reads are deleted outright by the application.

Additive and metadata-only on PostgreSQL: nullable columns without defaults,
so neither table is rewritten. Existing rows stay visible (archived_at NULL).
"""

from alembic import op
import sqlalchemy as sa

revision = "20261023_0057_archive_history"
down_revision = "20261022_0056_dump_release"
branch_labels = None
depends_on = None

TABLES = ("add_reconciliation_jobs", "add_firmware_campaigns")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        columns = {row["name"] for row in inspector.get_columns(table)}
        if "archived_at" not in columns:
            op.add_column(table, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        if "archived_by" not in columns:
            op.add_column(table, sa.Column("archived_by", sa.String(120), nullable=True))


def downgrade() -> None:
    # The previous release ignores the columns; archived rows reappear there.
    pass

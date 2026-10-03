"""Retain explicit interpretation obligations for raw canonical source custody."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0047"
down_revision = "20261004_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("add_reconciliation_jobs", "add_reconciliation_chunks",
                  "add_source_tail_chunks", "add_reconciliation_coverage"):
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        if "raw_preserved_count" not in columns:
            op.add_column(table, sa.Column("raw_preserved_count", sa.Integer(), nullable=False, server_default="0"))
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("add_zkt_custody_work")}
    if "source_manifest_id" not in columns:
        with op.batch_alter_table("add_zkt_custody_work") as batch:
            batch.add_column(sa.Column("source_manifest_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_add_zkt_work_source_manifest", "add_terminal_record_manifest",
                                     ["source_manifest_id"], ["id"])
            batch.create_unique_constraint("uq_add_zkt_work_source_manifest", ["source_manifest_id"])


def downgrade() -> None:
    # An older reader must not erase retained source-processing obligations.
    pass

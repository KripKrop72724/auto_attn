"""Recognise attendance batches that firmware re-sends with a new capture time.

Every ZKT firmware generation rebuilds its reconcile batches ("truth-<first
event_uid>") on each truth pass and stamps every row with the time it was
serialised. ADD compared whole payloads, so each pass of already-settled
punches became an ATTENDANCE_BATCH_ID_CONFLICT quarantine. The receipt now
keeps a digest of its events without that per-send field, so a replay can be
acknowledged with the receipt it repeats.

Additive and metadata-only on PostgreSQL: one nullable column without a
default or an index, so the table is not rewritten. Existing receipts keep
NULL until a replay derives the value from the receipt's own evidence.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261022_0054_batch_replay"
down_revision = "20261012_0053"
branch_labels = None
depends_on = None

TABLE = "add_attendance_batch_receipts"
COLUMN = "content_digest"


def upgrade() -> None:
    columns = {row["name"] for row in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))


def downgrade() -> None:
    # Additive replay evidence stays readable by the previous release, which
    # ignores the column.
    pass

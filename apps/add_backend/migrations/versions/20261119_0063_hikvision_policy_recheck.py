"""Cursor for re-checking stored Hikvision events after a policy change.

Adds two nullable columns to add_hikvision_policies (a handful of rows, one
per Hikvision terminal). No data is written: existing policies start idle
(NULL cursor), so the deploy itself re-checks nothing; the sweep starts only
when an operator next saves a device's event-code policy.

Downgrade drops the columns; the previous release does not read them.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0063_hik_recheck"
down_revision = "20261105_0062_unknown_auto_rel"
branch_labels = None
depends_on = None

TABLE = "add_hikvision_policies"
COLUMNS = (
    ("recheck_after_id", sa.Integer()),
    ("recheck_counts", sa.JSON()),
)


def _existing() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    present = _existing()
    for name, kind in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    present = _existing()
    with op.batch_alter_table(TABLE) as batch:
        for name, _kind in reversed(COLUMNS):
            if name in present:
                batch.drop_column(name)

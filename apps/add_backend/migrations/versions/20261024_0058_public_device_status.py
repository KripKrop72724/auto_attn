"""Online/Offline state and its start time for the public status page.

Creates one new, empty table. No existing table is altered, scanned or locked:
the table has no foreign key, so creating it takes no lock on add_connectors.
The page fills it on its first refresh after deploy.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261024_0058_public_status"
down_revision = "20261023_0057_archive_history"
branch_labels = None
depends_on = None

TABLE = "add_public_device_status"


def upgrade() -> None:
    if TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("connector_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("status", sa.String(length=10), nullable=False),
            sa.Column("since", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    # The previous release ignores the table.
    pass

"""Latch the redo-area OTA floor on the connector row (ZKT 2.7.7).

The floor check read only the latest heartbeat (connector.firmware_diagnostics),
so a device that journaled redo-area work and then rolled back to 2.5.2
overwrote that evidence with heartbeats carrying none, and ADD would offer
2.6.25 or 2.6.27 again to a device whose redo area may hold custody records.
This column keeps the floor once any heartbeat or progress report has shown
it; no later heartbeat clears it (ZKT 2.7.7 design, 14.1 item 3).

Additive and metadata-only on PostgreSQL: one nullable column without a
default, so the table is not rewritten.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261026_0059_ota_floor_latch"
down_revision = "20261024_0058_public_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {row["name"] for row in inspector.get_columns("add_connectors")}
    if "ota_floor_latched_at" not in columns:
        op.add_column("add_connectors", sa.Column("ota_floor_latched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # The previous release ignores the column; its heartbeat-fresh check keeps
    # working, only without the latch.
    pass

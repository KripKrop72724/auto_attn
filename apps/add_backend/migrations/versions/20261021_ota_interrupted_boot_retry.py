"""Index ESP heartbeat telemetry by connector and time.

Reset history, rollback evidence and peer resets read one ESP's samples in a
time window. add-api runs this migration before it starts (no heartbeat or
punch is ingested meanwhile, and deploy.ps1 waits at most 240 s for health),
so the index is built here only while the table is small. A production-sized
table is indexed by the operator beforehand with CREATE INDEX CONCURRENTLY
while ADD keeps running (docs/zone-lite-remote-firmware-updates.md); this
migration then finds the valid index and does nothing. If the index is still
missing, ADD works without it (those reads are slower) and the migration logs
the operator command instead of stalling the deploy. An INVALID index left by
an interrupted concurrent build is dropped first.
"""

import logging

from alembic import op
from sqlalchemy import text

revision = "20261021_ota_boot_retry"
down_revision = "20261012_0053"
branch_labels = None
depends_on = None

INDEX = "ix_add_device_telemetry_connector_created"
TABLE = "add_device_telemetry"
# A heap this size is read in seconds; a larger one is the operator's
# concurrent build, never the deploy's.
INLINE_BUILD_LIMIT_BYTES = 512 * 1024 * 1024
OPERATOR_COMMAND = (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON {TABLE} (connector_id, created_at)")


def inline_build(heap_bytes: int | None) -> bool:
    """Whether the deploy may build the index itself."""
    return heap_bytes is not None and 0 <= heap_bytes <= INLINE_BUILD_LIMIT_BYTES


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.create_index(INDEX, TABLE, ["connector_id", "created_at"])
        return
    # to_regclass follows search_path, so only this schema's index counts.
    valid = bind.execute(text(
        "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"), {"name": INDEX}).first()
    if valid is not None and valid[0]:
        return  # built beforehand by the operator
    if valid is not None:
        op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    heap_bytes = bind.execute(text("SELECT pg_relation_size(to_regclass(:name))"), {"name": TABLE}).scalar()
    if not inline_build(heap_bytes):
        logging.getLogger("alembic").warning(
            "%s is %s bytes; not building %s during the deploy. Run while ADD is up: %s",
            TABLE, heap_bytes, INDEX, OPERATOR_COMMAND)
        return
    op.create_index(INDEX, TABLE, ["connector_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index(INDEX, table_name=TABLE)
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")

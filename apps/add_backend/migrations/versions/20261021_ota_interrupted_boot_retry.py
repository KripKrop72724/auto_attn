"""Index ESP heartbeat telemetry by connector and time.

Reset history, rollback evidence and peer resets read one ESP's samples in a
time window. Built CONCURRENTLY on PostgreSQL so heartbeat inserts are never
blocked; a build interrupted earlier leaves an INVALID index, which is
dropped and rebuilt.
"""

from alembic import op
from sqlalchemy import text

revision = "20261021_ota_boot_retry"
down_revision = "20261012_0053"
branch_labels = None
depends_on = None

INDEX = "ix_add_device_telemetry_connector_created"
TABLE = "add_device_telemetry"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.create_index(INDEX, TABLE, ["connector_id", "created_at"])
        return
    with op.get_context().autocommit_block():
        invalid = bind.execute(text(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND NOT i.indisvalid"), {"name": INDEX}).first()
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.create_index(INDEX, TABLE, ["connector_id", "created_at"], postgresql_concurrently=True,
                        if_not_exists=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.drop_index(INDEX, table_name=TABLE)
        return
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name=TABLE, postgresql_concurrently=True, if_exists=True)

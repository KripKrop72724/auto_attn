"""The owner's switch for releasing unknown-identity punches automatically.

Creates one new singleton table and seeds its row ON (the owner's default).
No existing table is altered, scanned or locked: the table has no foreign key.
The release reads held punches newest first through
ix_add_attendance_unacked_id (migration 0061) and its runs' items through
their existing job and attendance-event indexes, so no new index is built
during the deploy.

Downgrade drops the table; the previous release does not read it.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261105_0062_unknown_auto_rel"
down_revision = "20261105_0061_dashboard_indexes"
branch_labels = None
depends_on = None

TABLE = "add_unknown_identity_auto_release"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("changed_by", sa.String(length=120)),
            sa.Column("changed_at", sa.DateTime(timezone=True)),
            sa.Column("reason_encrypted", sa.Text()),
            sa.Column("control_key", sa.String(length=120)),
            sa.Column("drain_requested_at", sa.DateTime(timezone=True)),
            sa.Column("drain_requested_by", sa.String(length=120)),
            sa.Column("drain_key", sa.String(length=120)),
            sa.Column("drain_completed_at", sa.DateTime(timezone=True)),
            sa.Column("scan_head_id", sa.Integer()),
            sa.Column("scan_cursor_id", sa.Integer()),
            sa.Column("scan_started_at", sa.DateTime(timezone=True)),
            sa.Column("scan_idle_until", sa.DateTime(timezone=True)),
            sa.Column("backlog_count", sa.Integer()),
            sa.Column("backlog_counted_at", sa.DateTime(timezone=True)),
            sa.Column("stats", sa.JSON()),
            sa.Column("stats_counted_at", sa.DateTime(timezone=True)),
            sa.Column("last_batch_at", sa.DateTime(timezone=True)),
            sa.Column("last_batch_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_batch_job_id", sa.String(length=36)),
            sa.Column("batches_created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("punches_submitted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error_code", sa.String(length=80)),
            sa.Column("last_error_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    # ON is the owner's default; a row an earlier attempt seeded is kept.
    op.execute(sa.text(
        f"INSERT INTO {TABLE} (id, enabled, updated_at) "
        "SELECT 1, :enabled, CURRENT_TIMESTAMP "
        f"WHERE NOT EXISTS (SELECT 1 FROM {TABLE} WHERE id = 1)"
    ).bindparams(enabled=True))


def downgrade() -> None:
    op.drop_table(TABLE)

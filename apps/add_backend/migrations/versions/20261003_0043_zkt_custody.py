"""Add raw observation custody without enabling any connector or new writer."""
from alembic import op
import sqlalchemy as sa

revision = "20261003_0043"
down_revision = "20261003_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktObservationReceipt, ZktOccurrenceAlias, ZktObservationLink
    bind = op.get_bind()
    if "zkt_custody_enabled" not in {c["name"] for c in sa.inspect(bind).get_columns("add_connectors")}:
        op.add_column("add_connectors", sa.Column("zkt_custody_enabled", sa.Boolean(),
                                                  nullable=False, server_default=sa.false()))
    for model in (ZktObservationReceipt, ZktOccurrenceAlias, ZktObservationLink):
        model.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    # A firmware rollback must not destroy custody or source evidence.
    pass

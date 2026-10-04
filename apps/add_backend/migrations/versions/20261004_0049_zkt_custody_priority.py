"""Bound live inspection bursts while preserving historical progress."""
from alembic import op

revision = "20261004_0049"
down_revision = "20261004_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktCustodySchedule, ZktCustodyWork
    bind = op.get_bind()
    ZktCustodySchedule.__table__.create(bind, checkfirst=True)
    for name in ("ix_add_zkt_work_recent_live", "ix_add_zkt_work_revision_hold"):
        next(index for index in ZktCustodyWork.__table__.indexes
             if index.name == name).create(bind, checkfirst=True)


def downgrade() -> None:
    # Preserve fairness and all custody/decoder evidence on backend rollback.
    pass

"""Add explicit derived work obligations without activating journal delivery."""
from alembic import op

revision = "20261003_0044"
down_revision = "20261003_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktCustodyWork, ZktCustodyWorkReceipt
    bind = op.get_bind()
    for model in (ZktCustodyWork, ZktCustodyWorkReceipt):
        model.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    # Firmware/backend rollback must retain the obligation and its evidence.
    pass

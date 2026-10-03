"""Retain ADD-owned delivery intents and scoped content receipts without activation."""
from alembic import op

revision = "20261004_0046"
down_revision = "20261004_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktOracleIntent, ZktOracleContentReceipt
    for model in (ZktOracleIntent, ZktOracleContentReceipt):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Firmware/backend rollback must not discard a frozen payload or receipt.
    pass

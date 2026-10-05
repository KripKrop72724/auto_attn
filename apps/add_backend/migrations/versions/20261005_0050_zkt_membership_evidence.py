"""Keep experimental membership confirmation separate from full projection proof."""
from alembic import op
import sqlalchemy as sa

revision = "20261005_0050"
down_revision = "20261004_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktOracleMembershipReceipt
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("add_zkt_oracle_intents")}
    if "verification_scope" not in columns:
        op.add_column("add_zkt_oracle_intents", sa.Column("verification_scope", sa.String(80),
            nullable=False, server_default="ORACLE_RAW_DAY_TIMES_V2"))
    ZktOracleMembershipReceipt.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    # A backend rollback must retain route authority, frozen payloads and receipts.
    pass

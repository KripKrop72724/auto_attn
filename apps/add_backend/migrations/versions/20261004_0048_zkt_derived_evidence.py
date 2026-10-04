"""Retain versioned decoder evidence without changing original source claims."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0048"
down_revision = "20261004_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktCustodyWork, ZktDerivedEvidence
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("add_zkt_custody_work")}
    if "interpretation_version" not in columns:
        op.add_column("add_zkt_custody_work", sa.Column("interpretation_version", sa.String(80), nullable=True))
    next(index for index in ZktCustodyWork.__table__.indexes
         if index.name == "ix_add_zkt_work_interpretation").create(bind, checkfirst=True)
    ZktDerivedEvidence.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    # Backend rollback cannot erase the derivation or its original custody.
    pass

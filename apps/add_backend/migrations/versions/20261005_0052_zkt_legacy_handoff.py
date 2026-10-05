"""Retain the checked legacy custody handoff alongside its source permit."""
from alembic import op

revision = "20261005_0052"
down_revision = "20261005_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktLegacyHandoff
    ZktLegacyHandoff.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Never remove the authority for retained attendance during application rollback.
    pass

"""Keep derived attendance separate from immutable raw source custody."""
from alembic import op

revision = "20261005_0051"
down_revision = "20261005_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from zk_add.models import ZktSourceCutover, ZktSourceAttendance
    bind = op.get_bind()
    ZktSourceCutover.__table__.create(bind, checkfirst=True)
    ZktSourceAttendance.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    # Receipt ownership, source boundaries and canonical identities survive rollback.
    pass

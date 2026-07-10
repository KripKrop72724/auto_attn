"""Create the standalone Attendance Device Dashboard schema.

Revision ID: 20260710_0001
Revises:
Create Date: 2026-07-10

The first migration intentionally creates the exact SQLAlchemy metadata snapshot. Future
schema changes must be separate Alembic revisions; production never runs create_all.
"""

from alembic import op

from zk_add.db import Base
import zk_add.models  # noqa: F401


revision = "20260710_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)

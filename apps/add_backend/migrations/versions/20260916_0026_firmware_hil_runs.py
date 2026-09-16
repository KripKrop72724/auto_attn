"""Durable exact-target ownership for production HIL observations."""

from alembic import op
import sqlalchemy as sa

revision = "20260916_0026"
down_revision = "20260916_0025"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("add_firmware_hil_runs"):
        return
    op.create_table(
        "add_firmware_hil_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column(
            "deployment_id",
            sa.Integer(),
            sa.ForeignKey("add_firmware_deployments.id"),
            nullable=False,
        ),
        sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
        sa.Column(
            "release_id", sa.Integer(), sa.ForeignKey("add_firmware_releases.id"), nullable=False
        ),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("release_identity", sa.JSON(), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.UniqueConstraint("actor", "idempotency_key", name="uq_add_hil_run_actor_key"),
    )
    for field in ("run_id", "deployment_id", "connector_id", "release_id", "status"):
        op.create_index(
            f"ix_add_firmware_hil_runs_{field}",
            "add_firmware_hil_runs",
            [field],
            unique=field == "run_id",
        )
    op.create_index(
        "uq_add_hil_run_active_connector",
        "add_firmware_hil_runs",
        ["connector_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OBSERVING'"),
        sqlite_where=sa.text("status = 'OBSERVING'"),
    )


def downgrade():
    op.drop_table("add_firmware_hil_runs")

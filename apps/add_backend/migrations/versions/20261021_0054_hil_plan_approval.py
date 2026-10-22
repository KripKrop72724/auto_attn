"""One-approval staged HIL plans (owner decision, 21 October 2026).

Additive: one new table. Existing campaigns, deployments and releases are
unchanged; a plan only links to the campaigns it creates by their actor and
idempotency key.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_0054_hil_plan"
down_revision = "20261012_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("add_firmware_hil_plans"):
        return
    op.create_table(
        "add_firmware_hil_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.String(32), nullable=False),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("add_firmware_releases.id"), nullable=False),
        sa.Column("release_public_id", sa.String(100), nullable=False),
        sa.Column("release_version", sa.String(80), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("application_sha256", sa.String(64), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("seal", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("open_guard", sa.String(8), nullable=True),
        sa.Column("current_stage", sa.Integer(), nullable=False),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_reason", sa.String(200), nullable=True),
        sa.Column("halt_code", sa.String(120), nullable=True),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("approved_by", "idempotency_key", name="uq_add_hil_plan_actor_key"),
        sa.UniqueConstraint("open_guard", name="add_firmware_hil_plans_open_guard_key"),
    )
    op.create_index("ix_add_firmware_hil_plans_plan_id", "add_firmware_hil_plans", ["plan_id"], unique=True)
    op.create_index("ix_add_firmware_hil_plans_release_id", "add_firmware_hil_plans", ["release_id"])
    op.create_index("ix_add_firmware_hil_plans_status", "add_firmware_hil_plans", ["status"])


def downgrade() -> None:
    op.drop_table("add_firmware_hil_plans")

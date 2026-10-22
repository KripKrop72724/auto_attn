"""Employee check-in phone notification outbox and runtime switch.

Creates two new, empty tables and their indexes only. No existing table is
altered, scanned, locked or backfilled: punches confirmed before this feature
have no notification row and are shown as "not sent". The new tables carry no
foreign keys, so creating them never takes a lock on the attendance table.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0054"
down_revision = "20261012_0053"
branch_labels = None
depends_on = None

NOTIFICATIONS = "add_attendance_checkin_notifications"
CONTROL = "add_checkin_notify_control"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if NOTIFICATIONS not in tables:
        op.create_table(
            NOTIFICATIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("attendance_event_id", sa.Integer(), nullable=False),
            sa.Column("connector_id", sa.Integer(), nullable=False),
            sa.Column("punch_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confirmation_path", sa.String(length=40), nullable=True),
            sa.Column("cnic_encrypted", sa.Text(), nullable=True),
            sa.Column("cnic_lookup_hash", sa.String(length=64), nullable=True),
            sa.Column("cnic_last4", sa.String(length=4), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("reason", sa.String(length=60), nullable=True),
            sa.Column("detail", sa.String(length=200), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("attempt_base", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_http_status", sa.Integer(), nullable=True),
            sa.Column("host_used", sa.String(length=20), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("grouped_into_id", sa.Integer(), nullable=True),
            sa.Column(
                "manual_retry", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("retry_deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("attendance_event_id", name="uq_add_checkin_notify_event"),
        )
        op.create_index("ix_add_checkin_notify_due", NOTIFICATIONS, ["status", "next_attempt_at"])
        op.create_index(
            "ix_add_checkin_notify_cnic_time", NOTIFICATIONS, ["cnic_lookup_hash", "punch_time"]
        )
        op.create_index(
            "ix_add_checkin_notify_connector", NOTIFICATIONS, ["connector_id", "status"]
        )
        op.create_index("ix_add_checkin_notify_created", NOTIFICATIONS, ["created_at"])
    if CONTROL not in tables:
        op.create_table(
            CONTROL,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mode", sa.String(length=10), nullable=False),
            sa.Column("punch_policy", sa.String(length=20), nullable=False),
            sa.Column("pilot_zone_ids", sa.JSON(), nullable=False),
            sa.Column("pilot_cnics", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("updated_by", sa.String(length=120), nullable=True),
            sa.Column("update_reason", sa.String(length=300), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_category", sa.String(length=60), nullable=True),
            sa.Column("preferred_host", sa.String(length=20), nullable=False),
            sa.Column("primary_reprobe_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if CONTROL in tables:
        op.drop_table(CONTROL)
    if NOTIFICATIONS in tables:
        for name in (
            "ix_add_checkin_notify_created",
            "ix_add_checkin_notify_connector",
            "ix_add_checkin_notify_cnic_time",
            "ix_add_checkin_notify_due",
        ):
            op.drop_index(name, table_name=NOTIFICATIONS)
        op.drop_table(NOTIFICATIONS)

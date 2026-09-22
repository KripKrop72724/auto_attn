"""Add append-only attendance recovery jobs and source correction lineage.

Revision ID: 20260922_0033
Revises: 20260922_0032
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260922_0033"
down_revision = "20260922_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "add_attendance_recovery_jobs" not in tables:
        op.create_table(
            "add_attendance_recovery_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(36), nullable=False, unique=True),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="QUEUED"),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("candidate_digest", sa.String(64), nullable=False),
            sa.Column("preview_expires_at", sa.DateTime(timezone=True)),
            sa.Column("lease_owner", sa.String(120)),
            sa.Column("lease_until", sa.DateTime(timezone=True)),
            sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("identity_held_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_add_attendance_recovery_jobs_job_id", "add_attendance_recovery_jobs", ["job_id"], unique=True)
        op.create_index("ix_add_attendance_recovery_jobs_action", "add_attendance_recovery_jobs", ["action"])
        op.create_index("ix_add_attendance_recovery_jobs_status", "add_attendance_recovery_jobs", ["status"])
        op.create_index("ix_add_attendance_recovery_jobs_candidate_digest", "add_attendance_recovery_jobs", ["candidate_digest"])
        op.create_index("ix_add_attendance_recovery_jobs_lease_owner", "add_attendance_recovery_jobs", ["lease_owner"])
        op.create_index("ix_add_attendance_recovery_jobs_lease_until", "add_attendance_recovery_jobs", ["lease_until"])
        op.create_index("ix_add_attendance_recovery_jobs_status_updated", "add_attendance_recovery_jobs", ["status", "updated_at"])

    if "add_attendance_recovery_items" not in tables:
        op.create_table(
            "add_attendance_recovery_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.String(36), nullable=False, unique=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_attendance_recovery_jobs.id"), nullable=False),
            sa.Column("source_kind", sa.String(40), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id")),
            sa.Column("attendance_event_id", sa.Integer(), sa.ForeignKey("add_attendance_events.id")),
            sa.Column("manifest_id", sa.Integer(), sa.ForeignKey("add_terminal_record_manifest.id")),
            sa.Column("hikvision_evidence_id", sa.Integer()),
            sa.Column("expected_state_digest", sa.String(64), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="PENDING"),
            sa.Column("lane", sa.String(40), nullable=False),
            sa.Column("outcome", sa.String(80)),
            sa.Column("error_code", sa.String(120)),
            sa.Column("error_message", sa.Text()),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("corrected_device_time", sa.DateTime(timezone=True)),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "source_kind", "source_ref", name="uq_add_attendance_recovery_item_source"),
        )
        op.create_index("ix_add_attendance_recovery_items_item_id", "add_attendance_recovery_items", ["item_id"], unique=True)
        op.create_index("ix_add_attendance_recovery_items_job_id", "add_attendance_recovery_items", ["job_id"])
        op.create_index("ix_add_attendance_recovery_items_source_kind", "add_attendance_recovery_items", ["source_kind"])
        op.create_index("ix_add_attendance_recovery_items_source_ref", "add_attendance_recovery_items", ["source_ref"])
        op.create_index("ix_add_attendance_recovery_items_connector_id", "add_attendance_recovery_items", ["connector_id"])
        op.create_index("ix_add_attendance_recovery_items_attendance_event_id", "add_attendance_recovery_items", ["attendance_event_id"])
        op.create_index("ix_add_attendance_recovery_items_manifest_id", "add_attendance_recovery_items", ["manifest_id"])
        op.create_index("ix_add_attendance_recovery_items_hikvision_evidence_id", "add_attendance_recovery_items", ["hikvision_evidence_id"])
        op.create_index("ix_add_attendance_recovery_items_status", "add_attendance_recovery_items", ["status"])
        op.create_index("ix_add_attendance_recovery_items_lane", "add_attendance_recovery_items", ["lane"])
        op.create_index("ix_add_attendance_recovery_items_job_status", "add_attendance_recovery_items", ["job_id", "status"])

    if "add_attendance_source_corrections" not in tables:
        op.create_table(
            "add_attendance_source_corrections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("correction_id", sa.String(36), nullable=False, unique=True),
            sa.Column("source_kind", sa.String(40), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
            sa.Column("manifest_id", sa.Integer(), sa.ForeignKey("add_terminal_record_manifest.id")),
            sa.Column("hikvision_evidence_id", sa.Integer()),
            sa.Column("original_digest", sa.String(64), nullable=False),
            sa.Column("correction_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("corrected_device_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="CREATED"),
            sa.Column("derived_event_uid", sa.String(128)),
            sa.Column("derived_attendance_event_id", sa.Integer(), sa.ForeignKey("add_attendance_events.id")),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("source_kind", "source_ref", "correction_version", name="uq_add_attendance_source_correction_version"),
        )
        op.create_index("ix_add_attendance_source_corrections_correction_id", "add_attendance_source_corrections", ["correction_id"], unique=True)
        op.create_index("ix_add_attendance_source_corrections_source_kind", "add_attendance_source_corrections", ["source_kind"])
        op.create_index("ix_add_attendance_source_corrections_source_ref", "add_attendance_source_corrections", ["source_ref"])
        op.create_index("ix_add_attendance_source_corrections_connector_id", "add_attendance_source_corrections", ["connector_id"])
        op.create_index("ix_add_attendance_source_corrections_manifest_id", "add_attendance_source_corrections", ["manifest_id"])
        op.create_index("ix_add_attendance_source_corrections_hikvision_evidence_id", "add_attendance_source_corrections", ["hikvision_evidence_id"])
        op.create_index("ix_add_attendance_source_corrections_original_digest", "add_attendance_source_corrections", ["original_digest"])
        op.create_index("ix_add_attendance_source_corrections_status", "add_attendance_source_corrections", ["status"])
        op.create_index("ix_add_attendance_source_corrections_derived_event_uid", "add_attendance_source_corrections", ["derived_event_uid"])
        op.create_index("ix_add_att_src_corr_derived_event", "add_attendance_source_corrections", ["derived_attendance_event_id"])


def downgrade() -> None:
    # Recovery records are operational evidence and must survive a rollback.
    pass

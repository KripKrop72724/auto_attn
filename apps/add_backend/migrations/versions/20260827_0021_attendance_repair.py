"""Add durable employee attendance identity repair domain.

Revision ID: 20260827_0021
Revises: 20260825_0020
Create Date: 2026-08-27
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260827_0021"
down_revision = "20260825_0020"
branch_labels = None
depends_on = None


def _indexes(table: str, columns: list[str]) -> None:
    existing = {row["name"] for row in inspect(op.get_bind()).get_indexes(table)}
    for column in columns:
        name = f"ix_{table}_{column}"
        if name not in existing:
            op.create_index(name, table, [column])


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    if "add_audit_chain_head" not in tables:
        op.create_table(
            "add_audit_chain_head",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("last_audit_event_id", sa.Integer(), sa.ForeignKey("add_audit_events.id")),
            sa.Column("last_hash", sa.String(64)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        latest = (
            bind.execute(
                sa.text("SELECT id, row_hash FROM add_audit_events ORDER BY id DESC LIMIT 1")
            )
            .mappings()
            .first()
        )
        bind.execute(
            sa.text(
                "INSERT INTO add_audit_chain_head "
                "(id, last_audit_event_id, last_hash, updated_at) "
                "VALUES (1, :event_id, :last_hash, :updated_at)"
            ),
            {
                "event_id": latest["id"] if latest else None,
                "last_hash": latest["row_hash"] if latest else None,
                "updated_at": datetime.now(timezone.utc),
            },
        )

    if "add_attendance_repair_jobs" not in tables:
        op.create_table(
            "add_attendance_repair_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(36), nullable=False, unique=True),
            sa.Column(
                "connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False
            ),
            sa.Column(
                "zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False
            ),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("status", sa.String(40), nullable=False, server_default="PREPARING_SOURCE"),
            sa.Column("phase", sa.String(50), nullable=False, server_default="SOURCE_PREFLIGHT"),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("preview_digest", sa.String(64)),
            sa.Column("cohort_digest", sa.String(64)),
            sa.Column("source_certificate_digest", sa.String(64)),
            sa.Column(
                "source_reconciliation_job_id",
                sa.Integer(),
                sa.ForeignKey("add_reconciliation_jobs.id"),
            ),
            sa.Column("date_start_utc", sa.DateTime(timezone=True)),
            sa.Column("date_end_utc", sa.DateTime(timezone=True)),
            sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attention_event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "preparation_attempt_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column(
                "cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("preview_expires_at", sa.DateTime(timezone=True)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("first_oracle_mutation_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("wait_reason", sa.String(160)),
            sa.Column("error_code", sa.String(120)),
            sa.Column("error_message", sa.Text()),
            sa.Column("evidence_digest", sa.String(64)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "connector_id", "idempotency_key", name="uq_add_attendance_repair_job_idempotency"
            ),
        )
        _indexes(
            "add_attendance_repair_jobs",
            [
                "job_id",
                "connector_id",
                "zkt_device_id",
                "actor",
                "status",
                "phase",
                "request_digest",
                "preview_digest",
                "cohort_digest",
                "source_reconciliation_job_id",
                "date_start_utc",
                "date_end_utc",
                "preview_expires_at",
                "next_attempt_at",
                "wait_reason",
                "error_code",
            ],
        )
        active = sa.text("status not in ('COMPLETED','COMPLETED_WITH_ATTENTION','CANCELLED')")
        op.create_index(
            "uq_add_attendance_repair_active_connector",
            "add_attendance_repair_jobs",
            ["connector_id"],
            unique=True,
            postgresql_where=active,
            sqlite_where=active,
        )

    if "add_attendance_repair_targets" not in tables:
        op.create_table(
            "add_attendance_repair_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "job_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_jobs.id"),
                nullable=False,
            ),
            sa.Column(
                "device_user_id", sa.Integer(), sa.ForeignKey("add_device_users.id"), nullable=False
            ),
            sa.Column("user_key", sa.String(36), nullable=False),
            sa.Column("expected_row_version", sa.Integer(), nullable=False),
            sa.Column(
                "all_provable_history", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("selected_alias_tokens", sa.JSON(), nullable=False),
            sa.Column(
                "identity_snapshot_id",
                sa.Integer(),
                sa.ForeignKey("add_device_user_snapshots.id"),
                nullable=False,
            ),
            sa.Column("terminal_identity_fingerprint", sa.String(64)),
            sa.Column("desired_display_name_encrypted", sa.Text(), nullable=False),
            sa.Column("desired_cnic_encrypted", sa.Text(), nullable=False),
            sa.Column("desired_cnic_lookup_hash", sa.String(64), nullable=False),
            sa.Column("desired_cnic_last4", sa.String(4), nullable=False),
            sa.Column("desired_identity_digest", sa.String(64), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="FROZEN"),
            sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed_event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attention_event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("job_id", "device_user_id", name="uq_add_attendance_repair_target"),
        )
        _indexes(
            "add_attendance_repair_targets",
            [
                "job_id",
                "device_user_id",
                "user_key",
                "identity_snapshot_id",
                "desired_cnic_lookup_hash",
                "desired_identity_digest",
                "status",
            ],
        )

    if "add_attendance_repair_cohorts" not in tables:
        op.create_table(
            "add_attendance_repair_cohorts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "target_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_targets.id"),
                nullable=False,
            ),
            sa.Column("cohort_token", sa.String(64), nullable=False),
            sa.Column("evidence_classification", sa.String(60), nullable=False),
            sa.Column("source_device_user_id", sa.Integer(), sa.ForeignKey("add_device_users.id")),
            sa.Column("source_uid_digest", sa.String(64)),
            sa.Column("source_user_id_digest", sa.String(64), nullable=False),
            sa.Column("membership_digest", sa.String(64), nullable=False),
            sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("event_count", sa.Integer(), nullable=False),
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "target_id", "cohort_token", name="uq_add_attendance_repair_cohort_token"
            ),
        )
        _indexes(
            "add_attendance_repair_cohorts",
            [
                "target_id",
                "cohort_token",
                "evidence_classification",
                "source_device_user_id",
                "membership_digest",
            ],
        )

    if "add_attendance_repair_items" not in tables:
        op.create_table(
            "add_attendance_repair_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "job_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_jobs.id"),
                nullable=False,
            ),
            sa.Column(
                "target_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_targets.id"),
                nullable=False,
            ),
            sa.Column(
                "cohort_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_cohorts.id"),
                nullable=False,
            ),
            sa.Column(
                "attendance_event_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_events.id"),
                nullable=False,
            ),
            sa.Column("event_uid", sa.String(128), nullable=False),
            sa.Column("immutable_facts_digest", sa.String(64), nullable=False),
            sa.Column("source_ownership_digest", sa.String(64), nullable=False),
            sa.Column("before_device_user_id", sa.Integer()),
            sa.Column("before_display_name_encrypted", sa.Text()),
            sa.Column("before_cnic_encrypted", sa.Text()),
            sa.Column("before_cnic_lookup_hash", sa.String(64)),
            sa.Column("before_cnic_last4", sa.String(4)),
            sa.Column("before_identity_digest", sa.String(64), nullable=False),
            sa.Column("desired_display_name_encrypted", sa.Text(), nullable=False),
            sa.Column("desired_cnic_encrypted", sa.Text(), nullable=False),
            sa.Column("desired_cnic_lookup_hash", sa.String(64), nullable=False),
            sa.Column("desired_cnic_last4", sa.String(4), nullable=False),
            sa.Column("desired_identity_digest", sa.String(64), nullable=False),
            sa.Column(
                "oracle_classification", sa.String(40), nullable=False, server_default="NOT_CHECKED"
            ),
            sa.Column("expected_oracle_token_encrypted", sa.Text()),
            sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
            sa.Column("operation_payload_digest", sa.String(64)),
            sa.Column("state", sa.String(40), nullable=False, server_default="FROZEN"),
            sa.Column("outcome", sa.String(60)),
            sa.Column("lease_owner", sa.String(120)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("oracle_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("downstream_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("last_http_status", sa.Integer()),
            sa.Column("error_code", sa.String(120)),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "job_id", "attendance_event_id", name="uq_add_attendance_repair_job_event"
            ),
        )
        _indexes(
            "add_attendance_repair_items",
            [
                "job_id",
                "target_id",
                "cohort_id",
                "attendance_event_id",
                "event_uid",
                "operation_id",
                "oracle_classification",
                "state",
                "outcome",
                "lease_owner",
                "lease_expires_at",
                "next_attempt_at",
                "error_code",
            ],
        )

    if "add_attendance_identity_revisions" not in tables:
        op.create_table(
            "add_attendance_identity_revisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "attendance_event_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_events.id"),
                nullable=False,
            ),
            sa.Column(
                "repair_item_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_items.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column(
                "effective_device_user_id",
                sa.Integer(),
                sa.ForeignKey("add_device_users.id"),
                nullable=False,
            ),
            sa.Column("display_name_encrypted", sa.Text(), nullable=False),
            sa.Column("cnic_encrypted", sa.Text(), nullable=False),
            sa.Column("cnic_lookup_hash", sa.String(64), nullable=False),
            sa.Column("cnic_last4", sa.String(4), nullable=False),
            sa.Column("identity_digest", sa.String(64), nullable=False),
            sa.Column("state", sa.String(30), nullable=False, server_default="PENDING"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True)),
            sa.Column("superseded_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "attendance_event_id",
                "sequence",
                name="uq_add_attendance_identity_revision_sequence",
            ),
        )
        _indexes(
            "add_attendance_identity_revisions",
            [
                "attendance_event_id",
                "repair_item_id",
                "effective_device_user_id",
                "cnic_lookup_hash",
                "identity_digest",
                "state",
            ],
        )
        active_revision = sa.text("state = 'ACTIVE'")
        op.create_index(
            "uq_add_attendance_identity_active_revision",
            "add_attendance_identity_revisions",
            ["attendance_event_id"],
            unique=True,
            postgresql_where=active_revision,
            sqlite_where=active_revision,
        )

    attendance_columns = {row["name"] for row in inspect(bind).get_columns("add_attendance_events")}
    if "effective_identity_revision_id" not in attendance_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("add_attendance_events") as batch:
                batch.add_column(sa.Column("effective_identity_revision_id", sa.Integer()))
                batch.create_foreign_key(
                    "fk_add_attendance_effective_identity_revision",
                    "add_attendance_identity_revisions",
                    ["effective_identity_revision_id"],
                    ["id"],
                )
                batch.create_index(
                    "ix_add_attendance_events_effective_identity_revision_id",
                    ["effective_identity_revision_id"],
                )
        else:
            op.add_column(
                "add_attendance_events", sa.Column("effective_identity_revision_id", sa.Integer())
            )
            op.create_foreign_key(
                "fk_add_attendance_effective_identity_revision",
                "add_attendance_events",
                "add_attendance_identity_revisions",
                ["effective_identity_revision_id"],
                ["id"],
                use_alter=True,
            )
            op.create_index(
                "ix_add_attendance_events_effective_identity_revision_id",
                "add_attendance_events",
                ["effective_identity_revision_id"],
            )
    if "identity_content_status" not in attendance_columns:
        op.add_column(
            "add_attendance_events",
            sa.Column(
                "identity_content_status",
                sa.String(40),
                nullable=False,
                server_default="NOT_CHECKED",
            ),
        )
        op.create_index(
            "ix_add_attendance_events_identity_content_status",
            "add_attendance_events",
            ["identity_content_status"],
        )
    if "identity_content_confirmed_at" not in attendance_columns:
        op.add_column(
            "add_attendance_events",
            sa.Column("identity_content_confirmed_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_add_attendance_events_identity_content_confirmed_at",
            "add_attendance_events",
            ["identity_content_confirmed_at"],
        )
    if "identity_downstream_confirmed_at" not in attendance_columns:
        op.add_column(
            "add_attendance_events",
            sa.Column("identity_downstream_confirmed_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_add_attendance_events_identity_downstream_confirmed_at",
            "add_attendance_events",
            ["identity_downstream_confirmed_at"],
        )

    if "add_oracle_identity_repair_receipts" not in tables:
        op.create_table(
            "add_oracle_identity_repair_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "repair_item_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_items.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
            sa.Column("payload_digest", sa.String(64), nullable=False),
            sa.Column("action", sa.String(30), nullable=False),
            sa.Column("oracle_receipt_id", sa.String(120), nullable=False, unique=True),
            sa.Column("current_content_token_encrypted", sa.Text(), nullable=False),
            sa.Column("verified_identity_digest", sa.String(64), nullable=False),
            sa.Column("raw_content_verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("downstream_status", sa.String(40), nullable=False, server_default="PENDING"),
            sa.Column("downstream_verified_at", sa.DateTime(timezone=True)),
            sa.Column("observation_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        _indexes(
            "add_oracle_identity_repair_receipts",
            [
                "repair_item_id",
                "operation_id",
                "payload_digest",
                "action",
                "oracle_receipt_id",
                "verified_identity_digest",
                "raw_content_verified_at",
                "downstream_status",
                "downstream_verified_at",
            ],
        )

    if "add_attendance_repair_events" not in tables:
        op.create_table(
            "add_attendance_repair_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "job_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_repair_jobs.id"),
                nullable=False,
            ),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(50), nullable=False),
            sa.Column("item_id", sa.Integer(), sa.ForeignKey("add_attendance_repair_items.id")),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("previous_hash", sa.String(64)),
            sa.Column("row_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "job_id", "sequence", name="uq_add_attendance_repair_event_sequence"
            ),
            sa.UniqueConstraint(
                "job_id", "idempotency_key", name="uq_add_attendance_repair_event_idempotency"
            ),
        )
        _indexes("add_attendance_repair_events", ["job_id", "state", "item_id", "row_hash"])

    if "add_attendance_repair_oracle_slots" not in tables:
        op.create_table(
            "add_attendance_repair_oracle_slots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("lease_owner", sa.String(120)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "id in (1, 2)",
                name="ck_add_attendance_repair_oracle_slot_id",
            ),
        )
        _indexes(
            "add_attendance_repair_oracle_slots",
            ["lease_owner", "lease_expires_at"],
        )
        now = datetime.now(timezone.utc)
        bind.execute(
            sa.text(
                "INSERT INTO add_attendance_repair_oracle_slots "
                "(id, lease_owner, lease_expires_at, updated_at) "
                "VALUES (:id, NULL, NULL, :updated_at)"
            ),
            [{"id": 1, "updated_at": now}, {"id": 2, "updated_at": now}],
        )

    if "add_attendance_repair_worker_heartbeat" not in tables:
        op.create_table(
            "add_attendance_repair_worker_heartbeat",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("worker_id", sa.String(120), nullable=False, unique=True),
            sa.Column("state", sa.String(30), nullable=False),
            sa.Column("last_started_at", sa.DateTime(timezone=True)),
            sa.Column("last_completed_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(120)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        _indexes(
            "add_attendance_repair_worker_heartbeat",
            ["worker_id", "state", "last_error_code"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    attendance_columns = {
        row["name"] for row in inspector.get_columns("add_attendance_events")
    }
    attendance_indexes = {
        row["name"] for row in inspector.get_indexes("add_attendance_events")
    }
    for name in (
        "ix_add_attendance_events_identity_downstream_confirmed_at",
        "ix_add_attendance_events_identity_content_confirmed_at",
        "ix_add_attendance_events_identity_content_status",
        "ix_add_attendance_events_effective_identity_revision_id",
    ):
        if name in attendance_indexes:
            op.drop_index(name, table_name="add_attendance_events")
    attendance_foreign_keys = {
        row["name"] for row in inspector.get_foreign_keys("add_attendance_events")
    }
    with op.batch_alter_table("add_attendance_events") as batch:
        if (
            "effective_identity_revision_id" in attendance_columns
            and "fk_add_attendance_effective_identity_revision"
            in attendance_foreign_keys
        ):
            batch.drop_constraint(
                "fk_add_attendance_effective_identity_revision",
                type_="foreignkey",
            )
        for name in (
            "identity_downstream_confirmed_at",
            "identity_content_confirmed_at",
            "identity_content_status",
            "effective_identity_revision_id",
        ):
            if name in attendance_columns:
                batch.drop_column(name)
    for table in (
        "add_attendance_repair_worker_heartbeat",
        "add_attendance_repair_oracle_slots",
        "add_attendance_repair_events",
        "add_oracle_identity_repair_receipts",
        "add_attendance_identity_revisions",
        "add_attendance_repair_items",
        "add_attendance_repair_cohorts",
        "add_attendance_repair_targets",
        "add_attendance_repair_jobs",
        "add_audit_chain_head",
    ):
        if table in tables:
            op.drop_table(table)

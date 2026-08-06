"""Add ADD-owned resumable attendance reconciliation.

Revision ID: 20260806_0012
Revises: 20260729_0011
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260806_0012"
down_revision = "20260729_0011"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _index(
    name: str,
    table: str,
    columns: list[str],
    *,
    unique: bool = False,
    where: str | None = None,
) -> None:
    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(table)}
    if name in indexes:
        return
    kwargs: dict[str, object] = {}
    if where:
        kwargs["postgresql_where"] = sa.text(where)
        kwargs["sqlite_where"] = sa.text(where)
    op.create_index(name, table, columns, unique=unique, **kwargs)


def upgrade() -> None:
    tables = _tables()
    if "add_reconciliation_jobs" not in tables:
        op.create_table(
            "add_reconciliation_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(36), nullable=False),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
            sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
            sa.Column("mode", sa.String(40), nullable=False, server_default="FULL_HISTORY_BASELINE"),
            sa.Column("status", sa.String(40), nullable=False, server_default="QUEUED"),
            sa.Column("phase", sa.String(50), nullable=False, server_default="PREFLIGHT"),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("terminal_serial", sa.String(120)),
            sa.Column("terminal_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("firmware_version", sa.String(80)),
            sa.Column("identity_snapshot_id", sa.Integer(), sa.ForeignKey("add_device_user_snapshots.id")),
            sa.Column("cutoff_count", sa.Integer()),
            sa.Column("latest_terminal_count", sa.Integer()),
            sa.Column("record_size", sa.Integer()),
            sa.Column("source_total_bytes", sa.BigInteger()),
            sa.Column("committed_next_ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("scanned_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("add_durable_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("already_present_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("terminal_duplicate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blocked_identity_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ords_target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ords_confirmed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ords_pending_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("first_anchor_digest", sa.String(64)),
            sa.Column("last_chain_digest", sa.String(64)),
            sa.Column("wait_reason", sa.String(160)),
            sa.Column("error_code", sa.String(120)),
            sa.Column("error_message", sa.Text()),
            sa.Column("capture_certificate", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("oracle_certificate", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("capture_certified_at", sa.DateTime(timezone=True)),
            sa.Column("oracle_certified_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("last_progress_at", sa.DateTime(timezone=True)),
            sa.Column("next_retry_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", name="uq_add_reconciliation_jobs_job_id"),
            sa.UniqueConstraint("connector_id", "idempotency_key", name="uq_add_reconciliation_job_idempotency"),
        )
    for column in (
        "job_id", "connector_id", "zkt_device_id", "mode", "status", "phase",
        "actor", "terminal_serial", "identity_snapshot_id", "wait_reason", "error_code",
        "capture_certified_at", "oracle_certified_at", "last_progress_at", "next_retry_at",
    ):
        _index(f"ix_add_reconciliation_jobs_{column}", "add_reconciliation_jobs", [column], unique=column == "job_id")
    _index(
        "uq_add_reconciliation_active_connector",
        "add_reconciliation_jobs",
        ["connector_id"],
        unique=True,
        where="status not in ('COMPLETED','CANCELLED','FAILED','INVALIDATED')",
    )

    if "add_reconciliation_chunks" not in tables:
        op.create_table(
            "add_reconciliation_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("start_ordinal", sa.Integer(), nullable=False),
            sa.Column("end_ordinal", sa.Integer(), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("chunk_digest", sa.String(64), nullable=False),
            sa.Column("previous_chain_digest", sa.String(64)),
            sa.Column("resulting_chain_digest", sa.String(64), nullable=False),
            sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("already_present_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blocked_identity_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "generation", "sequence", name="uq_add_reconcile_chunk_sequence"),
            sa.UniqueConstraint("job_id", "generation", "start_ordinal", name="uq_add_reconcile_chunk_start"),
        )
    for column in ("job_id", "chunk_digest", "resulting_chain_digest"):
        _index(f"ix_add_reconciliation_chunks_{column}", "add_reconciliation_chunks", [column])

    if "add_terminal_record_manifest" not in tables:
        op.create_table(
            "add_terminal_record_manifest",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
            sa.Column("chunk_id", sa.Integer(), sa.ForeignKey("add_reconciliation_chunks.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("raw_record_digest", sa.String(64), nullable=False),
            sa.Column("terminal_record_key", sa.String(64), nullable=False),
            sa.Column("occurrence_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("attendance_event_id", sa.Integer(), sa.ForeignKey("add_attendance_events.id")),
            sa.Column("disposition", sa.String(50), nullable=False),
            sa.Column("protected_raw_record", sa.Text()),
            sa.Column("error_code", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "generation", "ordinal", name="uq_add_terminal_record_ordinal"),
        )
    for column in (
        "job_id", "chunk_id", "raw_record_digest", "terminal_record_key",
        "attendance_event_id", "disposition", "error_code",
    ):
        _index(f"ix_add_terminal_record_manifest_{column}", "add_terminal_record_manifest", [column])

    if "add_reconciliation_coverage" not in tables:
        op.create_table(
            "add_reconciliation_coverage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("coverage_id", sa.String(36), nullable=False),
            sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
            sa.Column("terminal_serial", sa.String(120), nullable=False),
            sa.Column("terminal_generation", sa.Integer(), nullable=False),
            sa.Column("certified_source_cursor", sa.Integer(), nullable=False),
            sa.Column("source_chain_digest", sa.String(64), nullable=False),
            sa.Column("capture_state", sa.String(50), nullable=False),
            sa.Column("oracle_state", sa.String(50), nullable=False),
            sa.Column("capture_evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("oracle_evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("invalidated_reason", sa.String(160)),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("oracle_certified_at", sa.DateTime(timezone=True)),
            sa.Column("invalidated_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("coverage_id", name="uq_add_reconciliation_coverage_id"),
        )
    for column in ("coverage_id", "zkt_device_id", "job_id", "terminal_serial", "capture_state", "oracle_state", "active"):
        _index(f"ix_add_reconciliation_coverage_{column}", "add_reconciliation_coverage", [column], unique=column == "coverage_id")
    _index(
        "uq_add_reconciliation_active_coverage",
        "add_reconciliation_coverage",
        ["zkt_device_id"],
        unique=True,
        where="active = true",
    )

    if "add_reconciliation_events" not in tables:
        op.create_table(
            "add_reconciliation_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
            sa.Column("state", sa.String(50), nullable=False),
            sa.Column("idempotency_key", sa.String(120)),
            sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "idempotency_key", name="uq_add_reconciliation_event_idempotency"),
        )
    _index("ix_add_reconciliation_events_job_id", "add_reconciliation_events", ["job_id"])
    _index("ix_add_reconciliation_events_state", "add_reconciliation_events", ["state"])
    _index("ix_add_reconciliation_events_idempotency_key", "add_reconciliation_events", ["idempotency_key"])


def downgrade() -> None:
    raise RuntimeError(
        "Reconciliation manifests and assurance evidence are immutable production records; restore a pre-deploy backup."
    )

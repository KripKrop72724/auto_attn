"""Add poison-record-safe source tail evidence and exception review.

Revision ID: 20260806_0014
Revises: 20260806_0013
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260806_0014"
down_revision = "20260806_0013"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    return {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    manifest = "add_terminal_record_manifest"
    existing = _column_names(manifest)
    additions = {
        "connector_id": sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id")),
        "zkt_device_id": sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id")),
        "terminal_serial": sa.Column("terminal_serial", sa.String(120)),
        "source_kind": sa.Column("source_kind", sa.String(30), server_default="BASELINE"),
        "canonical_source": sa.Column(
            "canonical_source", sa.Boolean(), server_default=sa.false()
        ),
        "record_size": sa.Column("record_size", sa.Integer()),
        "raw_timestamp": sa.Column("raw_timestamp", sa.BigInteger()),
        "observed_uid": sa.Column("observed_uid", sa.String(40)),
        "observed_user_id": sa.Column("observed_user_id", sa.String(100)),
    }
    for name, column in additions.items():
        if name not in existing:
            op.add_column(manifest, column)

    op.execute(
        sa.text(
            """
            UPDATE add_terminal_record_manifest
            SET connector_id = (
                    SELECT connector_id FROM add_reconciliation_jobs
                    WHERE add_reconciliation_jobs.id = add_terminal_record_manifest.job_id
                ),
                zkt_device_id = (
                    SELECT zkt_device_id FROM add_reconciliation_jobs
                    WHERE add_reconciliation_jobs.id = add_terminal_record_manifest.job_id
                ),
                terminal_serial = COALESCE((
                    SELECT terminal_serial FROM add_reconciliation_jobs
                    WHERE add_reconciliation_jobs.id = add_terminal_record_manifest.job_id
                ), 'unknown'),
                source_kind = COALESCE(source_kind, 'BASELINE'),
                record_size = (
                    SELECT record_size FROM add_reconciliation_jobs
                    WHERE add_reconciliation_jobs.id = add_terminal_record_manifest.job_id
                )
            WHERE connector_id IS NULL OR zkt_device_id IS NULL OR terminal_serial IS NULL
            """
        )
    )
    conflicting_source = op.get_bind().execute(
        sa.text(
            """
            SELECT zkt_device_id, generation, ordinal
            FROM add_terminal_record_manifest
            GROUP BY zkt_device_id, generation, ordinal
            HAVING COUNT(DISTINCT raw_record_digest) > 1
            LIMIT 1
            """
        )
    ).first()
    if conflicting_source is not None:
        raise RuntimeError(
            "Existing terminal source evidence diverges at one immutable ordinal; "
            "migration stopped before choosing a canonical ledger row."
        )
    op.execute(
        sa.text(
            """
            UPDATE add_terminal_record_manifest
            SET canonical_source = CASE WHEN id IN (
                SELECT MIN(id)
                FROM add_terminal_record_manifest
                GROUP BY zkt_device_id, generation, ordinal
            ) THEN TRUE ELSE FALSE END
            WHERE canonical_source IS NULL OR canonical_source = FALSE
            """
        )
    )
    with op.batch_alter_table(manifest) as batch:
        batch.alter_column("job_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("chunk_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("connector_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("zkt_device_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("terminal_serial", existing_type=sa.String(120), nullable=False)
        batch.alter_column(
            "canonical_source",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=None,
        )

    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(manifest)}
    for column in ("connector_id", "zkt_device_id", "terminal_serial", "source_kind", "observed_uid", "observed_user_id"):
        name = f"ix_{manifest}_{column}"
        if name not in indexes:
            op.create_index(name, manifest, [column])
    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(manifest)}
    if "uq_add_terminal_source_ordinal" not in indexes:
        op.create_index(
            "uq_add_terminal_source_ordinal",
            manifest,
            ["zkt_device_id", "generation", "ordinal"],
            unique=True,
            postgresql_where=sa.text("canonical_source = true"),
            sqlite_where=sa.text("canonical_source = 1"),
        )

    coverage = "add_reconciliation_coverage"
    existing = _column_names(coverage)
    coverage_additions = {
        "source_committed_cursor": sa.Column("source_committed_cursor", sa.Integer()),
        "source_committed_chain_digest": sa.Column("source_committed_chain_digest", sa.String(64)),
        "tail_exception_count": sa.Column("tail_exception_count", sa.Integer(), server_default="0"),
        "tail_last_committed_at": sa.Column("tail_last_committed_at", sa.DateTime(timezone=True)),
    }
    for name, column in coverage_additions.items():
        if name not in existing:
            op.add_column(coverage, column)
    op.execute(
        sa.text(
            """
            UPDATE add_reconciliation_coverage
            SET source_committed_cursor = certified_source_cursor,
                source_committed_chain_digest = source_chain_digest,
                tail_exception_count = COALESCE(tail_exception_count, 0)
            WHERE source_committed_cursor IS NULL OR source_committed_chain_digest IS NULL
            """
        )
    )
    with op.batch_alter_table(coverage) as batch:
        batch.alter_column("source_committed_cursor", existing_type=sa.Integer(), nullable=False)
        batch.alter_column(
            "source_committed_chain_digest", existing_type=sa.String(64), nullable=False
        )
    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(coverage)}
    if "ix_add_reconciliation_coverage_tail_last_committed_at" not in indexes:
        op.create_index(
            "ix_add_reconciliation_coverage_tail_last_committed_at",
            coverage,
            ["tail_last_committed_at"],
        )

    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_source_tail_chunks" not in tables:
        op.create_table(
            "add_source_tail_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("coverage_id", sa.Integer(), sa.ForeignKey("add_reconciliation_coverage.id"), nullable=False),
            sa.Column("connector_id", sa.Integer(), sa.ForeignKey("add_connectors.id"), nullable=False),
            sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("start_ordinal", sa.Integer(), nullable=False),
            sa.Column("end_ordinal", sa.Integer(), nullable=False),
            sa.Column("latest_terminal_count", sa.Integer(), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("chunk_digest", sa.String(64), nullable=False),
            sa.Column("previous_chain_digest", sa.String(64), nullable=False),
            sa.Column("resulting_chain_digest", sa.String(64), nullable=False),
            sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blocked_identity_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("exception_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("coverage_id", "generation", "start_ordinal", name="uq_add_source_tail_start"),
        )
        for column in ("coverage_id", "connector_id", "zkt_device_id", "chunk_digest", "resulting_chain_digest"):
            op.create_index(f"ix_add_source_tail_chunks_{column}", "add_source_tail_chunks", [column])

    if "add_terminal_record_reviews" not in tables:
        op.create_table(
            "add_terminal_record_reviews",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("review_id", sa.String(36), nullable=False, unique=True),
            sa.Column("manifest_id", sa.Integer(), sa.ForeignKey("add_terminal_record_manifest.id"), nullable=False),
            sa.Column("state", sa.String(30), nullable=False, server_default="REVIEWED"),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("idempotency_key", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("manifest_id", "idempotency_key", name="uq_add_terminal_record_review_request"),
        )
        for column in ("review_id", "manifest_id", "state", "actor"):
            op.create_index(f"ix_add_terminal_record_reviews_{column}", "add_terminal_record_reviews", [column])


def downgrade() -> None:
    raise RuntimeError(
        "Poison-record-safe tail rows are immutable production evidence; restore a pre-deploy backup."
    )

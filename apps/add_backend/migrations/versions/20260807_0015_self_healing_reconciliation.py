"""Add self-healing reconciliation epochs and divergence evidence.

Revision ID: 20260807_0015
Revises: 20260806_0014
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5


revision = "20260807_0015"
down_revision = "20260806_0014"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "add_terminal_source_epochs" not in tables:
        op.create_table(
            "add_terminal_source_epochs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("epoch_id", sa.String(36), nullable=False, unique=True),
            sa.Column("zkt_device_id", sa.Integer(), sa.ForeignKey("add_zkt_devices.id"), nullable=False),
            sa.Column("terminal_generation", sa.Integer(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(40), nullable=False, server_default="ACTIVE"),
            sa.Column("parent_epoch_id", sa.Integer(), sa.ForeignKey("add_terminal_source_epochs.id")),
            sa.Column("activated_at", sa.DateTime(timezone=True)),
            sa.Column("superseded_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "zkt_device_id", "terminal_generation", "sequence",
                name="uq_add_terminal_source_epoch_sequence",
            ),
        )
        for column in ("epoch_id", "zkt_device_id", "terminal_generation", "state", "parent_epoch_id"):
            op.create_index(f"ix_add_terminal_source_epochs_{column}", "add_terminal_source_epochs", [column])

    jobs = "add_reconciliation_jobs"
    additions = {
        "operation_id": sa.Column("operation_id", sa.String(36)),
        "recovery_parent_job_id": sa.Column("recovery_parent_job_id", sa.Integer(), sa.ForeignKey(f"{jobs}.id")),
        "source_epoch_id": sa.Column("source_epoch_id", sa.Integer(), sa.ForeignKey("add_terminal_source_epochs.id")),
        "auto_retry_count": sa.Column("auto_retry_count", sa.Integer(), server_default="0"),
        "completion_outcome": sa.Column("completion_outcome", sa.String(80)),
        "review_required": sa.Column("review_required", sa.Boolean(), server_default=sa.false()),
    }
    existing = _columns(jobs)
    for name, column in additions.items():
        if name not in existing:
            op.add_column(jobs, column)
    op.execute(sa.text("UPDATE add_reconciliation_jobs SET operation_id = job_id WHERE operation_id IS NULL"))
    op.execute(sa.text("UPDATE add_reconciliation_jobs SET auto_retry_count = 0 WHERE auto_retry_count IS NULL"))
    op.execute(sa.text("UPDATE add_reconciliation_jobs SET review_required = FALSE WHERE review_required IS NULL"))

    # One legacy epoch per physical terminal generation. Evidence bytes and
    # existing job checkpoints are left untouched.
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    legacy_generations = bind.execute(sa.text(
        "SELECT zkt_device_id, generation FROM add_terminal_record_manifest "
        "UNION SELECT zkt_device_id, terminal_generation AS generation FROM add_reconciliation_jobs "
        "UNION SELECT zkt_device_id, terminal_generation AS generation FROM add_reconciliation_coverage"
    )).all()
    for zkt_device_id, generation in legacy_generations:
        exists = bind.execute(sa.text(
            "SELECT id FROM add_terminal_source_epochs "
            "WHERE zkt_device_id = :zkt AND terminal_generation = :generation AND sequence = 1"
        ), {"zkt": zkt_device_id, "generation": generation}).first()
        if exists is None:
            bind.execute(sa.text(
                """
                INSERT INTO add_terminal_source_epochs
                    (epoch_id, zkt_device_id, terminal_generation, sequence, state,
                     activated_at, created_at, updated_at)
                VALUES (:epoch_id, :zkt, :generation, 1, 'ACTIVE', :now, :now, :now)
                """
            ), {
                "epoch_id": str(uuid5(NAMESPACE_URL, f"add-source:{zkt_device_id}:{generation}:1")),
                "zkt": zkt_device_id,
                "generation": generation,
                "now": now,
            })

    manifest = "add_terminal_record_manifest"
    if "source_epoch_id" not in _columns(manifest):
        op.add_column(manifest, sa.Column("source_epoch_id", sa.Integer(), sa.ForeignKey("add_terminal_source_epochs.id")))
    op.execute(sa.text(
        """
        UPDATE add_terminal_record_manifest m
        SET source_epoch_id = e.id
        FROM add_terminal_source_epochs e
        WHERE m.source_epoch_id IS NULL
          AND e.zkt_device_id = m.zkt_device_id
          AND e.terminal_generation = m.generation
          AND e.sequence = 1
        """
    ))

    coverage = "add_reconciliation_coverage"
    if "source_epoch_id" not in _columns(coverage):
        op.add_column(coverage, sa.Column("source_epoch_id", sa.Integer(), sa.ForeignKey("add_terminal_source_epochs.id")))
    op.execute(sa.text(
        """
        UPDATE add_reconciliation_coverage c
        SET source_epoch_id = e.id
        FROM add_terminal_source_epochs e
        WHERE c.source_epoch_id IS NULL
          AND e.zkt_device_id = c.zkt_device_id
          AND e.terminal_generation = c.terminal_generation
          AND e.sequence = 1
        """
    ))
    op.execute(sa.text(
        """
        UPDATE add_reconciliation_jobs j
        SET source_epoch_id = e.id
        FROM add_terminal_source_epochs e
        WHERE j.source_epoch_id IS NULL
          AND e.zkt_device_id = j.zkt_device_id
          AND e.terminal_generation = j.terminal_generation
          AND e.sequence = 1
        """
    ))

    indexes = {row["name"] for row in inspect(op.get_bind()).get_indexes(manifest)}
    if "uq_add_terminal_source_ordinal" in indexes:
        op.drop_index("uq_add_terminal_source_ordinal", table_name=manifest)
    op.create_index(
        "uq_add_terminal_source_ordinal",
        manifest,
        ["zkt_device_id", "generation", "source_epoch_id", "ordinal"],
        unique=True,
        postgresql_where=sa.text("canonical_source = true"),
        sqlite_where=sa.text("canonical_source = 1"),
    )
    for table, columns in (
        (jobs, ("operation_id", "recovery_parent_job_id", "source_epoch_id", "completion_outcome", "review_required")),
        (manifest, ("source_epoch_id",)),
        (coverage, ("source_epoch_id",)),
    ):
        current = {row["name"] for row in inspect(op.get_bind()).get_indexes(table)}
        for column in columns:
            name = f"ix_{table}_{column}"
            if name not in current:
                op.create_index(name, table, [column])

    if "add_reconciliation_divergences" not in tables:
        op.create_table(
            "add_reconciliation_divergences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("divergence_id", sa.String(36), nullable=False, unique=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("add_reconciliation_jobs.id"), nullable=False),
            sa.Column("source_epoch_id", sa.Integer(), sa.ForeignKey("add_terminal_source_epochs.id")),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(40), nullable=False, server_default="OBSERVED"),
            sa.Column("old_raw_digest", sa.String(64), nullable=False),
            sa.Column("new_raw_digest", sa.String(64), nullable=False),
            sa.Column("old_disposition", sa.String(50)),
            sa.Column("new_disposition", sa.String(50)),
            sa.Column("protected_new_raw_record", sa.Text()),
            sa.Column("observations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("next_probe_at", sa.DateTime(timezone=True)),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("divergence_id", "job_id", "source_epoch_id", "ordinal", "state", "old_raw_digest", "new_raw_digest", "next_probe_at"):
            op.create_index(f"ix_add_reconciliation_divergences_{column}", "add_reconciliation_divergences", [column])


def downgrade() -> None:
    raise RuntimeError(
        "Self-healing source epochs are immutable production evidence; restore a pre-deploy backup."
    )

"""Add exact-event attendance release review.

Revision ID: 20260904_0023
Revises: 20260902_0022
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260904_0023"
down_revision = "20260902_0022"
branch_labels = None
depends_on = None


REUSE_ATTESTATION_INDEXES = {
    "attestation_id": "ix_add_attendance_repair_reuse_attestations_attestation_id",
    "job_id": "ix_add_attendance_repair_reuse_attestations_job_id",
    "target_id": "ix_add_attendance_repair_reuse_attestations_target_id",
    "target_identity_digest": "ix_add_repair_reuse_target_digest",
    "event_membership_digest": "ix_add_repair_reuse_event_digest",
    "evidence_type": "ix_add_attendance_repair_reuse_attestations_evidence_type",
    "actor": "ix_add_attendance_repair_reuse_attestations_actor",
}


def _columns(table: str) -> set[str]:
    return {row["name"] for row in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {row["name"] for row in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())

    job_columns = _columns("add_attendance_repair_jobs")
    job_additions = (
        sa.Column(
            "workflow_version",
            sa.String(30),
            nullable=False,
            server_default="LEGACY_COHORT_V1",
        ),
        sa.Column("selection_mode", sa.String(30), nullable=False, server_default="COHORT"),
        sa.Column("selection_manifest_digest", sa.String(64)),
        sa.Column("selection_filters", sa.JSON()),
        sa.Column("selection_exclusion_manifest_digest", sa.String(64)),
        sa.Column("candidate_membership_digest", sa.String(64)),
        sa.Column("candidate_source_certificate_digest", sa.String(64)),
        sa.Column("release_target_user_id", sa.String(100)),
        sa.Column("selected_blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_reuse_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operator_excluded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_reuse_count", sa.Integer(), nullable=False, server_default="0"),
    )
    for column in job_additions:
        if column.name not in job_columns:
            op.add_column("add_attendance_repair_jobs", column)
    for name in (
        "workflow_version",
        "selection_mode",
        "selection_manifest_digest",
        "release_target_user_id",
    ):
        index_name = f"ix_add_attendance_repair_jobs_{name}"
        if index_name not in _indexes("add_attendance_repair_jobs"):
            op.create_index(index_name, "add_attendance_repair_jobs", [name])

    cohort_columns = _columns("add_attendance_repair_cohorts")
    if "selected_event_count" not in cohort_columns:
        op.add_column(
            "add_attendance_repair_cohorts",
            sa.Column("selected_event_count", sa.Integer(), nullable=False, server_default="0"),
        )
        op.execute(
            "UPDATE add_attendance_repair_cohorts "
            "SET selected_event_count = event_count WHERE selected_event_count = 0"
        )

    if "add_attendance_repair_selections" not in tables:
        op.create_table(
            "add_attendance_repair_selections",
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
                "attendance_event_id",
                sa.Integer(),
                sa.ForeignKey("add_attendance_events.id"),
                nullable=False,
            ),
            sa.Column("event_uid", sa.String(128), nullable=False),
            sa.Column("immutable_facts_digest", sa.String(64), nullable=False),
            sa.Column("source_ownership_digest", sa.String(64), nullable=False),
            sa.Column("before_identity_digest", sa.String(64), nullable=False),
            sa.Column("source_ords_status", sa.String(40), nullable=False),
            sa.Column("risk_class", sa.String(40), nullable=False),
            sa.Column("selection_origin", sa.String(30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "job_id",
                "attendance_event_id",
                name="uq_add_attendance_repair_selection_event",
            ),
        )
        for name in (
            "job_id",
            "target_id",
            "attendance_event_id",
            "event_uid",
            "source_ords_status",
            "risk_class",
            "selection_origin",
        ):
            op.create_index(
                f"ix_add_attendance_repair_selections_{name}",
                "add_attendance_repair_selections",
                [name],
            )

    if "add_attendance_repair_reuse_attestations" not in tables:
        op.create_table(
            "add_attendance_repair_reuse_attestations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("attestation_id", sa.String(36), nullable=False),
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
            sa.Column("target_identity_digest", sa.String(64), nullable=False),
            sa.Column("target_row_version", sa.Integer(), nullable=False),
            sa.Column("event_membership_digest", sa.String(64), nullable=False),
            sa.Column("event_count", sa.Integer(), nullable=False),
            sa.Column("evidence_type", sa.String(60), nullable=False),
            sa.Column("verified_name_digest", sa.String(64), nullable=False),
            sa.Column("reason_digest", sa.String(64), nullable=False),
            sa.Column("confirmation_digest", sa.String(64), nullable=False),
            sa.Column("actor", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", name="uq_add_attendance_repair_reuse_job"),
        )
        for column_name, index_name in REUSE_ATTESTATION_INDEXES.items():
            op.create_index(
                index_name,
                "add_attendance_repair_reuse_attestations",
                [column_name],
                unique=column_name == "attestation_id",
            )

    item_columns = _columns("add_attendance_repair_items")
    with op.batch_alter_table("add_attendance_repair_items") as batch:
        if "reuse_attestation_id" not in item_columns:
            batch.add_column(sa.Column("reuse_attestation_id", sa.Integer()))
            batch.create_foreign_key(
                "fk_add_attendance_repair_item_reuse_attestation",
                "add_attendance_repair_reuse_attestations",
                ["reuse_attestation_id"],
                ["id"],
            )
        if "source_ords_status" not in item_columns:
            batch.add_column(sa.Column("source_ords_status", sa.String(40)))
        if "risk_class" not in item_columns:
            batch.add_column(
                sa.Column("risk_class", sa.String(40), nullable=False, server_default="LEGACY")
            )
        if "selection_origin" not in item_columns:
            batch.add_column(
                sa.Column(
                    "selection_origin", sa.String(30), nullable=False, server_default="COHORT"
                )
            )
    for name in (
        "reuse_attestation_id",
        "source_ords_status",
        "risk_class",
        "selection_origin",
    ):
        index_name = f"ix_add_attendance_repair_items_{name}"
        if index_name not in _indexes("add_attendance_repair_items"):
            op.create_index(index_name, "add_attendance_repair_items", [name])

    queue_index = "ix_add_attendance_release_queue"
    if queue_index not in _indexes("add_attendance_events"):
        held = sa.text(
            "ords_status in ('BLOCKED_IDENTITY','QUARANTINED_IDENTITY_REUSE')"
        )
        op.create_index(
            queue_index,
            "add_attendance_events",
            ["zkt_device_id", "ords_status", "user_id", "device_event_time", "id"],
            postgresql_where=held,
            sqlite_where=held,
        )
    global_queue_index = "ix_add_attendance_release_queue_status_date"
    if global_queue_index not in _indexes("add_attendance_events"):
        held = sa.text(
            "ords_status in ('BLOCKED_IDENTITY','QUARANTINED_IDENTITY_REUSE')"
        )
        op.create_index(
            global_queue_index,
            "add_attendance_events",
            ["ords_status", "device_event_time", "id"],
            postgresql_where=held,
            sqlite_where=held,
        )


def downgrade() -> None:
    # Production repair evidence must be restored from a paired backup rather
    # than downgraded once release jobs exist. This reverse path is for empty
    # development databases only.
    if "ix_add_attendance_release_queue_status_date" in _indexes(
        "add_attendance_events"
    ):
        op.drop_index(
            "ix_add_attendance_release_queue_status_date",
            table_name="add_attendance_events",
        )
    if "ix_add_attendance_release_queue" in _indexes("add_attendance_events"):
        op.drop_index("ix_add_attendance_release_queue", table_name="add_attendance_events")

    item_columns = _columns("add_attendance_repair_items")
    item_indexes = _indexes("add_attendance_repair_items")
    for name in (
        "reuse_attestation_id",
        "source_ords_status",
        "risk_class",
        "selection_origin",
    ):
        index_name = f"ix_add_attendance_repair_items_{name}"
        if index_name in item_indexes:
            op.drop_index(index_name, table_name="add_attendance_repair_items")
    with op.batch_alter_table("add_attendance_repair_items") as batch:
        if "reuse_attestation_id" in item_columns:
            batch.drop_constraint(
                "fk_add_attendance_repair_item_reuse_attestation", type_="foreignkey"
            )
        for name in (
            "reuse_attestation_id",
            "source_ords_status",
            "risk_class",
            "selection_origin",
        ):
            if name in item_columns:
                batch.drop_column(name)

    tables = set(inspect(op.get_bind()).get_table_names())
    for table in (
        "add_attendance_repair_reuse_attestations",
        "add_attendance_repair_selections",
    ):
        if table in tables:
            op.drop_table(table)

    if "selected_event_count" in _columns("add_attendance_repair_cohorts"):
        op.drop_column("add_attendance_repair_cohorts", "selected_event_count")

    job_columns = _columns("add_attendance_repair_jobs")
    job_indexes = _indexes("add_attendance_repair_jobs")
    for name in (
        "workflow_version",
        "selection_mode",
        "selection_manifest_digest",
        "release_target_user_id",
    ):
        index_name = f"ix_add_attendance_repair_jobs_{name}"
        if index_name in job_indexes:
            op.drop_index(index_name, table_name="add_attendance_repair_jobs")
    for name in (
        "workflow_version",
        "selection_mode",
        "selection_manifest_digest",
        "selection_filters",
        "selection_exclusion_manifest_digest",
        "candidate_membership_digest",
        "candidate_source_certificate_digest",
        "release_target_user_id",
        "selected_blocked_count",
        "selected_reuse_count",
        "operator_excluded_count",
        "safe_reuse_count",
    ):
        if name in job_columns:
            op.drop_column("add_attendance_repair_jobs", name)

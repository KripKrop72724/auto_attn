"""Restore missing serials only where retained source evidence agrees.

Revision ID: 20260921_0031
Revises: 20260917_0030
"""
from alembic import op
import sqlalchemy as sa

revision = "20260921_0031"
down_revision = "20260917_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Never infer an old punch's terminal from today's connector assignment.
    # Conflicting evidence, placeholder serials, and foreign manifests cannot
    # repair provenance. Identity and Oracle delivery remain separately gated.
    op.execute(sa.text("""
        UPDATE add_attendance_events AS event
        SET device_serial = (
            SELECT MIN(manifest.terminal_serial)
            FROM add_terminal_record_manifest AS manifest
            WHERE manifest.attendance_event_id = event.id
              AND manifest.connector_id = event.connector_id
              AND manifest.zkt_device_id = event.zkt_device_id
            HAVING COUNT(DISTINCT manifest.terminal_serial) = 1
               AND MIN(manifest.terminal_serial) NOT IN ('', 'unknown')
        )
        WHERE event.device_serial IS NULL
          AND EXISTS (
            SELECT 1 FROM add_terminal_record_manifest AS manifest
            WHERE manifest.attendance_event_id = event.id
              AND manifest.connector_id = event.connector_id
              AND manifest.zkt_device_id = event.zkt_device_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM add_attendance_repair_items AS item
            WHERE item.attendance_event_id = event.id
          )
    """))


def downgrade() -> None:
    # Do not destroy restored provenance on rollback.
    pass

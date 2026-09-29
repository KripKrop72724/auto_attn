"""Identify the attendance row that prevents a snapshot transaction."""

from alembic import op

revision = "20260929_0040"
down_revision = "20260929_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION add_check_manual_attendance_delivery()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE event_id integer; attendance add_attendance_events%ROWTYPE;
          decision add_attendance_force_release_decisions%ROWTYPE; delivery_status text;
        BEGIN
          IF TG_TABLE_NAME = 'add_ords_outbox' THEN
            event_id := NEW.attendance_event_id;
            SELECT status INTO delivery_status FROM add_ords_outbox WHERE id = NEW.id;
          ELSE
            event_id := NEW.id;
            SELECT ords_status INTO delivery_status FROM add_attendance_events WHERE id = NEW.id;
          END IF;
          SELECT * INTO attendance FROM add_attendance_events WHERE id = event_id;
          IF NOT FOUND OR NOT attendance.manual_release_required
             OR delivery_status NOT IN ('PENDING','IN_FLIGHT','FAILED_RETRYABLE','RETRYING','ACKED','ACKED_CHECK') THEN
            RETURN NULL;
          END IF;
          SELECT * INTO decision FROM add_attendance_force_release_decisions
            WHERE attendance_event_id = event_id ORDER BY id DESC LIMIT 1;
          IF FOUND THEN
            IF delivery_status IN ('ACKED','ACKED_CHECK') AND NOT EXISTS (
              SELECT 1 FROM add_attendance_recovery_items i WHERE i.id = decision.item_id
                AND i.result ->> 'oracle_verified_payload_digest' = decision.payload_digest
                AND i.result ->> 'oracle_content_token' IS NOT NULL) THEN
              RAISE EXCEPTION 'Manual attendance requires matching Oracle content verification';
            END IF;
          ELSIF NOT EXISTS (SELECT 1 FROM add_attendance_safe_repair_decisions
              WHERE attendance_event_id = event_id AND actor NOT LIKE 'system:%')
            AND NOT EXISTS (SELECT 1 FROM add_attendance_repair_items i
              JOIN add_attendance_repair_jobs j ON j.id = i.job_id
              WHERE i.attendance_event_id = event_id AND j.approved_at IS NOT NULL) THEN
            RAISE EXCEPTION 'Attendance requires explicit administrator approval (event %)', event_id;
          END IF;
          RETURN NULL;
        END $$;
    """)


def downgrade() -> None:
    # Keep the internal event identifier available during operational rollback.
    pass

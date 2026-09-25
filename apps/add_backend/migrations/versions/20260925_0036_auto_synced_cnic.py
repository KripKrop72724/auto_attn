"""Allow automatic release of attendance proven by a synced device CNIC."""

from alembic import op

revision = "20260925_0036"
down_revision = "20260923_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION add_auto_synced_cnic_verified(e add_attendance_events)
        RETURNS boolean LANGUAGE sql STABLE AS $$
          SELECT COALESCE(
            e.identity_resolution_status = 'RESOLVED_SYNCED_CNIC'
            AND e.identity_repair_reason = 'VERIFIED_SYNCED_CNIC'
            AND e.cnic_lookup_hash IS NOT NULL
            AND e.cnic_encrypted IS NOT NULL
            AND e.clock_quality = 'OK'
            AND e.device_event_time >= TIMESTAMPTZ '2010-01-01 00:00:00+00'
            AND e.device_event_time <= e.captured_at + INTERVAL '1 day'
            AND EXISTS (
              SELECT 1 FROM add_device_users u
              JOIN add_zkt_devices z ON z.id = u.zkt_device_id
              WHERE u.id = e.device_user_id
                AND u.zkt_device_id = e.zkt_device_id
                AND u.user_id = e.user_id
                AND u.lifecycle_state = 'ACTIVE' AND u.present
                AND u.identity_conflict_code IS NULL
                AND u.cnic_lookup_hash = e.cnic_lookup_hash
                AND u.cnic_encrypted IS NOT NULL
                AND u.snapshot_revision = z.identity_snapshot_revision
                AND z.snapshot_complete AND z.identity_snapshot_stable
                AND z.identity_snapshot_id = e.identity_snapshot_id
                AND z.confirmed_serial = z.serial
                AND e.device_serial = z.confirmed_serial
                AND z.identity_snapshot_observed_at IS NOT NULL
                AND (e.captured_cnic_lookup_hash = u.cnic_lookup_hash
                  OR (e.captured_cnic_lookup_hash IS NULL
                    AND e.source IN ('LIVE','LIVE_POLL')
                    AND (e.display_name IS NULL OR btrim(e.display_name) = ''
                      OR lower(btrim(e.display_name)) = lower(btrim(u.display_name)))
                    AND e.captured_at >= e.device_event_time
                    AND e.captured_at - e.device_event_time <= INTERVAL '10 minutes'
                    AND z.last_identity_change_at IS NOT NULL
                    AND e.device_event_time >= z.last_identity_change_at
                    AND z.identity_snapshot_observed_at >= e.captured_at))
            ), false)
        $$;
        CREATE OR REPLACE FUNCTION add_retain_manual_attendance_hold()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.ords_status IN ('BLOCKED_IDENTITY','WAITING_FOR_SNAPSHOT','QUARANTINED_IDENTITY_REUSE')
             OR (TG_OP = 'UPDATE' AND OLD.manual_release_required
                 AND NOT add_auto_synced_cnic_verified(NEW)) THEN
            NEW.manual_release_required := true;
          END IF;
          RETURN NEW;
        END $$;
    """)


def downgrade() -> None:
    # Retain verified releases and their audit trail on rollback. Reinstall the
    # conservative guard for subsequent writes.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION add_retain_manual_attendance_hold()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.ords_status IN ('BLOCKED_IDENTITY','WAITING_FOR_SNAPSHOT','QUARANTINED_IDENTITY_REUSE')
             OR (TG_OP = 'UPDATE' AND OLD.manual_release_required) THEN
            NEW.manual_release_required := true;
          END IF;
          RETURN NEW;
        END $$;
        DROP FUNCTION IF EXISTS add_auto_synced_cnic_verified(add_attendance_events);
    """)

"""Allow delayed 40-byte record UID with exact user and retained CNIC."""

from alembic import op

revision = "20260929_0039"
down_revision = "20260929_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION add_auto_reconciled_cnic_verified(e add_attendance_events)
        RETURNS boolean LANGUAGE sql STABLE AS $$
          SELECT COALESCE(
            e.identity_resolution_status = 'RESOLVED_SYNCED_CNIC'
            AND e.identity_repair_reason = 'VERIFIED_SYNCED_CNIC'
            AND e.source = 'CURRENT_RECONCILE'
            AND e.captured_cnic_lookup_hash IS NULL
            AND e.cnic_lookup_hash IS NOT NULL
            AND e.cnic_encrypted IS NOT NULL
            AND e.uid IS NOT NULL
            AND e.identity_terminal_fingerprint IS NOT NULL
            AND e.clock_quality = 'OK'
            AND e.device_event_time >= TIMESTAMPTZ '2010-01-01 00:00:00+00'
            AND e.device_event_time <= e.captured_at + INTERVAL '1 day'
            AND (
              e.captured_at BETWEEN e.device_event_time
                                AND e.device_event_time + INTERVAL '10 minutes'
              OR EXISTS (
                SELECT 1 FROM add_terminal_record_manifest saved
                LEFT JOIN add_terminal_source_epochs epoch
                  ON epoch.id = saved.source_epoch_id
                WHERE saved.attendance_event_id = e.id
                  AND saved.canonical_source
                  AND saved.disposition IN ('EVENT','BLOCKED_IDENTITY')
                  AND saved.connector_id = e.connector_id
                  AND saved.zkt_device_id = e.zkt_device_id
                  AND saved.terminal_serial = e.device_serial
                  AND (saved.observed_uid = e.uid
                    OR (saved.record_size = 40
                      AND saved.observed_user_id = e.user_id
                      AND saved.observed_uid =
                        (e.raw_event ->> 'attendance_record_uid')))
                  AND (saved.observed_user_id IS NULL
                    OR saved.observed_user_id = e.user_id)
                  AND (saved.source_epoch_id IS NULL
                    OR (epoch.state = 'ACTIVE'
                      AND epoch.zkt_device_id = e.zkt_device_id
                      AND epoch.terminal_generation = saved.generation))
              )
            )
            AND e.received_at BETWEEN e.captured_at - INTERVAL '30 seconds'
                                  AND e.captured_at + INTERVAL '10 minutes'
            AND EXISTS (
              SELECT 1 FROM add_device_users u
              JOIN add_zkt_devices z ON z.id = u.zkt_device_id
              WHERE u.id = e.device_user_id
                AND u.zkt_device_id = e.zkt_device_id
                AND z.connector_id = e.connector_id
                AND u.user_id = e.user_id AND u.uid = e.uid
                AND u.lifecycle_state = 'ACTIVE' AND u.present
                AND u.identity_conflict_code IS NULL
                AND u.cnic_lookup_hash = e.cnic_lookup_hash
                AND u.cnic_encrypted IS NOT NULL
                AND u.snapshot_revision = z.identity_snapshot_revision
                AND z.snapshot_complete AND z.identity_snapshot_stable
                AND z.identity_snapshot_id = e.identity_snapshot_id
                AND z.confirmed_serial = z.serial
                AND e.device_serial = z.confirmed_serial
                AND EXISTS (
                  SELECT 1 FROM add_attendance_identity_history h
                  WHERE h.zkt_device_id = z.id
                    AND h.device_user_id = u.id
                    AND h.terminal_serial = e.device_serial
                    AND h.user_id = e.user_id AND h.uid = e.uid
                    AND h.fingerprint = e.identity_terminal_fingerprint
                    AND h.cnic_lookup_hash = u.cnic_lookup_hash
                    AND h.cnic_encrypted IS NOT NULL
                    AND NOT h.revoked
                    AND h.observed_from <= e.device_event_time
                    AND h.observed_until >= e.device_event_time
                )
                AND NOT EXISTS (
                  SELECT 1 FROM add_attendance_identity_history h
                  WHERE h.zkt_device_id = z.id
                    AND h.terminal_serial = e.device_serial
                    AND h.user_id = e.user_id AND h.uid = e.uid
                    AND h.fingerprint = e.identity_terminal_fingerprint
                    AND NOT h.revoked
                    AND h.observed_from <= e.device_event_time
                    AND h.observed_until >= e.device_event_time
                    AND (h.device_user_id IS DISTINCT FROM u.id
                      OR h.cnic_lookup_hash IS DISTINCT FROM u.cnic_lookup_hash)
                )
            )
            AND NOT EXISTS (
              SELECT 1 FROM add_terminal_record_manifest m
              WHERE m.attendance_event_id = e.id
                AND (m.connector_id IS DISTINCT FROM e.connector_id
                  OR m.zkt_device_id IS DISTINCT FROM e.zkt_device_id
                  OR m.terminal_serial IS DISTINCT FROM e.device_serial
                  OR (m.observed_user_id IS NOT NULL
                    AND m.observed_user_id <> e.user_id)
                  OR (m.observed_uid IS NOT NULL AND m.observed_uid <> e.uid
                    AND NOT COALESCE(m.record_size = 40 AND
                      m.observed_uid = (e.raw_event ->> 'attendance_record_uid'), false))
                  OR m.disposition NOT IN
                    ('EVENT','BLOCKED_IDENTITY','TERMINAL_DUPLICATE'))
            ), false)
        $$;
    """)


def downgrade() -> None:
    # Keep verified releases intact if application code rolls back.
    pass

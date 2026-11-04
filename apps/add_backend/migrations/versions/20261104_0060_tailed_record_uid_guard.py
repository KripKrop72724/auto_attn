"""Treat 49-byte ZKT records like 40-byte ones in the reconciled CNIC guard.

ZKTeco MB40-VL/ID terminals on platform ZAM180_TFT (LHRCENTRAL, 4 Nov 2026)
return the 40-byte attendance record followed by a 9-byte tail (0xff or 0x00,
ASCII "255", zeros). Its first two bytes are the attendance record UID, as on
a 40-byte record, never the enrolled UID. add_auto_reconciled_cnic_verified
admitted that record UID only for record_size 40, so every reconciled punch
from these terminals stayed held after the application proved it.

The function is 0056's, with each "record_size = 40" now
"record_size IN (40, 49)". Nothing else changes, and 8- and 16-byte records
are unaffected.

Only CREATE OR REPLACE FUNCTION: no table is altered, scanned, locked or
backfilled.
"""

from alembic import op

revision = "20261104_0060_tailed_record_uid"
down_revision = "20261026_0059_ota_floor_latch"
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
            AND e.source IN ('CURRENT_RECONCILE', 'DUMP_RECONNECT', 'DUMP_STARTUP',
                             'RECONCILE_15M', 'FULL_HISTORY')
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
                  -- 0060: a 49-byte MB40-VL/ID record keeps the 40-byte
                  -- record's UID namespace; its tail is never decoded.
                  AND (saved.observed_uid = e.uid
                    OR (saved.record_size IN (40, 49)
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
              OR (
                -- 0056: a legacy reconnect/startup dump row with no saved
                -- terminal record from any connector. Its batch identifiers
                -- must match one retained, fingerprint-bound interval of the
                -- released user and CNIC at punch time. Whole-roster
                -- continuity never satisfies this case.
                e.source IN ('DUMP_RECONNECT', 'DUMP_STARTUP')
                AND e.event_uid ~ '^[0-9a-f]{64}$'
                AND e.identity_terminal_fingerprint ~ '^[0-9a-f]{64}$'
                AND NOT EXISTS (
                  SELECT 1 FROM add_terminal_record_manifest any_saved
                  WHERE any_saved.attendance_event_id = e.id
                )
                AND EXISTS (
                  SELECT 1 FROM add_attendance_identity_history h
                  WHERE h.zkt_device_id = e.zkt_device_id
                    AND h.device_user_id = e.device_user_id
                    AND h.terminal_serial = e.device_serial
                    AND h.user_id = e.user_id AND h.uid = e.uid
                    AND h.fingerprint = e.identity_terminal_fingerprint
                    AND h.cnic_lookup_hash = e.cnic_lookup_hash
                    AND h.cnic_encrypted IS NOT NULL
                    AND NOT h.revoked
                    AND h.observed_from <= e.device_event_time
                    AND h.observed_until >= e.device_event_time
                )
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
                AND (
                  EXISTS (
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
                  OR (
                    z.last_identity_change_at IS NOT NULL
                    AND z.identity_snapshot_observed_at IS NOT NULL
                    AND z.last_identity_change_at <= e.device_event_time
                    AND e.device_event_time <=
                      z.identity_snapshot_observed_at + INTERVAL '5 seconds'
                    AND u.terminal_identity_fingerprint =
                      e.identity_terminal_fingerprint
                    AND (
                      e.captured_at BETWEEN e.device_event_time
                                        AND e.device_event_time + INTERVAL '10 minutes'
                      OR NOT EXISTS (
                        SELECT 1 FROM add_terminal_record_manifest m
                        WHERE m.attendance_event_id = e.id
                          AND m.record_size IN (40, 49)
                          AND m.observed_uid IS DISTINCT FROM e.uid
                      )
                    )
                    AND NOT (
                      e.source IN ('DUMP_RECONNECT', 'DUMP_STARTUP')
                      AND NOT EXISTS (
                        SELECT 1 FROM add_terminal_record_manifest dump_saved
                        WHERE dump_saved.attendance_event_id = e.id
                      )
                    )
                    AND NOT EXISTS (
                      SELECT 1 FROM add_attendance_identity_history h
                      WHERE h.zkt_device_id = z.id
                        AND h.terminal_serial = e.device_serial
                        AND h.user_id = e.user_id AND h.uid = e.uid
                        AND h.observed_from <= e.device_event_time
                        AND h.observed_until >= e.device_event_time
                    )
                  )
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
                    AND NOT COALESCE(m.record_size IN (40, 49) AND
                      m.observed_uid = (e.raw_event ->> 'attendance_record_uid'), false))
                  OR m.disposition NOT IN
                    ('EVENT','BLOCKED_IDENTITY','TERMINAL_DUPLICATE'))
            ), false)
        $$;
    """)


def downgrade() -> None:
    # Keep verified releases intact if application code rolls back. Earlier
    # application code rejects 49-byte records outright, so it never proposes
    # a release this function admits and 0056 would refuse.
    pass

"""Check an automatic CNIC release against the installed PostgreSQL hold.

The application proof can be more permissive than an older database guard.
Evaluate the proposed identity without updating the event, so an unsupported
release stays held instead of rolling back its whole ingestion transaction.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def database_synced_cnic_release_proven(session: Session, *, row, zkt, user) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    if not row.id or not user.id or not zkt.identity_snapshot_id:
        return False

    # Migration availability is constant within the ingestion transaction.
    # Older installations may have only the live/captured-CNIC exception.
    transaction = session.get_transaction()
    cached = session.info.get("attendance_auto_cnic_guards")
    with session.no_autoflush:
        if cached is None or cached[0] is not transaction:
            available = session.execute(text("""
                SELECT
                  to_regprocedure(
                    'add_auto_synced_cnic_verified(add_attendance_events)'
                  ) IS NOT NULL AS synced,
                  to_regprocedure(
                    'add_auto_reconciled_cnic_verified(add_attendance_events)'
                  ) IS NOT NULL AS reconciled
            """)).mappings().one()
            guards = tuple(
                name for name, present in (
                    ("add_auto_synced_cnic_verified", available["synced"]),
                    ("add_auto_reconciled_cnic_verified", available["reconciled"]),
                ) if present
            )
            session.info["attendance_auto_cnic_guards"] = (transaction, guards)
        else:
            guards = cached[1]
        if not guards:
            return False

        # Function names come only from the two literals above. The proposed
        # fields mirror release_synced_cnic_attendance_event; capture evidence,
        # terminal fingerprint and event time remain the persisted originals.
        predicate = " OR ".join(f"COALESCE({name}(proposed.e), false)" for name in guards)
        accepted = session.scalar(text(f"""
            SELECT {predicate}
            FROM (
              SELECT jsonb_populate_record(
                NULL::add_attendance_events,
                to_jsonb(e) || jsonb_build_object(
                  'device_user_id', u.id,
                  'identity_snapshot_id', :snapshot_id,
                  'identity_resolution_status', 'RESOLVED_SYNCED_CNIC',
                  'identity_repair_reason', 'VERIFIED_SYNCED_CNIC',
                  'display_name', u.display_name,
                  'cnic_encrypted', u.cnic_encrypted,
                  'cnic_lookup_hash', u.cnic_lookup_hash,
                  'cnic_last4', u.cnic_last4,
                  'manual_release_required', false,
                  'ords_status', 'PENDING'
                )
              ) AS e
              FROM add_attendance_events e
              JOIN add_device_users u ON u.id = :user_id
              WHERE e.id = :event_id
            ) proposed
        """), {
            "event_id": row.id,
            "user_id": user.id,
            "snapshot_id": zkt.identity_snapshot_id,
        })
    return accepted is True

"""Server-recorded ordering proof for a terminal refresh with a skewed clock.

Only the authenticated WebSocket ingestion path supplies these observations.
Device timestamps are compared with each other, never with the server clock.
The original absolute-time check remains required when this proof is absent.
"""

from sqlalchemy import select

from zk_add.models import DeviceCommand, DeviceCommandEvent, DeviceUserSnapshot
from zk_add.time_utils import ensure_utc, parse_datetime

BEGIN = "SYNC_READ_BEGIN"
ROSTER = "SYNC_READ_ROSTER"
END = "SYNC_READ_END"


def _event(session, command_id, status):
    return session.scalar(
        select(DeviceCommandEvent)
        .where(DeviceCommandEvent.command_id == command_id, DeviceCommandEvent.status == status)
        .order_by(DeviceCommandEvent.id)
        .limit(1)
    )


def record_command(session, connector, command, envelope):
    if command.command_type != "REFRESH_USERS" or connector.firmware_family == "hikvision":
        return
    status = {"RUNNING": BEGIN, "SUCCEEDED": END}.get(envelope.payload.get("status"))
    if not status or command.status != envelope.payload.get("status"):
        return
    if _event(session, command.id, status):
        return  # A replay cannot replace the original execution boundary.
    session.add(DeviceCommandEvent(command_id=command.id, status=status, details={
        "version": 1, "boot_id": envelope.boot_id, "sequence": envelope.seq,
        "sent_at": ensure_utc(envelope.sent_at).isoformat(),
    }))


def record_roster(session, connector, envelope):
    terminal = connector.zkt_device
    snapshot = session.get(DeviceUserSnapshot, terminal.identity_snapshot_id)
    if not snapshot or not snapshot.complete or not snapshot.stable:
        return
    # A fresh transport envelope around a previously saved snapshot is a replay.
    if session.scalar(select(DeviceUserSnapshot.id).where(
        DeviceUserSnapshot.zkt_device_id == terminal.id,
        DeviceUserSnapshot.snapshot_id == snapshot.snapshot_id,
        DeviceUserSnapshot.id != snapshot.id,
    ).limit(1)):
        return
    command = session.scalar(select(DeviceCommand).where(
        DeviceCommand.connector_id == connector.id,
        DeviceCommand.command_type == "REFRESH_USERS",
        DeviceCommand.status.in_(["RUNNING", "ACKNOWLEDGED", "RETRYING"]),
    ).order_by(DeviceCommand.id.desc()).limit(1))
    if not command or _event(session, command.id, ROSTER):
        return
    begin = _event(session, command.id, BEGIN)
    if not begin:
        return
    start = begin.details
    try:
        started = parse_datetime(start["sent_at"])
        valid_start = (
            start["version"] == 1 and start["boot_id"] == envelope.boot_id
            and isinstance(start["sequence"], int) and start["sequence"] < envelope.seq
        )
    except (KeyError, TypeError, ValueError):
        return
    observed = ensure_utc(snapshot.observed_at)
    sent = ensure_utc(envelope.sent_at)
    if not (
        valid_start and envelope.boot_id == snapshot.connector_boot_id
        and started <= observed <= sent
        and 0 <= (sent - started).total_seconds() <= 600
        and 0 <= (sent - observed).total_seconds() <= 30
        and ensure_utc(snapshot.received_at) >= ensure_utc(begin.created_at)
    ):
        return
    session.add(DeviceCommandEvent(command_id=command.id, status=ROSTER, details={
        "version": 1, "boot_id": envelope.boot_id, "sequence": envelope.seq,
        "sent_at": sent.isoformat(), "snapshot_id": snapshot.id,
        "snapshot_revision": snapshot.revision, "state_hash": snapshot.state_hash,
        "observed_at": observed.isoformat(),
    }))


def ordered_refresh_proven(session, command, snapshot, *, boot_id, requested_at):
    rows = [_event(session, command.id, status) for status in (BEGIN, ROSTER, END)]
    if any(row is None for row in rows):
        return False
    begin, roster, end = rows
    a, b, c = (row.details for row in rows)
    try:
        return bool(
            command.status == "SUCCEEDED"
            and all(d["version"] == 1 and d["boot_id"] == boot_id for d in (a, b, c))
            and a["sequence"] < b["sequence"] < c["sequence"]
            and b["snapshot_id"] == snapshot.id
            and b["snapshot_revision"] == snapshot.revision
            and b["state_hash"] == snapshot.state_hash
            and parse_datetime(b["observed_at"]) == ensure_utc(snapshot.observed_at)
            and parse_datetime(a["sent_at"]) <= ensure_utc(snapshot.observed_at)
            <= parse_datetime(b["sent_at"]) <= parse_datetime(c["sent_at"])
            and (parse_datetime(c["sent_at"]) - parse_datetime(a["sent_at"])).total_seconds() <= 600
            and ensure_utc(requested_at) <= ensure_utc(begin.created_at)
            <= ensure_utc(snapshot.received_at) <= ensure_utc(roster.created_at)
            <= ensure_utc(end.created_at)
            and (ensure_utc(end.created_at) - ensure_utc(begin.created_at)).total_seconds() <= 600
        )
    except (KeyError, TypeError, ValueError):
        return False

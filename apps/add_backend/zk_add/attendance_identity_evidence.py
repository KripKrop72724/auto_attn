"""Read-only identity evaluation shared by checks, execution and final delivery.

A roster is an observation, not proof about all preceding time. Continuity starts
at the first retained observation and breaks on partial snapshots or replacement.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.crypto import cnic_lookup, decrypt_cnic, decrypt_text, encrypt_text
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    Connector,
    DeviceUser,
    DeviceUserSnapshot,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc


@dataclass(frozen=True)
class IdentityEvidence:
    user_id: int
    encrypted_cnic: str
    cnic_hash: str
    display_name: str | None
    fingerprint: str | None
    proof: dict


def record_identity_observation(
    session: Session, connector: Connector, snapshot: DeviceUserSnapshot
) -> None:
    """Called in the snapshot transaction; rollback removes both observation and history."""
    zkt = connector.zkt_device
    if zkt is None:
        return
    previous = session.scalars(
        select(AttendanceIdentityHistory)
        .where(
            AttendanceIdentityHistory.zkt_device_id == zkt.id,
            AttendanceIdentityHistory.closed.is_(False),
        )
        .with_for_update()
    ).all()
    by_user = {row.device_user_id: row for row in previous}
    serial = zkt.confirmed_serial
    good_snapshot = snapshot.complete and snapshot.stable and serial and serial == zkt.serial
    users = (
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all()
        if good_snapshot
        else []
    )
    continued: set[int] = set()
    for user in users:
        old = by_user.get(user.id)
        if user.identity_conflict_code or not valid_cnic(
            user.cnic_encrypted, user.cnic_lookup_hash
        ):
            if old:
                old.revoked = True
            continue
        observed = ensure_utc(snapshot.observed_at)
        if (
            old
            and not old.revoked
            and (
                old.terminal_serial == serial
                and old.user_id == user.user_id
                and old.uid == user.uid
                and old.fingerprint == user.terminal_identity_fingerprint
                and old.cnic_lookup_hash == user.cnic_lookup_hash
                and old.last_revision + 1 == snapshot.revision
                and observed >= ensure_utc(old.observed_until)
            )
        ):
            old.last_snapshot_id = snapshot.id
            old.last_revision = snapshot.revision
            old.observed_until = observed
            continued.add(old.id)
        else:
            session.add(
                AttendanceIdentityHistory(
                    zkt_device_id=zkt.id,
                    device_user_id=user.id,
                    terminal_serial=serial,
                    user_id=user.user_id,
                    uid=user.uid,
                    fingerprint=user.terminal_identity_fingerprint,
                    cnic_encrypted=user.cnic_encrypted,
                    cnic_lookup_hash=user.cnic_lookup_hash,
                    display_name_encrypted=encrypt_text(user.display_name),
                    first_snapshot_id=snapshot.id,
                    last_snapshot_id=snapshot.id,
                    last_revision=snapshot.revision,
                    observed_from=observed,
                    observed_until=observed,
                )
            )
    for old in previous:
        if old.id not in continued:
            old.closed = True


def valid_cnic(encrypted: str | None, digest: str | None) -> bool:
    try:
        value = decrypt_cnic(encrypted)
        return bool(value and digest and cnic_lookup(value) == digest)
    except Exception:
        return False


def source_evidence(session: Session, event: AttendanceEvent, connector: Connector) -> dict | None:
    from zk_add.models import TerminalRecordManifest, TerminalSourceEpoch

    if event.source in {"LIVE", "LIVE_POLL"}:
        return {"kind": "CAPTURE_BINDING", "serial": event.device_serial}
    manifest = session.scalar(
        select(TerminalRecordManifest)
        .where(
            TerminalRecordManifest.attendance_event_id == event.id,
        )
        .order_by(TerminalRecordManifest.canonical_source.desc(), TerminalRecordManifest.id.desc())
        .limit(1)
    )
    if manifest:
        if not (
            manifest.canonical_source
            and manifest.connector_id == connector.id
            and manifest.zkt_device_id == event.zkt_device_id
            and manifest.terminal_serial == event.device_serial
            and manifest.disposition == "EVENT"
            and (not manifest.observed_user_id or manifest.observed_user_id == event.user_id)
            and (not event.uid or not manifest.observed_uid or manifest.observed_uid == event.uid)
        ):
            return None
        epoch = (
            session.get(TerminalSourceEpoch, manifest.source_epoch_id)
            if manifest.source_epoch_id
            else None
        )
        if manifest.source_epoch_id and (
            not epoch
            or epoch.state != "ACTIVE"
            or epoch.zkt_device_id != event.zkt_device_id
            or epoch.terminal_generation != manifest.generation
        ):
            return None
        return {
            "kind": "SAVED_TERMINAL_RECORD",
            "manifest_id": manifest.id,
            "digest": manifest.raw_record_digest,
            "generation": manifest.generation,
            "epoch_id": manifest.source_epoch_id,
        }
    if (event.raw_event or {}).get("reconciliation_source") == "VERIFIED_TERMINAL_SOURCE":
        return {"kind": "VERIFIED_TERMINAL_SOURCE", "serial": event.device_serial}
    return None


def identity_evidence(
    session: Session, event: AttendanceEvent, connector: Connector
) -> IdentityEvidence | None:
    """No writes, guesses from names, tolerance before the first observation, or cross-terminal joins."""
    zkt = connector.zkt_device
    if (
        zkt is None
        or not event.device_serial
        or event.device_serial != zkt.confirmed_serial
        or zkt.serial != zkt.confirmed_serial
    ):
        return None
    if event.ords_status == "QUARANTINED_IDENTITY_REUSE" or event.identity_resolution_status in {
        "QUARANTINED_REUSE",
        "BLOCKED_SOURCE_CONFLICT",
    }:
        return None
    source = source_evidence(session, event, connector)
    if source is None:
        return None
    when = ensure_utc(event.device_event_time)
    tolerance = timedelta(seconds=settings.identity_snapshot_capture_tolerance_seconds)
    history_query = select(AttendanceIdentityHistory).where(
        AttendanceIdentityHistory.zkt_device_id == zkt.id,
        AttendanceIdentityHistory.terminal_serial == event.device_serial,
        AttendanceIdentityHistory.user_id == event.user_id,
        AttendanceIdentityHistory.revoked.is_(False),
        AttendanceIdentityHistory.observed_from <= when,
        (AttendanceIdentityHistory.observed_until >= when)
        | (
            AttendanceIdentityHistory.closed.is_(False)
            & (AttendanceIdentityHistory.observed_until >= when - tolerance)
        ),
    )
    if event.uid:
        history_query = history_query.where(AttendanceIdentityHistory.uid == event.uid)
    if event.device_user_id:
        history_query = history_query.where(
            AttendanceIdentityHistory.device_user_id == event.device_user_id
        )
    histories = session.scalars(
        history_query.order_by(AttendanceIdentityHistory.id.desc()).limit(3)
    ).all()
    # Multiple observation intervals are not interchangeable around an identity change.
    if len(histories) == 1:
        h = histories[0]
        user = session.get(DeviceUser, h.device_user_id)
        if (
            user
            and not user.identity_conflict_code
            and (
                not event.identity_terminal_fingerprint
                or event.identity_terminal_fingerprint == h.fingerprint
            )
            and valid_cnic(h.cnic_encrypted, h.cnic_lookup_hash)
        ):
            return IdentityEvidence(
                h.device_user_id,
                h.cnic_encrypted,
                h.cnic_lookup_hash,
                decrypt_text(h.display_name_encrypted),
                h.fingerprint,
                {
                    "kind": "RETAINED_INTERVAL",
                    "source": source,
                    "history_id": h.id,
                    "terminal": h.terminal_serial,
                    "user_id": h.user_id,
                    "uid": h.uid,
                    "cnic_hash": h.cnic_lookup_hash,
                },
            )
        return None
    if histories:
        return None
    # Legacy observations can prove only their existing global continuity window.
    # A changed snapshot id alone does not invalidate an unchanged employee.
    if not (
        zkt.snapshot_complete
        and zkt.identity_snapshot_stable
        and zkt.identity_snapshot_id
        and zkt.last_identity_change_at
        and zkt.identity_snapshot_observed_at
        and ensure_utc(zkt.last_identity_change_at)
        <= when
        <= ensure_utc(zkt.identity_snapshot_observed_at) + tolerance
    ):
        return None
    users = session.scalars(
        select(DeviceUser)
        .where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_id == event.user_id,
        )
        .limit(2)
    ).all()
    if len(users) != 1:
        return None
    user = users[0]
    if not (
        user.present
        and user.lifecycle_state == "ACTIVE"
        and not user.identity_conflict_code
        and user.snapshot_revision == zkt.identity_snapshot_revision
        and (not event.uid or user.uid == event.uid)
        and (not event.device_user_id or user.id == event.device_user_id)
        and (
            not event.identity_terminal_fingerprint
            or event.identity_terminal_fingerprint == user.terminal_identity_fingerprint
        )
        and valid_cnic(user.cnic_encrypted, user.cnic_lookup_hash)
    ):
        return None
    return IdentityEvidence(
        user.id,
        user.cnic_encrypted,
        user.cnic_lookup_hash,
        user.display_name,
        user.terminal_identity_fingerprint,
        {
            "kind": "LEGACY_CONTINUITY",
            "source": source,
            "user_key": user.id,
            "terminal": event.device_serial,
            "user_id": user.user_id,
            "uid": user.uid,
            "cnic_hash": user.cnic_lookup_hash,
            "from": ensure_utc(zkt.last_identity_change_at).isoformat(),
        },
    )

"""ADD/Oracle custody for Hikvision, using the existing strict name-CNIC rule.

The source policy is ADD-owned, never supplied by a connector heartbeat. Names
are excluded from attendance UIDs; conflicting encoded identities require review.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.crypto import encrypt_cnic, cnic_lookup, normalize_cnic, decrypt_text
from zk_add.db import Base
from zk_add.identity import parse_machine_name
from zk_add.models import AttendanceEvent, Connector, OrdsOutbox, DeviceUser, ZKTDevice
from zk_add.time_utils import utc_now


class HikvisionPolicy(Base):
    __tablename__ = "add_hikvision_policies"
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    terminal_serial: Mapped[str] = mapped_column(String(120))
    source_epoch: Mapped[str] = mapped_column(String(64))
    profile_id: Mapped[str] = mapped_column(String(80))
    mapping_revision: Mapped[int] = mapped_column(Integer, default=1)
    success_codes: Mapped[list] = mapped_column(JSON, default=list)
    excluded_codes: Mapped[list] = mapped_column(JSON, default=list)


def profile_identity(session, connector, employee_no):
    """Use the exact terminal employee number, never a name similarity match.

    An observed identifier reuse or conflicting profile requires review. Snapshot
    provenance is retained when importing preexisting history under the operator's
    name-CNIC mapping rule; this does not claim historical continuity was observed.
    """
    terminal = connector.zkt_device
    if not terminal or not terminal.snapshot_complete or not terminal.identity_snapshot_stable:
        return None, None
    rows = session.scalars(select(DeviceUser).where(
        DeviceUser.zkt_device_id == terminal.id, DeviceUser.user_id == employee_no,
    )).all()
    if len(rows) != 1:
        return None, None
    user = rows[0]
    if (not user.present or user.lifecycle_state != "ACTIVE" or user.identity_conflict_code
            or user.snapshot_revision != terminal.identity_snapshot_revision):
        return None, None
    parsed = parse_machine_name(decrypt_text(user.machine_name_encrypted))
    if not parsed.display_name or not normalize_cnic(parsed.cnic):
        return None, None
    return user, parsed


def repair_profile_identity_holds(session: Session, limit: int = 200) -> int:
    """Bounded recovery after a verified profile snapshot arrives after a punch."""
    from sqlalchemy.orm import aliased
    from zk_add.hikvision_evidence import HikvisionEvidence
    from zk_add.hikvision_protocol import normalize_observation
    from zk_add.hikvision_probe import decode_body

    other = aliased(DeviceUser)
    reused = select(other.id).where(
        other.zkt_device_id == DeviceUser.zkt_device_id,
        other.user_id == DeviceUser.user_id, other.id != DeviceUser.id,
    ).exists()
    candidates = session.scalars(select(HikvisionEvidence).join(
        AttendanceEvent, AttendanceEvent.event_uid == HikvisionEvidence.event_uid,
    ).join(DeviceUser, (DeviceUser.zkt_device_id == AttendanceEvent.zkt_device_id) &
           (DeviceUser.user_id == AttendanceEvent.user_id)).join(
        ZKTDevice, ZKTDevice.id == DeviceUser.zkt_device_id,
    ).join(HikvisionPolicy, HikvisionPolicy.connector_id == HikvisionEvidence.connector_id).where(
        HikvisionPolicy.enabled.is_(True), HikvisionPolicy.source_epoch == HikvisionEvidence.source_epoch,
        HikvisionPolicy.terminal_serial == HikvisionEvidence.terminal_serial,
        ZKTDevice.snapshot_complete.is_(True), ZKTDevice.identity_snapshot_stable.is_(True),
        DeviceUser.snapshot_revision == ZKTDevice.identity_snapshot_revision,
        HikvisionEvidence.disposition == "IDENTITY_BLOCKED",
        AttendanceEvent.ords_status == "BLOCKED_IDENTITY",
        AttendanceEvent.cnic_lookup_hash.is_(None),
        DeviceUser.present.is_(True), DeviceUser.lifecycle_state == "ACTIVE",
        DeviceUser.identity_conflict_code.is_(None), DeviceUser.cnic_lookup_hash.is_not(None),
        ~reused,
    ).order_by(HikvisionEvidence.id).limit(limit)).all()
    resolved = 0
    for evidence in candidates:
        connector = session.get(Connector, evidence.connector_id)
        raw = decode_body(decrypt_text(evidence.raw_encrypted).encode())
        observation = normalize_observation(raw, terminal_serial=evidence.terminal_serial,
                                            source_epoch=evidence.source_epoch)
        deliver_observation(session, connector, evidence, observation, raw)
        resolved += evidence.disposition == "ATTENDANCE"
    session.flush()
    return resolved


def _hold_existing(session, row, code):
    if not row:
        return
    row.identity_resolution_status = "BLOCKED_SOURCE_CONFLICT"
    row.ords_status = code
    outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id))
    if outbox:
        outbox.status = code
        outbox.next_attempt_at = None


def deliver_observation(
    session: Session, connector: Connector, evidence, observation, raw_payload=None
) -> None:
    """Same transaction as raw custody; never call ORDS inside this transaction."""
    from zk_add.service import attendance_device_time_is_plausible

    existing = (
        session.scalar(
            select(AttendanceEvent).where(
                AttendanceEvent.event_uid == evidence.event_uid,
            )
        )
        if evidence.event_uid
        else None
    )
    if evidence.disposition == "SOURCE_FACT_CONFLICT":
        _hold_existing(session, existing, "QUARANTINED_SOURCE_CONFLICT")
        return
    if observation is None:
        return
    policy = session.get(HikvisionPolicy, connector.id)
    if not policy or not policy.enabled:
        return
    if (
        policy.source_epoch != evidence.source_epoch
        or policy.terminal_serial != evidence.terminal_serial
    ):
        evidence.disposition = "SOURCE_EPOCH_REVIEW"
        return
    code = [observation.major, observation.minor]
    if code in policy.excluded_codes and code not in policy.success_codes:
        evidence.disposition = "NON_ATTENDANCE"
        return
    if code not in policy.success_codes or code in policy.excluded_codes:
        evidence.disposition = "UNCLASSIFIED_SOURCE"
        return
    if not observation.employee_no:
        evidence.disposition = "IDENTITY_BLOCKED"
        return
    root = raw_payload if isinstance(raw_payload, dict) else {}
    root = root.get("EventNotificationAlert", root)
    event = root.get("AccessControllerEvent", root) if isinstance(root, dict) else {}
    name = event.get("name") if isinstance(event, dict) else None
    parsed = parse_machine_name(name if isinstance(name, str) and len(name) <= 256 else None)
    cnic = normalize_cnic(parsed.cnic) if parsed.display_name else None
    user = None
    identity_source = "TERMINAL_NAME_CNIC" if cnic else "MISSING_NAME_CNIC"
    if not cnic:
        user, mapped = profile_identity(session, connector, observation.employee_no)
        if mapped:
            parsed = mapped
            cnic = normalize_cnic(parsed.cnic)
            identity_source = "VERIFIED_PROFILE_NAME_CNIC"
    lookup = cnic_lookup(cnic)
    if existing:
        if existing.connector_id != connector.id:
            raise ValueError("ATTENDANCE_CONNECTOR_CONFLICT")
        if lookup and existing.cnic_lookup_hash and lookup != existing.cnic_lookup_hash:
            evidence.disposition = "IDENTITY_FACT_CONFLICT"
            _hold_existing(session, existing, "QUARANTINED_IDENTITY_CONFLICT")
            return
        if (cnic and not existing.cnic_lookup_hash and existing.ords_status == "BLOCKED_IDENTITY"
                and existing.identity_resolution_status == "BLOCKED_PROVENANCE"
                and existing.oracle_confirmed_at is None):
            outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == existing.id))
            if not outbox or outbox.status != "BLOCKED_IDENTITY" or outbox.attempt_count:
                return
            existing.cnic_encrypted = encrypt_cnic(cnic)
            existing.cnic_lookup_hash = lookup
            existing.cnic_last4 = cnic[-4:]
            existing.display_name = parsed.display_name
            existing.raw_punch = bool(parsed.shift_worker)
            existing.identity_resolution_status = "RESOLVED_HIKVISION_NAME"
            existing.identity_resolved_at = utc_now()
            existing.device_user_id = user.id if user else None
            existing.identity_snapshot_id = connector.zkt_device.identity_snapshot_id if user else None
            existing.raw_event = {**existing.raw_event, "identity_source": identity_source,
                                  "identity_profile_version": user.row_version if user else None,
                                  "identity_snapshot_revision": user.snapshot_revision if user else None}
            existing.ords_status = outbox.status = "PENDING"
            evidence.disposition = "ATTENDANCE"
            return
        # An existing non-null identity is pinned. Corrections use controlled repair.
        evidence.disposition = "DUPLICATE_OBSERVATION"
        return
    event_time = datetime.fromisoformat(observation.event_time_utc)
    captured = datetime.fromtimestamp(evidence.captured_epoch, timezone.utc)
    valid_time = attendance_device_time_is_plausible(event_time, captured)
    terminal = connector.zkt_device
    status = (
        "QUARANTINED_INVALID_DEVICE_TIME"
        if not valid_time
        else ("PENDING" if cnic else "BLOCKED_IDENTITY")
    )
    row = AttendanceEvent(
        event_uid=observation.event_uid,
        connector_id=connector.id,
        zkt_device_id=terminal.id,
        device_user_id=user.id if user else None,
        identity_snapshot_id=terminal.identity_snapshot_id if user else None,
        identity_resolution_status="RESOLVED_HIKVISION_NAME" if cnic else "BLOCKED_PROVENANCE",
        identity_resolved_at=utc_now() if cnic else None,
        device_serial=evidence.terminal_serial,
        uid=None,
        user_id=observation.employee_no,
        display_name=parsed.display_name or None,
        cnic_encrypted=encrypt_cnic(cnic),
        cnic_lookup_hash=lookup,
        cnic_last4=cnic[-4:] if cnic else None,
        device_event_time=event_time,
        captured_at=captured,
        source="LIVE_POLL" if evidence.channel == "POLL" else "FULL_HISTORY",
        status=observation.attendance_status,
        punch=None,
        raw_punch=bool(cnic and parsed.shift_worker),
        clock_quality="UNKNOWN" if valid_time else "INVALID",
        sequence=observation.serial_no,
        raw_event={
            "source_protocol": "hikvision-isapi-v1",
            "source_epoch": policy.source_epoch,
            "source_event_id": str(observation.serial_no),
            "major": observation.major,
            "minor": observation.minor,
            "mapping_revision": policy.mapping_revision,
            "identity_source": identity_source,
            "identity_profile_version": user.row_version if user else None,
            "identity_snapshot_revision": user.snapshot_revision if user else None,
            "timezone_assumed": observation.timezone_assumed,
        },
        ords_status=status,
    )
    session.add(row)
    session.flush()
    session.add(
        OrdsOutbox(
            attendance_event_id=row.id,
            status=status,
            delivery_type="LIVE" if evidence.channel == "POLL" else "FULL_HISTORY",
        )
    )
    evidence.disposition = (
        "INVALID_TIME" if not valid_time else ("ATTENDANCE" if cnic else "IDENTITY_BLOCKED")
    )


def configure_policy(
    session,
    connector,
    *,
    terminal_serial,
    source_epoch,
    profile_id,
    success_codes,
    excluded_codes,
    enabled,
    actor,
    reason,
    idempotency_key,
):
    from zk_add.audit import append_audit
    from zk_add.models import AuditEvent

    session.scalar(select(Connector).where(Connector.id == connector.id).with_for_update())
    terminal = connector.zkt_device
    if (
        connector.firmware_family != "hikvision"
        or not terminal
        or terminal.confirmed_serial != terminal_serial
        or terminal.serial != terminal_serial
    ):
        raise ValueError("HIKVISION_TERMINAL_BINDING_REQUIRED")
    if not source_epoch or len(source_epoch) > 64 or not profile_id or len(profile_id) > 80:
        raise ValueError("HIKVISION_PROFILE_REQUIRED")
    if not success_codes or len(success_codes) > 32 or len(excluded_codes) > 256:
        raise ValueError("INVALID_EVENT_MAPPING")
    for code in [*success_codes, *excluded_codes]:
        if (
            not isinstance(code, (list, tuple))
            or len(code) != 2
            or any(type(v) is not int or not 0 <= v <= 65535 for v in code)
        ):
            raise ValueError("INVALID_EVENT_MAPPING")
    success_codes = sorted({tuple(code) for code in success_codes})
    excluded_codes = sorted({tuple(code) for code in excluded_codes})
    if set(success_codes) & set(excluded_codes):
        raise ValueError("CONFLICTING_EVENT_MAPPING")
    request = {
        "terminal_serial": terminal_serial,
        "source_epoch": source_epoch,
        "profile_id": profile_id,
        "success_codes": [list(code) for code in success_codes],
        "excluded_codes": [list(code) for code in excluded_codes],
        "enabled": bool(enabled),
        "capture_mode": "poll",
        "poll_interval_seconds": 5,
        "identity_rule": "name-cnic",
        "reason": reason,
    }
    prior = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "HIKVISION_POLICY_CONFIGURED",
            AuditEvent.target_id == connector.connector_id,
            AuditEvent.request_id == idempotency_key,
        )
    )
    policy = session.get(HikvisionPolicy, connector.id)
    if prior:
        if prior.after != request:
            raise ValueError("IDEMPOTENCY_CONFLICT")
        return policy
    if policy and (
        policy.source_epoch != source_epoch or policy.terminal_serial != terminal_serial
    ):
        raise ValueError("SOURCE_EPOCH_CHANGE_REQUIRES_REVIEW")
    if not policy:
        policy = HikvisionPolicy(
            connector_id=connector.id,
            terminal_serial=terminal_serial,
            source_epoch=source_epoch,
            mapping_revision=0,
        )
        session.add(policy)
    policy.profile_id = profile_id
    policy.success_codes = request["success_codes"]
    policy.excluded_codes = request["excluded_codes"]
    policy.enabled = bool(enabled)
    policy.mapping_revision += 1
    append_audit(
        session,
        actor=actor,
        action="HIKVISION_POLICY_CONFIGURED",
        target_type="connector",
        target_id=connector.connector_id,
        request_id=idempotency_key,
        outcome="ENABLED" if enabled else "DISABLED",
        after=request,
    )
    session.flush()
    return policy

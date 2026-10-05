"""Derived source attendance for an explicitly authorized experimental cutover.

Original source rows, aliases and interpretation chains are never rewritten.
No API or heartbeat creates a cutover here: the release handoff must first prove
its quiesced source boundary and legacy custody. Without that persisted permit,
normal custody inspection retains its existing non-activating behavior.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import base64
import hashlib

from sqlalchemy import select

from zk_add.attendance_repair import _protected_digest
from zk_add.crypto import decrypt_json, decrypt_text
from zk_add.models import (AttendanceEvent, OrdsOutbox, TerminalRecordManifest,
    TerminalSourceEpoch, ZktCustodyWork, ZktDerivedEvidence, ZktOccurrenceAlias,
    ZktSourceAttendance, ZktSourceCutover)
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_decode import DECODER_VERSION, MODEL_PROFILES, decode_source
from zk_add import zkt_derived_evidence as derived

POLICY = "EXPERIMENTAL_SOURCE_ATTENDANCE_V1"
AUTHORITY = "QUIESCED_LEGACY_CUSTODY_AND_SOURCE_BOUNDARY_V1"
MAX_EPOCHS = 32


class SourceAttendanceHold(ValueError):
    """Fixed reason codes only; no employee or raw source material."""


def cutover_material(cutover, connector, epoch, *, migration_digest, writer_digest, boot_id):
    """Canonical format for a future verified release handoff, not an activator."""
    return dict(schema_version=1, authority=AUTHORITY, policy=POLICY,
        connector_id=connector.connector_id, terminal_serial=cutover.terminal_serial,
        source_epoch=epoch.epoch_id, first_new_ordinal=cutover.first_new_ordinal,
        model=cutover.model, record_size=cutover.record_size, decoder_version=DECODER_VERSION,
        migration_digest=migration_digest, writer_digest=writer_digest, boot_id=boot_id)


def _cutover(session, connector):
    cutover = session.scalar(select(ZktSourceCutover).where(ZktSourceCutover.connector_id == connector.id))
    if cutover is None:
        return None
    from zk_add.zkt_oracle_delivery import TOKEN
    terminal = connector.zkt_device
    epoch = session.get(TerminalSourceEpoch, cutover.source_epoch_id)
    value = decrypt_json(cutover.protected_authority)
    if (terminal is None or epoch is None or connector.firmware_family != "zkt"
            or epoch.zkt_device_id != terminal.id or cutover.terminal_serial != terminal.serial
            or cutover.terminal_serial != terminal.confirmed_serial or cutover.model != terminal.model
            or cutover.model not in MODEL_PROFILES or cutover.record_size not in {8, 16, 40}
            or type(cutover.first_new_ordinal) is not int or not 0 <= cutover.first_new_ordinal < 2**31
            or not isinstance(value, dict) or not isinstance(value.get("boot_id"), str)
            or not 1 <= len(value["boot_id"]) <= 100
            or any(not isinstance(value.get(key), str) or not TOKEN.fullmatch(value[key])
                   for key in ("migration_digest", "writer_digest"))
            or _protected_digest(value) != cutover.authority_digest
            or value != cutover_material(cutover, connector, epoch,
                migration_digest=value["migration_digest"], writer_digest=value["writer_digest"], boot_id=value["boot_id"])):
        raise SourceAttendanceHold("SOURCE_CUTOVER_EVIDENCE_CHANGED")
    return cutover


def _manifest(session, connector, alias):
    manifest = session.get(TerminalRecordManifest, alias.manifest_id) if alias else None
    terminal = connector.zkt_device
    if (manifest is None or terminal is None or alias.zkt_device_id != terminal.id
            or manifest.connector_id != connector.id or manifest.zkt_device_id != terminal.id
            or not manifest.canonical_source or manifest.terminal_serial != terminal.serial
            or terminal.confirmed_serial != terminal.serial
            or manifest.disposition != "RAW_PRESERVED" or manifest.attendance_event_id is not None
            or alias.attendance_event_id is not None
            or (alias.source_epoch_id, alias.ordinal, alias.raw_digest) != (
                manifest.source_epoch_id, manifest.ordinal, manifest.raw_record_digest)):
        raise SourceAttendanceHold("SOURCE_DERIVATION_BINDING_CHANGED")
    from zk_add.zkt_custody import occurrence_id
    epoch = session.get(TerminalSourceEpoch, manifest.source_epoch_id)
    if (epoch is None or epoch.zkt_device_id != terminal.id or epoch.terminal_generation != manifest.generation
            or alias.occurrence_id != occurrence_id(manifest.terminal_serial, epoch.epoch_id,
                manifest.ordinal, manifest.raw_record_digest)):
        raise SourceAttendanceHold("SOURCE_DERIVATION_EPOCH_CHANGED")
    return manifest, epoch


def _canonical(session, connector, alias, cutover):
    """Only exact copies of an ancestor ordinal share its attendance identity."""
    manifest, epoch = _manifest(session, connector, alias)
    if manifest.record_size != cutover.record_size:
        raise SourceAttendanceHold("SOURCE_PROFILE_LAYOUT_CHANGED")
    root = alias
    seen = set()
    reached = False
    for _ in range(MAX_EPOCHS):
        if epoch.id in seen:
            break
        seen.add(epoch.id)
        reached |= epoch.id == cutover.source_epoch_id
        if epoch.parent_epoch_id is None:
            if not reached:
                raise SourceAttendanceHold("SOURCE_CUTOVER_EPOCH_UNRELATED")
            if manifest.ordinal < cutover.first_new_ordinal:
                raise SourceAttendanceHold("SOURCE_BEFORE_CUTOVER")
            return root
        parent = session.get(TerminalSourceEpoch, epoch.parent_epoch_id)
        if (parent is None or parent.zkt_device_id != epoch.zkt_device_id
                or parent.terminal_generation != epoch.terminal_generation or parent.sequence >= epoch.sequence):
            raise SourceAttendanceHold("SOURCE_RECOVERY_PARENT_CHANGED")
        prior = session.scalar(select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id == manifest.zkt_device_id,
            TerminalRecordManifest.source_epoch_id == parent.id,
            TerminalRecordManifest.canonical_source.is_(True), TerminalRecordManifest.ordinal == manifest.ordinal))
        if prior is not None:
            if epoch.id == cutover.source_epoch_id:
                raise SourceAttendanceHold("SOURCE_PREDATES_CUTOVER_EPOCH")
            if (prior.raw_record_digest != manifest.raw_record_digest or prior.record_size != manifest.record_size
                    or prior.terminal_serial != manifest.terminal_serial or prior.connector_id != connector.id):
                raise SourceAttendanceHold("SOURCE_CHANGED_OCCURRENCE_REVIEW")
            ancestor = session.scalar(select(ZktOccurrenceAlias).where(ZktOccurrenceAlias.manifest_id == prior.id))
            _manifest(session, connector, ancestor)
            root = ancestor
            manifest = prior
        elif manifest.source_kind == "RECOVERY_PREFIX":
            raise SourceAttendanceHold("SOURCE_RECOVERY_PREFIX_MISSING")
        epoch = parent
    raise SourceAttendanceHold("SOURCE_RECOVERY_ANCESTRY_INVALID")


def _facts_material(event):
    return dict(event_uid=event.event_uid, connector_id=event.connector_id, zkt_device_id=event.zkt_device_id,
        terminal_serial=event.device_serial, user_id=event.user_id, uid=event.uid,
        device_event_time=ensure_utc(event.device_event_time).isoformat(),
        captured_at=ensure_utc(event.captured_at).isoformat(), source=event.source,
        status=event.status, punch=event.punch, source_evidence={key: (event.raw_event or {}).get(key) for key in (
            "reconciliation_source", "source_policy", "profile_qualification", "source_epoch_id",
            "source_ordinal", "raw_record_digest", "encoded_time", "interpretation_digest")})


def binding_event(session, connector, alias):
    """Validate a derived link for delivery without changing the custody row."""
    link = session.scalar(select(ZktSourceAttendance).where(ZktSourceAttendance.occurrence_alias_id == alias.id))
    if link is None:
        return None
    cutover = _cutover(session, connector)
    if cutover is None or cutover.id != link.cutover_id:
        raise SourceAttendanceHold("SOURCE_CUTOVER_EVIDENCE_CHANGED")
    root = _canonical(session, connector, alias, cutover)
    event = session.get(AttendanceEvent, link.attendance_event_id)
    evidence = session.get(ZktDerivedEvidence, link.evidence_id)
    work = session.get(ZktCustodyWork, link.work_id)
    if (root.id != link.canonical_alias_id or event is None or event.event_uid != root.occurrence_id
            or event.connector_id != connector.id or event.zkt_device_id != alias.zkt_device_id
            or event.device_serial != cutover.terminal_serial or work is None
            or work.source_manifest_id != alias.manifest_id or work.connector_id != connector.id
            or work.kind != "SOURCE_LEDGER" or evidence is None or evidence.work_id != work.id
            or evidence.evidence_digest != link.evidence_digest
            or evidence.result != "UNQUALIFIED_FACTS"
            or _protected_digest(_facts_material(event)) != link.facts_digest):
        raise SourceAttendanceHold("SOURCE_DERIVED_ATTENDANCE_CHANGED")
    manifest = session.get(TerminalRecordManifest, alias.manifest_id)
    try:
        encoded = decrypt_text(manifest.protected_raw_record)
        if not encoded or len(encoded) > 684:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        steps = list(derived.verified_steps(session, work, raw))
        facts = decode_source(raw, record_size=cutover.record_size)
        if (hashlib.sha256(raw).hexdigest() != alias.raw_digest or len(steps) != 1
                or evidence.evidence_digest != derived._digest(steps[0])
                or facts.user_id != event.user_id or event.uid is not None
                or ensure_utc(event.device_event_time) != facts.utc_time
                or (event.status, event.punch) != (str(facts.status), str(facts.punch))):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise SourceAttendanceHold("SOURCE_DERIVED_ATTENDANCE_CHANGED") from exc
    return event


def process_source(session, connector, work, raw):
    """One raw ledger record becomes attendance plus a durable owned intent.

    Caller holds connector/work locks and uses a savepoint around this mutation.
    Exceptions and unknown identities retain custody; current enrollment UIDs
    are never assigned from the historical attendance UID field.
    """
    cutover = _cutover(session, connector)
    if cutover is None or work.kind != "SOURCE_LEDGER":
        return False
    if not connector.zkt_custody_enabled:
        raise SourceAttendanceHold("SOURCE_CUSTODY_NOT_ENABLED")
    alias = session.scalar(select(ZktOccurrenceAlias).where(ZktOccurrenceAlias.manifest_id == work.source_manifest_id))
    root = _canonical(session, connector, alias, cutover)
    existing = binding_event(session, connector, alias)
    if existing is not None:
        work.state, work.reason_code, work.owner = "ATTENDANCE_CREATED", POLICY, "ADD_DELIVERY"
        return True
    steps = list(derived.verified_steps(session, work, raw))
    if (len(steps) != 1 or steps[0]["result"] != "UNQUALIFIED_FACTS"
            or steps[0]["plausible_layouts"] != [cutover.record_size]
            or len(steps[0]["records"]) != 1):
        raise SourceAttendanceHold("SOURCE_INTERPRETATION_NOT_USABLE")
    facts = decode_source(raw, record_size=cutover.record_size)
    expected = asdict(facts)
    expected.update(local_time=facts.local_time.isoformat(), utc_time=facts.utc_time.isoformat())
    if steps[0]["records"][0]["facts"] != expected:
        raise SourceAttendanceHold("SOURCE_INTERPRETATION_CHANGED")
    if facts.user_id is None:
        raise SourceAttendanceHold("SOURCE_IDENTITY_REFERENCE_UNAVAILABLE")
    evidence = session.scalar(select(ZktDerivedEvidence).where(ZktDerivedEvidence.work_id == work.id,
        ZktDerivedEvidence.interpretation_version == derived.INTERPRETATION_VERSION,
        ZktDerivedEvidence.input_fingerprint == derived.input_fingerprint(work), ZktDerivedEvidence.step_index == 0))
    if root.id != alias.id:
        event = binding_event(session, connector, root)
        if event is None:
            work.state, work.reason_code, work.owner = "WAIT_SOURCE", "SOURCE_ANCESTOR_ATTENDANCE_PENDING", "ADD_RECONCILIATION"
            work.next_attempt_at = utc_now() + timedelta(seconds=30)
            return True
    else:
        from zk_add.schemas import AttendanceEventIn
        from zk_add.service import ingest_attendance
        from zk_add.zkt_oracle_delivery import MEMBERSHIP_SCOPE, register_intent
        manifest = session.get(TerminalRecordManifest, alias.manifest_id)
        # An unexplained pre-existing UID is a collision, not permission to
        # adopt a possibly unrelated event or replace its Oracle key.
        if session.scalar(select(AttendanceEvent.id).where(AttendanceEvent.event_uid == alias.occurrence_id)):
            raise SourceAttendanceHold("SOURCE_CANONICAL_UID_ALREADY_EXISTS")
        accepted, duplicate = ingest_attendance(session, connector=connector, events=[AttendanceEventIn(
            event_uid=alias.occurrence_id, terminal_serial=manifest.terminal_serial, uid=None,
            user_id=facts.user_id, raw_name=None, device_event_time=facts.utc_time,
            captured_at=ensure_utc(manifest.created_at), source="CURRENT_RECONCILE",
            status=str(facts.status), punch=str(facts.punch), clock_quality="UNKNOWN",
            raw_event=dict(reconciliation_source="VERIFIED_TERMINAL_SOURCE", source_policy=POLICY,
                profile_qualification="NOT_ASSERTED", source_epoch_id=manifest.source_epoch_id,
                source_ordinal=manifest.ordinal, raw_record_digest=manifest.raw_record_digest,
                encoded_time=facts.encoded_time, interpretation_digest=evidence.evidence_digest))],
            defer_synced_identity_release=True)
        if accepted != [alias.occurrence_id] or duplicate:
            raise SourceAttendanceHold("SOURCE_CANONICAL_INGEST_CONFLICT")
        event = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == alias.occurrence_id))
    link = ZktSourceAttendance(occurrence_alias_id=alias.id, canonical_alias_id=root.id,
        cutover_id=cutover.id, work_id=work.id, evidence_id=evidence.id, evidence_digest=evidence.evidence_digest,
        attendance_event_id=event.id, facts_digest=_protected_digest(_facts_material(event)))
    session.add(link)
    session.flush()
    if root.id == alias.id:
        outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id))
        if outbox is None:
            outbox = OrdsOutbox(attendance_event_id=event.id, delivery_type="CURRENT_RECONCILE", status=event.ords_status)
            session.add(outbox)
            session.flush()
        register_intent(session, connector=connector, event=event, outbox=outbox, alias=alias,
                        verification_scope=MEMBERSHIP_SCOPE)
    work.state, work.reason_code, work.owner = "ATTENDANCE_CREATED", POLICY, "ADD_DELIVERY"
    return True

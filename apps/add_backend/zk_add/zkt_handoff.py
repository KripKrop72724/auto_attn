"""Read-only checks joining the immutable writer boundary to retained source.

Telemetry preserves device claims. This module independently checks the raw
anchor against ADD custody; it does not issue a migration/delivery permit.
"""
from datetime import datetime
import base64
import hashlib

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import select

from zk_add.crypto import decrypt_text
from zk_add.models import ReconciliationCoverage, TerminalRecordManifest, TerminalSourceEpoch
from zk_add.schemas import SourceBoundaryDiagnostics
from zk_add.time_utils import ensure_utc, utc_now


def terminal_digest(serial: str) -> str:
    raw = serial.encode("ascii")
    if not 1 <= len(raw) <= 80:
        raise ValueError("SOURCE_BOUNDARY_TERMINAL_BINDING")
    value = bytearray(112)
    # Match the firmware's fixed-width buffer, including its zero padding.
    prefix = b"ZKT-JOURNAL-TERMINAL-V1"
    value[:len(prefix)] = prefix
    value[31:31 + len(raw)] = raw
    return hashlib.sha256(value).hexdigest()


def boundary_status(session, connector, *, now=None) -> dict:
    now = ensure_utc(now or utc_now())
    result = {"state": "HELD", "reason": "SOURCE_BOUNDARY_NOT_OBSERVED",
              "migration_certified": False, "delivery_permission": "NOT_EVALUATED"}
    def held(reason):
        return {**result, "reason": reason}
    if connector.firmware_family != "zkt" or connector.firmware_version not in {"2.7.0", "zone-lite-2.7.0"}:
        return held("SOURCE_BOUNDARY_WRITER_REQUIRED")
    diagnostics = connector.firmware_diagnostics
    if not isinstance(diagnostics, dict) or not diagnostics.get("source_boundary"):
        return result
    try:
        claim = SourceBoundaryDiagnostics.model_validate(diagnostics["source_boundary"])
    except ValidationError:
        return held("SOURCE_BOUNDARY_INVALID")
    if not claim.verified:
        return held("SOURCE_BOUNDARY_" + claim.result)
    try:
        sampled = datetime.fromisoformat(diagnostics["sampled_at"].replace("Z", "+00:00"))
        received = connector.firmware_diagnostics_at
        fresh = (sampled.tzinfo is not None and received is not None
                 and 0 <= (now - ensure_utc(sampled)).total_seconds() <= 45
                 and 0 <= (now - ensure_utc(received)).total_seconds() <= 45)
    except (KeyError, ValueError, TypeError, AttributeError):
        fresh = False
    if (not fresh or not connector.connected or not connector.boot_id
            or diagnostics.get("boot_id") != connector.boot_id
            or type(diagnostics.get("sample_sequence")) is not int
            or not 0 < diagnostics["sample_sequence"] <= connector.last_sequence):
        return held("SOURCE_BOUNDARY_STALE_TELEMETRY")
    if (connector.ota_image_sha256 != claim.writer_digest or not connector.ota_secure_boot
            or not connector.ota_rollback_enabled or connector.ota_running_partition not in {"ota_0", "ota_1"}
            or diagnostics.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or diagnostics.get("delivery_authority") != "ADD"):
        return held("SOURCE_BOUNDARY_WRITER_BINDING")
    terminal = connector.zkt_device
    try:
        bound = (terminal is not None and terminal.serial == terminal.confirmed_serial
                 and terminal_digest(terminal.serial) == claim.terminal_digest)
    except (ValueError, AttributeError, UnicodeError):
        bound = False
    if not bound:
        return held("SOURCE_BOUNDARY_TERMINAL_BINDING")
    coverage = session.scalar(select(ReconciliationCoverage).where(
        ReconciliationCoverage.zkt_device_id == terminal.id, ReconciliationCoverage.active.is_(True)))
    epoch = session.get(TerminalSourceEpoch, coverage.source_epoch_id) if coverage and coverage.source_epoch_id else None
    if (coverage is None or epoch is None or epoch.state != "ACTIVE"
            or epoch.zkt_device_id != terminal.id or coverage.terminal_serial != terminal.serial
            or coverage.terminal_generation != epoch.terminal_generation
            or coverage.capture_state not in {"SOURCE_CAPTURE_CERTIFIED", "SOURCE_CAPTURE_CERTIFIED_RAW_PENDING",
                                               "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"}
            or coverage.source_committed_cursor < claim.next_ordinal):
        return held("SOURCE_BOUNDARY_WAIT_SOURCE_COVERAGE")
    if claim.next_ordinal:
        anchor = session.scalar(select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id == terminal.id, TerminalRecordManifest.source_epoch_id == epoch.id,
            TerminalRecordManifest.generation == epoch.terminal_generation,
            TerminalRecordManifest.canonical_source.is_(True), TerminalRecordManifest.ordinal == claim.next_ordinal - 1))
        if (anchor is None or anchor.connector_id != connector.id or anchor.terminal_serial != terminal.serial
                or anchor.record_size != claim.record_size or anchor.raw_record_digest != claim.anchor_digest):
            return held("SOURCE_BOUNDARY_ANCHOR_MISMATCH")
        try:
            encoded = decrypt_text(anchor.protected_raw_record)
            if not isinstance(encoded, str) or len(encoded) > 56:
                raise ValueError()
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) != claim.record_size or hashlib.sha256(raw).hexdigest() != claim.anchor_digest:
                raise ValueError()
        except (ValueError, TypeError, InvalidToken):
            return held("SOURCE_BOUNDARY_RAW_EVIDENCE_CHANGED")
    return {**result, "state": "VERIFIED_SOURCE_ANCHOR", "reason": "SOURCE_BOUNDARY_MATCHES_ADD_CUSTODY",
            "sampled_at": ensure_utc(sampled).isoformat(),
            "source_epoch": epoch.epoch_id, "next_ordinal": claim.next_ordinal, "record_size": claim.record_size,
            "writer_digest": claim.writer_digest, "capture_epoch": claim.capture_epoch}

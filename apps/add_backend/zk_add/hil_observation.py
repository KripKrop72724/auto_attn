"""Bounded server-owned experimental writer HIL; never production qualification.

Completion consumes committed ADD rows. Callers supply only a run ID and actor;
there is no API for submitting a verdict, receipt, or a success checkbox. The
sealed result retains typed Oracle receipt scope and all unperformed gates.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
import re

from cryptography.fernet import InvalidToken
from sqlalchemy import exists, select, update
from sqlalchemy.orm import load_only

from zk_add.crypto import decrypt_text
from zk_add.hil_scope import HilTarget, target_matches
from zk_add.hil_startup import classify_startup
from zk_add.hil_validation import (
    AttendanceProof, RecoveryTest, ReleaseIdentity, SmokeEvidence, SmokeSample, evaluate_smoke,
)
from zk_add.hil_writer_evidence import collect_writer_attendance
from zk_add.models import (
    Connector, DeviceAlert, DeviceLog, DeviceTelemetry, ReconciliationCoverage,
    ReconciliationJob, SourceTailChunk, TerminalRecordManifest, TerminalSourceEpoch,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease
from zk_add.reconciliation import reconciliation_chain_digest
from zk_add.runtime_contract import RUNTIMES, journal_storage_status, worker_snapshot_fresh
from zk_add.time_utils import ensure_utc, utc_now

PROFILE = "FULL_REMOTE_HIL_V1"
COLLECTOR_VERSION = "ZKT_WRITER_OBSERVATION_V1"
SAMPLE_LIMIT = 2048
LOG_LIMIT = 2048
SOURCE_LIMIT = 512
TOKEN = re.compile(r"^[0-9a-f]{64}$")
RUNTIME = RUNTIMES["ZKT_JOURNAL_V1"]
# Actual firmware zone_lite.c retry codes. These report that the durable source
# cursor did not advance; none denotes an attendance persistence failure.
TRANSPORT_RETRY_CODES = frozenset({"CURRENT_TAIL_CHECKPOINT_RETRY", "ADD_SOURCE_CHECKPOINT_RETRY",
                                  "CURRENT_TAIL_AUDIT_RETRY"})


def evidence_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def accepted_full_evidence_valid(run, event_details):
    """Pure linkage check for rollout gates; a verdict label alone is insufficient."""
    try:
        result, sealed = run.result, run.result["evidence"]
        digest = result["evidence_sha256"]
        return bool(isinstance(event_details, dict) and isinstance(sealed, dict)
            and run.status == "HIL_ACCEPTED" and result.get("profile") == PROFILE
            and result.get("outcome") == sealed.get("outcome") == "PASS"
            and result.get("reasons") == sealed.get("reasons") == []
            and result.get("collector_version") == sealed.get("collector_version") == COLLECTOR_VERSION
            and event_details.get("collector_version") == COLLECTOR_VERSION
            and isinstance(digest, str) and TOKEN.fullmatch(digest)
            and event_details.get("evidence_sha256") == digest == evidence_digest(sealed)
            and sealed.get("baseline_sha256") == evidence_digest(run.baseline)
            and sealed.get("scope") == {"run_id": run.run_id, "deployment_id": run.deployment_id,
                                       "target": run.target, "release_identity": run.release_identity}
            and _time(sealed.get("window_start")) == ensure_utc(run.started_at)
            and _time(sealed.get("window_end")) == ensure_utc(run.ends_at)
            and _time(sealed.get("evaluated_at")) == ensure_utc(run.completed_at))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _integer(value):
    return type(value) is int and value >= 0


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_utc(parsed) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _queues(diagnostics):
    if not isinstance(diagnostics, dict):
        return None
    rows = diagnostics.get("queues")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    if any(not isinstance(row.get("name"), str) for row in rows):
        return None
    values = {row.get("name"): row for row in rows}
    if len(values) != len(rows) or not RUNTIME.queues <= values.keys():
        return None
    if any(values[name].get("count_known") is not True or not _integer(values[name].get("records"))
           for name in RUNTIME.queues):
        return None
    return {name: values[name]["records"] for name in sorted(RUNTIME.queues)}


def _sample(row, run, target, identity, logs):
    payload = row.payload if isinstance(row.payload, dict) else {}
    diagnostics = payload.get("diagnostics") or {}
    errors = []
    if not isinstance(diagnostics, dict):
        diagnostics = {}
        errors.append("DIAGNOSTICS_SHAPE_INVALID")
    storage, ota, terminal = (diagnostics.get("storage") or {}, payload.get("ota") or {},
                              payload.get("zkt") or {})
    if not all(isinstance(value, dict) for value in (storage, ota, terminal)):
        storage, ota, terminal = {}, {}, {}
        errors.append("TELEMETRY_SHAPE_INVALID")
    recorded, sampled = ensure_utc(row.created_at), _time(payload.get("_trusted_envelope_sent_at"))
    tick = diagnostics.get("sampled_uptime_ms")
    if (sampled is None or not 0 <= (recorded - sampled).total_seconds() <= 45
            or not _integer(tick) or not _integer(row.uptime_seconds)
            or not -5000 <= row.uptime_seconds * 1000 - tick <= 5000):
        errors.append("DIAGNOSTICS_FRESHNESS_UNPROVEN")
    if not row.boot_id or diagnostics.get("boot_id") != row.boot_id:
        errors.append("DIAGNOSTICS_BOOT_MISMATCH")
    if (diagnostics.get("schema_version") != 2 or diagnostics.get("journal_format") != 1
            or diagnostics.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or ota.get("running_version") != identity.version
            or ota.get("image_sha256") != identity.application_sha256
            or ota.get("running_partition") not in ("ota_0", "ota_1")
            or ota.get("secure_boot") is not True or ota.get("rollback_enabled") is not True):
        errors.append("SIGNED_WRITER_APPLICATION_MISMATCH")
    if diagnostics.get("delivery_authority") != "ADD":
        errors.append("DELIVERY_AUTHORITY_UNVERIFIED")
    reader_proof = None
    reader_pending = False
    if "reader_admission" in run.baseline:
        from zk_add.zkt_reader_evidence import qualified_reader_proof
        try:
            reader_proof = qualified_reader_proof(diagnostics, run.baseline["reader_admission"], row.uptime_seconds)
            if reader_proof != run.baseline.get("qualified_reader"):
                errors.append("QUALIFIED_READER_CHANGED")
        except (ValueError, TypeError):
            raw = diagnostics.get("qualified_reader")
            minimal = {key: value for key, value in raw.items() if value is not None} if isinstance(raw, dict) else {}
            reader_pending = (minimal == {"schema_version": 1, "verified": False,
                "matrix_sha256": run.baseline["reader_admission"].get("matrix_sha256")}
                and type(minimal.get("schema_version")) is int
                and isinstance(diagnostics.get("journal_runtime"), dict)
                and diagnostics["journal_runtime"].get("writer_ready") is False)
            errors.append("QUALIFIED_READER_PROOF_PENDING" if reader_pending else "QUALIFIED_READER_UNVERIFIED")
    workers = diagnostics.get("workers") or []
    if not isinstance(workers, list) or not all(isinstance(item, dict)
            and isinstance(item.get("name"), str) for item in workers):
        workers = []
        errors.append("WORKER_SHAPE_INVALID")
    names = {item.get("name") for item in workers}
    workers_healthy = (len(names) == len(workers)
        and RUNTIME.workers <= names <= RUNTIME.workers | RUNTIME.auxiliary_workers
        and all(item.get("state") in ("RUNNING", "WAITING_NETWORK")
                and worker_snapshot_fresh(item, row.uptime_seconds, RUNTIME) for item in workers))
    restarts = [item.get("restart_count") for item in workers]
    queues = _queues(diagnostics)
    source_generation, cursor, count = (diagnostics.get("source_generation"),
        diagnostics.get("committed_source_cursor"), terminal.get("attendance_count"))
    if _integer(cursor) and _integer(count) and cursor > count:
        errors.append("SOURCE_CURSOR_EXCEEDS_COUNT")
    faults = any(storage.get(key) for key in ("error_code", "upgrade_error", "persistence_probe_error",
        "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))
    if not isinstance(diagnostics.get("journal_storage", {}), dict):
        diagnostics = {**diagnostics, "journal_storage": {}}
        errors.append("JOURNAL_STORAGE_SHAPE_INVALID")
    try:
        journal_status = journal_storage_status(diagnostics, row.uptime_seconds)
    except (AttributeError, TypeError, ValueError):
        journal_status = "UNKNOWN"
        errors.append("JOURNAL_STORAGE_SHAPE_INVALID")
    storage_health = (storage.get("durability") == "HEALTHY" and not faults and journal_status == "HEALTHY")
    if storage.get("durability") is None or journal_status in {None, "UNKNOWN"}:
        storage_health = None
    reboot_incident = (diagnostics.get("journal_storage") or {}).get("hil_reboot_persistence_incident")
    if reboot_incident is True:
        storage_health = False
    elif reboot_incident is not False:
        storage_health = None
        errors.append("REBOOT_PERSISTENCE_INCIDENT_STATE_UNKNOWN")
    terminal_health = (terminal.get("serial") == target.terminal_serial and terminal.get("online") is True
                       and source_generation == run.baseline.get("source_generation"))
    if not terminal.get("serial") or type(terminal.get("online")) is not bool or not _integer(source_generation):
        terminal_health = None
    sample = SmokeSample(telemetry_id=row.id, recorded_at=recorded,
        diagnostics_at=sampled or recorded - timedelta(seconds=46), target=target, release=identity,
        boot_id=row.boot_id or "MISSING",
        storage_healthy=storage_health,
        persistence_verified=storage.get("persistence_verified") if type(storage.get("persistence_verified")) is bool else None,
        recovery_complete=storage.get("recovery_complete") if type(storage.get("recovery_complete")) is bool else None,
        workers_healthy=True if workers_healthy else None, counts_known=True if queues is not None else None,
        terminal_certified=terminal_health,
        source_generation=source_generation if _integer(source_generation) else None,
        committed_cursor=cursor if _integer(cursor) else None, source_count=count if _integer(count) else None,
        write_failures=storage.get("write_failures") if _integer(storage.get("write_failures")) else None,
        read_failures=storage.get("read_failures") if _integer(storage.get("read_failures")) else None,
        worker_restarts=sum(restarts) if restarts and all(_integer(value) for value in restarts) else None,
        # These are server-recorded rejections in this observation, scoped to
        # the sample's boot. Never substitute an absent firmware counter with 0.
        message_rejections=sum(log.code == "DEVICE_MESSAGE_REJECTED" and log.boot_id == row.boot_id
                               and ensure_utc(log.received_at) <= recorded for log in logs))
    details = {"sequence": row.sequence, "uptime_seconds": row.uptime_seconds, "queues": queues,
               "qualified_reader": reader_proof, "qualified_reader_pending": reader_pending,
               "worker_restart_counts": {item["name"]: item.get("restart_count") for item in workers},
               "errors": errors, **sample.model_dump(mode="json")}
    return sample, details


def _source(session, run, connector, end_cursor):
    errors, details = [], {"chunks": [], "manifest_ids": [], "raw_dispositions": {}}
    baseline = run.baseline
    coverage = session.scalar(select(ReconciliationCoverage).where(
        ReconciliationCoverage.coverage_id == baseline.get("coverage_id")))
    epoch = session.get(TerminalSourceEpoch, baseline.get("source_epoch_id")) if baseline.get("source_epoch_id") else None
    job = session.get(ReconciliationJob, coverage.job_id) if coverage else None
    if (coverage is None or epoch is None or job is None or connector is None or connector.zkt_device is None
            or not coverage.active or epoch.state != "ACTIVE" or epoch.superseded_at is not None
            or coverage.source_epoch_id != epoch.id or job.source_epoch_id != epoch.id
            or epoch.epoch_id != baseline.get("source_epoch")
            or epoch.zkt_device_id != connector.zkt_device.id or coverage.zkt_device_id != epoch.zkt_device_id
            or job.connector_id != connector.id or job.job_id != baseline.get("job_id")
            or coverage.terminal_generation != baseline.get("source_generation")
            or epoch.terminal_generation != baseline.get("source_generation")
            or coverage.terminal_serial != run.target.get("terminal_serial")
            or job.status != "COMPLETED" or not job.capture_certificate
            or evidence_digest(job.capture_certificate) != baseline.get("capture_certificate_sha256")):
        return details, ["SOURCE_BASELINE_OR_CERTIFICATE_CHANGED"], None, ()
    first, chain = baseline.get("source_cursor"), baseline.get("source_chain")
    if (not _integer(first) or not _integer(end_cursor) or end_cursor < first
            or not isinstance(chain, str) or TOKEN.fullmatch(chain) is None
            or end_cursor - first > SOURCE_LIMIT):
        return details, ["SOURCE_RANGE_UNVERIFIED_OR_LIMIT"], None, ()
    start, end = ensure_utc(run.started_at), ensure_utc(run.ends_at)
    chunks = list(session.scalars(select(SourceTailChunk).where(
        SourceTailChunk.coverage_id == coverage.id, SourceTailChunk.start_ordinal >= first,
        SourceTailChunk.committed_at <= end).order_by(SourceTailChunk.start_ordinal).limit(SOURCE_LIMIT + 1)))
    records = list(session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.zkt_device_id == epoch.zkt_device_id,
        TerminalRecordManifest.source_epoch_id == epoch.id,
        TerminalRecordManifest.canonical_source.is_(True), TerminalRecordManifest.ordinal >= first,
        TerminalRecordManifest.created_at <= end,
    ).order_by(TerminalRecordManifest.ordinal).limit(SOURCE_LIMIT + 1)))
    if len(chunks) > SOURCE_LIMIT or len(records) > SOURCE_LIMIT:
        return details, ["SOURCE_EVIDENCE_LIMIT"], None, ()
    if [row.ordinal for row in records] != list(range(first, end_cursor)):
        errors.append("SOURCE_ORDINAL_INVENTORY_MISMATCH")
    by_ordinal = {row.ordinal: row for row in records}
    cursor, tail_at = first, None
    for chunk in chunks:
        if (chunk.start_ordinal != cursor or chunk.end_ordinal <= cursor or chunk.end_ordinal > end_cursor
                or chunk.record_count != chunk.end_ordinal - chunk.start_ordinal
                or chunk.connector_id != connector.id or chunk.zkt_device_id != epoch.zkt_device_id
                or chunk.generation != baseline["source_generation"] or chunk.previous_chain_digest != chain
                or not start <= ensure_utc(chunk.committed_at) <= end):
            errors.append("SOURCE_CHUNK_CONTINUITY_CHANGED")
            break
        material = []
        for ordinal in range(chunk.start_ordinal, chunk.end_ordinal):
            row = by_ordinal.get(ordinal)
            if row is None:
                errors.append("SOURCE_RAW_DISPOSITION_MISSING")
                continue
            # The signed writer transmits raw custody. Any legacy interpreted
            # claim in this new-writer interval is an explicit evidence gap.
            try:
                if (row.connector_id != connector.id or row.terminal_serial != coverage.terminal_serial
                        or row.generation != chunk.generation or row.source_kind != "TAIL"
                        or row.declared_disposition != "RAW_PRESERVED" or row.disposition != "RAW_PRESERVED"
                        or row.attendance_event_id is not None or not row.protected_raw_record
                        or len(row.protected_raw_record) > 2048):
                    raise ValueError()
                encoded = decrypt_text(row.protected_raw_record)
                if not encoded or len(encoded) > 684:
                    raise ValueError()
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) != row.record_size or hashlib.sha256(raw).hexdigest() != row.raw_record_digest:
                    raise ValueError()
            except (ValueError, TypeError, InvalidToken, RuntimeError):
                errors.append("SOURCE_RAW_CUSTODY_UNVERIFIED")
            material.append({"disposition": row.declared_disposition, "event_uid": None,
                "occurrence_index": row.occurrence_index, "ordinal": row.ordinal,
                "raw_record_digest": row.raw_record_digest, "terminal_record_key": row.terminal_record_key})
            details["manifest_ids"].append(row.id)
            details["raw_dispositions"][row.disposition] = details["raw_dispositions"].get(row.disposition, 0) + 1
        digest = evidence_digest(material)
        calculated = reconciliation_chain_digest(chain, start_ordinal=cursor,
                                                  end_ordinal=chunk.end_ordinal, chunk_digest=digest)
        if digest != chunk.chunk_digest or calculated != chunk.resulting_chain_digest:
            errors.append("SOURCE_DIGEST_CHAIN_CHANGED")
        cursor, chain = chunk.end_ordinal, chunk.resulting_chain_digest
        details["chunks"].append({"id": chunk.id, "start": chunk.start_ordinal, "end": chunk.end_ordinal,
            "digest": chunk.chunk_digest, "chain": chain, "committed_at": ensure_utc(chunk.committed_at).isoformat()})
        if (cursor == end_cursor and chunk.latest_terminal_count == cursor
                and ensure_utc(chunk.committed_at) >= start + timedelta(minutes=8)):
            tail_at = ensure_utc(chunk.committed_at)
    if cursor != end_cursor:
        errors.append("SOURCE_CHAIN_TAIL_MISSING")
    if (coverage.source_committed_cursor == cursor and coverage.source_committed_chain_digest != chain
            or coverage.source_committed_cursor < cursor):
        errors.append("CURRENT_SOURCE_CHAIN_REGRESSED")
    details.update(first_ordinal=first, committed_cursor=cursor, committed_chain=chain,
        coverage_id=coverage.coverage_id, source_epoch=epoch.epoch_id,
        certificate_sha256=baseline["capture_certificate_sha256"],
        final_tail_basis="COMMITTED_RAW_TAIL_AT_TERMINAL_COUNT",
        final_tail_verified_at=tail_at.isoformat() if tail_at else None)
    return details, sorted(set(errors)), tail_at, (job.job_id,)


def _interruption(session, run, samples):
    from zk_add.hil_transport import _control

    parsed = _control(run)
    if parsed is None:
        return None, ["ADD_INTERRUPTION_CONTROL_MISSING"]
    value, start, expires = parsed
    rejected, restored = _time(value.get("rejected_at")), _time(value.get("transport_restored_at"))
    if (rejected is None or restored is None or not start <= rejected < expires <= restored
            or value.get("expiry_reason") != "DEADLINE"
            or value.get("boot_before") != value.get("boot_after_transport")
            or value.get("boot_before") != run.baseline.get("boot_id")
            or not _integer(value.get("rejected_monotonic_ms"))
            or not value["started_monotonic_ms"] <= value["rejected_monotonic_ms"] < value["expires_monotonic_ms"]
            or not _integer(value.get("restored_monotonic_ms"))
            or value["restored_monotonic_ms"] < value["expires_monotonic_ms"]):
        return None, ["ADD_INTERRUPTION_EXPIRY_OR_RECOVERY_UNVERIFIED"]
    baseline = session.get(DeviceTelemetry, value.get("baseline_telemetry_id"))
    if (baseline is None or baseline.connector_id != run.connector_id or baseline.boot_id != value["boot_before"]
            or not start - timedelta(seconds=45) <= ensure_utc(baseline.created_at) <= start
            or _queues((baseline.payload or {}).get("diagnostics") or {}) != {name: 0 for name in RUNTIME.queues}):
        return None, ["ADD_INTERRUPTION_SAFE_BASELINE_UNVERIFIED"]
    initial, detail = _sample(baseline, run, HilTarget.model_validate(run.target),
                              ReleaseIdentity.model_validate(run.release_identity), [])
    if detail["errors"] or any(getattr(initial, key) is not True for key in (
            "storage_healthy", "persistence_verified", "recovery_complete", "workers_healthy",
            "counts_known", "terminal_certified")):
        return None, ["ADD_INTERRUPTION_SAFE_BASELINE_UNVERIFIED"]
    healthy = next((row for row in samples if row.recorded_at >= restored
                    and row.boot_id == value["boot_before"] and all(getattr(row, key) is True for key in (
                        "storage_healthy", "persistence_verified", "recovery_complete", "workers_healthy",
                        "counts_known", "terminal_certified"))), None)
    if healthy is None:
        return None, ["ADD_INTERRUPTION_HEALTH_RECOVERY_MISSING"]
    return RecoveryTest(command_id=value["control_id"], run_id=run.run_id, kind="ADD_INTERRUPT_30S",
        target=HilTarget.model_validate(run.target), issued_at=start, started_at=start, expires_at=expires,
        recovered_at=healthy.recorded_at, outcome="SUCCEEDED", durable_dedup_verified=True,
        safe_checkpoint_verified=True, automatic_expiry_verified=True, interruption_observed=True,
        interruption_seconds=30, boot_before=value["boot_before"], boot_after=healthy.boot_id), []


def _review(logs, alerts, tests, *, preservation_verified):
    """Classify bounded observed effects, never grant a whole-window exemption."""
    expected_logs, optional_logs, warnings, regressions = [], [], [], []
    expected_alerts, adverse_alerts, other_alerts = [], [], []
    for row in logs:
        level = row.level.upper()
        if level in {"ERROR", "CRITICAL", "FATAL"} or row.code == "DEVICE_MESSAGE_REJECTED":
            regressions.append(row.id)
        elif level not in {"DEBUG", "INFO"}:
            expected = preservation_verified and row.subsystem == "reconcile" and row.code in TRANSPORT_RETRY_CODES
            expected = expected and row.device_time is not None and any(
                test.kind == "ADD_INTERRUPT_30S" and row.boot_id == test.boot_before
                and test.started_at <= ensure_utc(row.device_time) <= test.expires_at
                and timedelta(0) <= ensure_utc(row.received_at) - ensure_utc(row.device_time) <= timedelta(seconds=45)
                and ensure_utc(row.received_at) <= test.recovered_at + timedelta(seconds=45) for test in tests)
            if expected:
                expected_logs.append(row.id)
            elif (preservation_verified and level == "WARN" and row.subsystem == "identity"
                    and row.code == "IDENTITY_CATALOG_MEMORY_FALLBACK"):
                # Catalog persistence is optional. Independent attendance and
                # journal recovery proofs, not this warning, establish custody.
                optional_logs.append(row.id)
            else:
                warnings.append(row.id)
    for row in alerts:
        expected = preservation_verified and row.code == "ESP_OFFLINE" and row.state == "RESOLVED"
        expected = expected and row.resolved_at is not None and any(
            test.started_at <= ensure_utc(row.first_seen_at) <= ensure_utc(row.last_seen_at)
            <= ensure_utc(row.resolved_at) <= test.recovered_at + timedelta(seconds=45) for test in tests)
        if expected:
            expected_alerts.append(row.id)
        elif row.severity.upper() in {"HIGH", "CRITICAL"}:
            adverse_alerts.append(row.id)
        elif row.severity.upper() not in {"INFO", "LOW"}:
            other_alerts.append(row.id)
    return {"regression_log_ids": regressions, "adverse_alert_ids": adverse_alerts,
        "warning_log_ids": warnings, "unclassified_alert_ids": other_alerts,
        "expected_interruption_log_ids": expected_logs, "optional_catalog_warning_ids": optional_logs,
        "expected_control_offline_alert_ids": expected_alerts,
        "policy": "EXACT_CODE_AND_CONTROL_INTERVAL_WITH_VERIFIED_PRESERVATION_V1"}


def complete_full_run(session, run_id, *, actor):
    """Seal one completed observation atomically, without accepting caller evidence.

    Connector -> run ordering matches authenticated ingestion and both controls.
    A conditional write also prevents duplicate verdict events on SQLite. Past
    window rows use ADD receipt times; late arrivals cannot extend the window.
    The caller owns commit/rollback; this function never touches Oracle.
    """
    from zk_add.hil_runs import _release_identity
    from zk_add.hil_reboot import collect_reboot_evidence

    if not actor or len(actor) > 120 or not isinstance(run_id, str) or not 1 <= len(run_id) <= 36:
        raise ValueError("A bounded HIL run and administrator identity are required")
    if session.new or session.dirty or session.deleted:
        raise ValueError("HIL completion requires a clean committed session")
    # A clean identity map can still contain values read before revocation or
    # supersession. Evidence must come from the locking transaction's reads.
    session.expire_all()
    connector_id = session.scalar(select(FirmwareHilRun.connector_id).where(FirmwareHilRun.run_id == run_id))
    connector = session.scalar(select(Connector).where(Connector.id == connector_id).with_for_update()
                               .execution_options(populate_existing=True)) if connector_id else None
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id).with_for_update()
                         .execution_options(populate_existing=True))
    if run is None or run.baseline.get("profile") != PROFILE:
        raise ValueError("Full writer HIL observation was not found")
    if run.status != "OBSERVING":
        return run
    now, start, end = utc_now(), ensure_utc(run.started_at), ensure_utc(run.ends_at)
    if now < end or end - start != timedelta(minutes=15):
        raise ValueError("Full HIL completion requires the exact complete 15-minute window")
    target, identity = HilTarget.model_validate(run.target), ReleaseIdentity.model_validate(run.release_identity)
    release = session.get(FirmwareRelease, run.release_id)
    deployment = session.get(FirmwareDeployment, run.deployment_id)
    campaign = session.get(FirmwareCampaign, deployment.campaign_id) if deployment else None
    errors = []
    reader_selection = None
    try:
        from zk_add.zkt_reader_evidence import admitted_reader
        reader_selection = admitted_reader(session, deployment, release)
        if reader_selection != run.baseline.get("reader_admission"):
            errors.append("READER_ADMISSION_CHANGED")
    except (ValueError, AttributeError, TypeError):
        errors.append("READER_ADMISSION_UNVERIFIED")
    if (release is None or release.version != "2.7.0" or release.state != "HIL_ONLY" or release.revoked_at is not None
            or release.manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or _release_identity(release).model_dump(mode="json") != run.release_identity):
        errors.append("RELEASE_CHANGED")
    latest_id = session.scalar(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == run.connector_id)
                               .order_by(FirmwareDeployment.id.desc()).limit(1))
    if (deployment is None or deployment.release_id != run.release_id or deployment.connector_id != run.connector_id
            or deployment.status != "SUCCEEDED" or latest_id != run.deployment_id
            or campaign is None or campaign.release_id != run.release_id or campaign.status not in {"ACTIVE", "COMPLETED"}):
        errors.append("DEPLOYMENT_OR_CAMPAIGN_CHANGED")
    if (connector is None or connector.firmware_family != "zkt" or not target_matches(target, connector)
            or not connector.connected or not connector.zkt_device.online or not connector.zkt_custody_enabled):
        errors.append("CURRENT_TARGET_NOT_READY")
    logs = list(session.scalars(select(DeviceLog).options(load_only(DeviceLog.id, DeviceLog.level,
        DeviceLog.code, DeviceLog.subsystem, DeviceLog.boot_id, DeviceLog.device_time, DeviceLog.received_at))
        .where(DeviceLog.connector_id == run.connector_id,
        DeviceLog.received_at >= start, DeviceLog.received_at <= now).order_by(DeviceLog.id).limit(LOG_LIMIT + 1)))
    alerts = list(session.scalars(select(DeviceAlert).options(load_only(DeviceAlert.id, DeviceAlert.code,
        DeviceAlert.severity, DeviceAlert.state, DeviceAlert.first_seen_at, DeviceAlert.last_seen_at, DeviceAlert.resolved_at))
        .where(DeviceAlert.connector_id == run.connector_id,
        DeviceAlert.last_seen_at >= start, DeviceAlert.first_seen_at <= now)
        .order_by(DeviceAlert.id).limit(LOG_LIMIT + 1)))
    review_complete = len(logs) <= LOG_LIMIT and len(alerts) <= LOG_LIMIT
    if not review_complete:
        errors.append("ALERT_REVIEW_LIMIT")
    logs, alerts = logs[:LOG_LIMIT], alerts[:LOG_LIMIT]
    rows = list(session.scalars(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == run.connector_id,
        DeviceTelemetry.created_at >= start, DeviceTelemetry.created_at <= end)
        .order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(SAMPLE_LIMIT + 1)))
    if len(rows) > SAMPLE_LIMIT:
        errors.append("OBSERVATION_SAMPLE_LIMIT")
    pairs = [_sample(row, run, target, identity, logs) for row in rows[:SAMPLE_LIMIT]]
    reboot = collect_reboot_evidence(session, run, now)
    control = (run.result or {}).get("esp_reboot")
    recovery_id = control.get("recovery_telemetry_id") if isinstance(control, dict) else None
    recovered = next((sample for sample, _ in pairs if sample.telemetry_id == recovery_id), None)
    for index, (sample, detail) in enumerate(pairs):
        classified = classify_startup(rows[index], sample, detail, control, reboot["test"], recovered)
        if classified is not None:
            transition, facts = classified
            sample = sample.model_copy(update={"reboot_startup": transition})
            detail = {**detail, "reboot_startup": transition.model_dump(mode="json"),
                      "startup_diagnostics": facts}
            pairs[index] = sample, detail
    samples, details = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
    for item in details:
        # The raw authority observation remains in the sealed sample. Only a
        # strictly classified unfinished boot can have UNKNOWN authority; the
        # shared evaluator still independently validates that classification.
        errors.extend(error for error in item["errors"] if not
            (item.get("reboot_startup") and error in {
                "DELIVERY_AUTHORITY_UNVERIFIED", "QUALIFIED_READER_PROOF_PENDING"}))
    baseline = session.get(DeviceTelemetry, run.baseline.get("telemetry_id")) if run.baseline.get("telemetry_id") else None
    if (baseline is None or baseline.connector_id != run.connector_id or baseline.boot_id != run.baseline.get("boot_id")
            or not start - timedelta(seconds=45) <= ensure_utc(baseline.created_at) <= start):
        errors.append("BASELINE_TELEMETRY_MISSING")
    else:
        initial, initial_details = _sample(baseline, run, target, identity, [])
        errors.extend(initial_details["errors"])
        if (initial.source_generation != run.baseline.get("source_generation")
                or initial.committed_cursor != run.baseline.get("source_cursor")
                or initial.source_count != run.baseline.get("source_cursor")):
            errors.append("BASELINE_SOURCE_MISMATCH")
        if samples and samples[0].boot_id != baseline.boot_id:
            errors.append("BASELINE_BOOT_CHANGED")
        if samples and any(getattr(initial, key) != getattr(samples[0], key) for key in (
                "write_failures", "read_failures", "worker_restarts")):
            errors.append("BASELINE_COUNTER_CHANGED")
        if details and initial_details["worker_restart_counts"] != details[0]["worker_restart_counts"]:
            errors.append("BASELINE_WORKER_COUNTER_CHANGED")
        if details and details[0]["telemetry_id"] != baseline.id and (
                not _integer(baseline.sequence) or not _integer(details[0]["sequence"])
                or details[0]["sequence"] <= baseline.sequence
                or not _integer(baseline.uptime_seconds) or not _integer(details[0]["uptime_seconds"])
                or details[0]["uptime_seconds"] <= baseline.uptime_seconds):
            errors.append("BASELINE_BOOT_PROGRESS_REGRESSED")
        if samples and any(getattr(samples[0], key) is None or getattr(initial, key) is None
                           or getattr(samples[0], key) < getattr(initial, key)
                           for key in ("committed_cursor", "source_count")):
            errors.append("BASELINE_SOURCE_REGRESSED")
    for before, after in zip(details, details[1:]):
        if before["boot_id"] == after["boot_id"] and (
                not _integer(before["sequence"]) or not _integer(after["sequence"])
                or after["sequence"] <= before["sequence"]
                or not _integer(before["uptime_seconds"]) or not _integer(after["uptime_seconds"])
                or after["uptime_seconds"] <= before["uptime_seconds"]):
            errors.append("BOOT_PROGRESS_OR_SEQUENCE_REGRESSED")
        if before["boot_id"] == after["boot_id"] and before["worker_restart_counts"] != after["worker_restart_counts"]:
            errors.append("WORKER_RESTART_COUNTER_CHANGED")
        # Source/counter continuity, including across unobserved startup
        # values, is checked by the shared evaluator without dropping rows.
    current = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == run.connector_id)
                             .order_by(DeviceTelemetry.id.desc()).limit(1))
    current_details = None
    if (current is None or connector is None or current.boot_id != connector.boot_id
            or not 0 <= (now - ensure_utc(current.created_at)).total_seconds() <= 45):
        errors.append("CURRENT_TELEMETRY_STALE_OR_WRONG_BOOT")
    else:
        current_sample, current_details = _sample(current, run, target, identity, logs)
        errors.extend(current_details["errors"])
        if (not samples or current.boot_id != samples[-1].boot_id
                or any(getattr(current_sample, key) is not True for key in (
                    "storage_healthy", "persistence_verified", "recovery_complete", "workers_healthy",
                    "counts_known", "terminal_certified"))
                or current_sample.committed_cursor != current_sample.source_count
                or any(getattr(current_sample, key) != getattr(samples[-1], key) for key in (
                    "write_failures", "read_failures", "worker_restarts", "source_generation"))
                or current_sample.committed_cursor is None or samples[-1].committed_cursor is None
                or current_sample.committed_cursor < samples[-1].committed_cursor):
            errors.append("CURRENT_HEALTH_CHANGED")
        if details and current_details["worker_restart_counts"] != details[-1]["worker_restart_counts"]:
            errors.append("CURRENT_WORKER_COUNTER_CHANGED")
    source, source_errors, tail_at, certificates = _source(session, run, connector,
        samples[-1].committed_cursor if samples else None)
    errors.extend(source_errors)
    writer = collect_writer_attendance(session, run.run_id, now=now, limit=SOURCE_LIMIT)
    errors.extend(writer["reasons"])
    attendance = []
    for item in writer["occurrences"]:
        if item["reasons"] or item["attendance"] is None or len(item["oracle_receipts"]) != 1:
            continue
        attendance.append(AttendanceProof(event_id=item["attendance"]["event_id"],
            observed_at=_time(item["observed_at"]), logical_record_count=1,
            oracle_receipt_id=item["oracle_receipts"][0]["id"]))
    interruption, interruption_errors = _interruption(session, run, samples)
    errors.extend(interruption_errors + reboot["reasons"])
    tests = tuple(test for test in (interruption, reboot["test"]) if test is not None)
    inventory = run.baseline.get("queue_inventory")
    empty = {name: 0 for name in sorted(RUNTIME.queues)}
    queue_verified = (isinstance(inventory, dict) and inventory.get("schema_version") == 1
        and inventory.get("basis") == "VERIFIED_EMPTY_REQUIRED_QUEUES"
        and inventory.get("telemetry_id") == run.baseline.get("telemetry_id")
        and inventory.get("queues") == empty and baseline is not None
        and _queues((baseline.payload or {}).get("diagnostics") or {}) == empty
        and bool(details) and details[-1]["queues"] == empty
        and current_details is not None and current_details["queues"] == empty and not source_errors)
    if not queue_verified:
        errors.append("QUEUE_BASELINE_OR_FINAL_INVENTORY_UNVERIFIED")
    review = _review(logs, alerts, tests, preservation_verified=bool(queue_verified
        and not writer["reasons"] and not source_errors and current_details
        and not current_details["errors"] and not any(name in errors for name in (
            "CURRENT_HEALTH_CHANGED", "CURRENT_TELEMETRY_STALE_OR_WRONG_BOOT"))))
    if review["warning_log_ids"]:
        errors.append("WARNING_LOG_REVIEW_REQUIRED")
    if review["unclassified_alert_ids"]:
        errors.append("ALERT_CLASSIFICATION_REVIEW_REQUIRED")
    evidence = SmokeEvidence(run_id=run.run_id, deployment_id=deployment.deployment_id if deployment else "MISSING",
        target=target, release=identity, release_state=release.state if release else "MISSING",
        deployment_state=deployment.status if deployment else "MISSING", started_at=start, ended_at=end,
        ready_at=start, samples=tuple(samples), recovery_tests=tests, attendance=tuple(attendance),
        certificate_ids=certificates, source_continuity_verified=True if not source_errors else None,
        tail_verified_at=tail_at, queue_preservation_verified=True if queue_verified else None,
        alert_review_complete=True if review_complete else None,
        unresolved_regression=bool(review["regression_log_ids"] or review["adverse_alert_ids"]))
    verdict = evaluate_smoke(evidence, now=now)
    reasons = sorted(set(errors + list(verdict.reasons)))
    outcome = "FAILED" if verdict.outcome == "FAILED" else "INCOMPLETE" if reasons else "PASS"
    sealed = {"collector_version": COLLECTOR_VERSION, "window_start": start.isoformat(), "window_end": end.isoformat(),
        "reader_admission": reader_selection, "qualified_reader": run.baseline.get("qualified_reader"),
        "scope": {"run_id": run.run_id, "deployment_id": run.deployment_id, "target": run.target,
                  "release_identity": run.release_identity}, "baseline_sha256": evidence_digest(run.baseline),
        "samples": details, "current_sample": current_details, "source": source, "attendance": writer,
        "recovery_tests": [test.model_dump(mode="json") for test in tests],
        "queue_inventory": {"baseline": inventory, "final": details[-1]["queues"] if details else None,
                            "verified": queue_verified, "basis": "EXPLICIT_EMPTY_BASELINE_AND_ACCOUNTED_SOURCE_RANGE"},
        "alert_review": {"complete": review_complete, "log_ids": [row.id for row in logs],
            "alert_ids": [row.id for row in alerts], **review},
        "outcome": outcome, "reasons": reasons, "evaluated_at": now.isoformat(),
        "acceptance_scope": "EXPERIMENTAL_REMOTE_CONTROL_AND_SOURCE_CUSTODY",
        "local_journal_preservation": "NOT_ASSERTED", "seven_day_capacity": "NOT_ASSERTED",
        "unasserted_requirements": {"local_journal_preservation": "NO_BOOT_BOUND_LOCAL_RESIDENCE_PROOF",
                                   "seven_day_capacity": "NOT_MEASURED_BY_15_MINUTE_OBSERVATION"},
        "limits": {"telemetry_samples": SAMPLE_LIMIT, "logs": LOG_LIMIT, "alerts": LOG_LIMIT,
                   "source_occurrences": SOURCE_LIMIT, "source_chunks": SOURCE_LIMIT,
                   "oracle_scope": "TYPED_RECEIPT_SCOPE_ONLY"},
        "physical_power_cut": "NOT_PERFORMED", "hardware_endurance": "NOT_PERFORMED",
        "model_profile_qualification": "NOT_ASSERTED", "production_qualification": "NOT_ASSERTED"}
    for attempt in range(2):
        digest = evidence_digest(sealed)
        result = {**run.result, "profile": PROFILE, "outcome": outcome, "reasons": reasons,
                  "actor": actor, "collector_version": COLLECTOR_VERSION,
                  "evidence_sha256": digest, "evidence": sealed}
        status = {"PASS": "HIL_ACCEPTED", "FAILED": "HIL_FAILED", "INCOMPLETE": "HIL_INCOMPLETE"}[outcome]
        statement = update(FirmwareHilRun).where(FirmwareHilRun.id == run.id, FirmwareHilRun.status == "OBSERVING")
        if outcome == "PASS":
            # Do not take campaign/release locks in an order opposite to OTA
            # cancellation/revocation. Their current eligibility participates
            # in the same statement that commits the positive verdict.
            statement = statement.where(
                exists(select(FirmwareRelease.id).where(FirmwareRelease.id == run.release_id,
                    FirmwareRelease.state == "HIL_ONLY", FirmwareRelease.revoked_at.is_(None),
                    FirmwareRelease.version == identity.version, FirmwareRelease.git_sha == identity.git_sha,
                    FirmwareRelease.image_sha256 == identity.artifact_sha256,
                    FirmwareRelease.signing_key_id == identity.signing_key_id,
                    FirmwareRelease.manifest["application_sha256"].as_string() == identity.application_sha256,
                    FirmwareRelease.manifest["runtime_profile"].as_string() == "ZKT_JOURNAL_V1")),
                exists(select(FirmwareDeployment.id).where(FirmwareDeployment.id == run.deployment_id,
                    FirmwareDeployment.connector_id == run.connector_id, FirmwareDeployment.release_id == run.release_id,
                    FirmwareDeployment.campaign_id == deployment.campaign_id, FirmwareDeployment.status == "SUCCEEDED")),
                exists(select(FirmwareCampaign.id).where(FirmwareCampaign.id == deployment.campaign_id,
                    FirmwareCampaign.release_id == run.release_id, FirmwareCampaign.status.in_(["ACTIVE", "COMPLETED"]))),
                ~exists(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == run.connector_id,
                    FirmwareDeployment.id > run.deployment_id)))
        changed = session.execute(statement.values(status=status, completed_at=now, result=result)
                                   .execution_options(synchronize_session=False))
        session.expire(run)
        if changed.rowcount == 1:
            session.add(FirmwareEvent(deployment_id=run.deployment_id, state=status,
                details={**run.release_identity, "target": run.target, "run_id": run.run_id,
                    "profile": PROFILE, "outcome": outcome, "reasons": reasons,
                    "collector_version": COLLECTOR_VERSION, "evidence_sha256": digest}))
            session.flush()
            break
        if run.status != "OBSERVING" or attempt:
            break
        outcome, reasons = "INCOMPLETE", sorted(set(reasons + ["SCOPE_CHANGED_BEFORE_FINALIZATION"]))
        sealed = {**sealed, "outcome": outcome, "reasons": reasons}
    return run

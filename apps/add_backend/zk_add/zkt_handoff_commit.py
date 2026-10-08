"""Commit experimental source authority from authenticated retained evidence.

This receipt certifies the boundary and local absence of retained legacy work.
Per-item custody receipts retain their existing identities. It does not certify
historical completeness, employee identity, Oracle delivery or physical tests.
"""

from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import select

from zk_add.attendance_repair import _protected_digest
from zk_add.audit import append_audit
from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.hil_scope import target_matches
from zk_add.models import (
    Connector,
    ZKTDevice,
    DeviceTelemetry,
    ReconciliationCoverage,
    TerminalSourceEpoch,
    ZktCustodyWork,
    ZktLegacyHandoff,
    ZktSourceCutover,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease, _application_sha256, _verify_manifest
from zk_add.runtime_contract import journal_storage_status, runtime_contract, worker_snapshot_fresh
from zk_add.schemas import LegacyInventoryDiagnostics
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID
from zk_add.zkt_handoff import boundary_status
from zk_add.zkt_source_attendance import cutover_material

SCOPE = "LOCAL_LEGACY_ABSENCE_AND_SOURCE_BOUNDARY_V1"


def _release(session, connector, release_id):
    release = session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.release_id == release_id).execution_options(populate_existing=True)
    )
    target = BY_ID.get(connector.connector_id)
    if (
        release is None
        or release.version != "2.7.0"
        or release.state != "HIL_ONLY"
        or release.revoked_at is not None
        or connector.firmware_family != "zkt"
        or target is None
        or not target_matches(target.identity, connector)
        or connector.zkt_device.model != target.model
    ):
        raise ValueError("HANDOFF_EXACT_EXPERIMENTAL_RELEASE_REQUIRED")
    # ADD adds publication metadata after verifying a manifest. Those private
    # keys are not part of the signed document and cannot grant capabilities.
    manifest = {key: value for key, value in release.manifest.items() if not key.startswith("_")}
    try:
        from zk_add.storage_contract import validate_storage_contract

        if validate_storage_contract(manifest, release.version)["schema_version"] != 5:
            raise ValueError("Historical writer contract is audit-only")
        _verify_manifest(manifest, release.manifest_signature)
    except (ValueError, RuntimeError, InvalidSignature, TypeError) as exc:
        raise ValueError("HANDOFF_RELEASE_SIGNATURE_OR_CONTRACT") from exc
    if (
        manifest.get("image_sha256") != release.image_sha256
        or manifest.get("git_sha") != release.git_sha
        or target.identity.model_dump() not in (manifest.get("hil_targets") or [])
        or target.identity.model_dump() not in (release.manifest.get("_hil_targets") or [])
    ):
        raise ValueError("HANDOFF_RELEASE_SCOPE_CHANGED")
    return release


def enable_custody(session, connector_id, *, release_id, actor, idempotency_key):
    """Enable bounded raw custody before installation, never attendance authority."""
    connector = _lock(session, connector_id, actor, idempotency_key)
    release = _release(session, connector, release_id)
    from zk_add.zkt_writer_contract import writer_predecessor_hold

    if not connector.zkt_custody_enabled:
        error = writer_predecessor_hold(session, connector, release)
        if error:
            raise ValueError(error)
        if not connector.connected or not connector.zkt_device.online:
            raise ValueError("HANDOFF_CAPTURE_TARGET_OFFLINE")
        connector.zkt_custody_enabled = True
        append_audit(
            session,
            actor=actor,
            action="ZKT_EXPERIMENTAL_CUSTODY_ENABLED",
            target_type="connector",
            target_id=connector.connector_id,
            outcome="CUSTODY_ONLY",
            after={
                "release_id": release.release_id,
                "image_sha256": release.image_sha256,
                "idempotency_key": idempotency_key,
                "delivery_permission": "NOT_GRANTED",
            },
        )
    return {
        "connector_id": connector.connector_id,
        "enabled": True,
        "delivery_permission": "NOT_GRANTED",
    }


def _lock(session, connector_id, actor, idempotency_key):
    if not actor or len(actor) > 120 or not idempotency_key or len(idempotency_key) > 120:
        raise ValueError("HANDOFF_ACTOR_AND_KEY_REQUIRED")
    connector = session.scalar(
        select(Connector).where(Connector.connector_id == connector_id).with_for_update().execution_options(populate_existing=True)
    )
    if connector is None:
        raise ValueError("HANDOFF_CONNECTOR_NOT_FOUND")
    terminal = session.scalar(select(ZKTDevice).where(ZKTDevice.connector_id == connector.id)
        .execution_options(populate_existing=True))
    if terminal is None or connector.zkt_device is not terminal:
        raise ValueError("HANDOFF_EXACT_TERMINAL_REQUIRED")
    return connector


def _local_proof(diagnostics, uptime_seconds):
    try:
        inventory = LegacyInventoryDiagnostics.model_validate(diagnostics.get("legacy_inventory"))
        runtime = runtime_contract(diagnostics, "zkt")
    except (ValueError, ValidationError) as exc:
        raise ValueError("HANDOFF_LEGACY_INVENTORY_REQUIRED") from exc
    if (
        diagnostics.get("runtime_profile") != "ZKT_JOURNAL_V1"
        or not inventory.fresh
        or not inventory.verified_empty
        or not inventory.idle
        or inventory.generation in {"0", str(2**64 - 1)}
        or journal_storage_status(diagnostics, uptime_seconds) != "HEALTHY"
    ):
        raise ValueError("HANDOFF_LOCAL_STORAGE_NOT_QUIESCENT")
    journal = diagnostics.get("journal_runtime") or {}
    if (
        journal.get("observed") is not True
        or journal.get("phase") != "READY"
        or journal.get("reader_ready") is not True
        or journal.get("writer_ready") is not True
        or journal.get("delivery_authority") != "ADD"
        or journal.get("compatibility") != ""
    ):
        raise ValueError("HANDOFF_WRITER_NOT_READY")
    storage = diagnostics.get("storage") or {}
    if (
        storage.get("persistence_verified") is not True
        or storage.get("recovery_complete") is not True
        or storage.get("durability") != "HEALTHY"
        or storage.get("upgrade_ready") is not True
        or any(
            storage.get(key)
            for key in (
                "error_code",
                "persistence_probe_error",
                "legacy_error_code",
                "legacy_read_faults",
                "legacy_append_faults",
                "legacy_retire_faults",
            )
        )
    ):
        raise ValueError("HANDOFF_LOCAL_STORAGE_NOT_QUIESCENT")
    workers = diagnostics.get("workers") or []
    by_name = {row.get("name"): row for row in workers}
    if (
        len(by_name) != len(workers)
        or not runtime.workers <= by_name.keys()
        or not {"legacy_add_delivery", "legacy_ords_delivery"} <= by_name.keys()
    ):
        raise ValueError("HANDOFF_WORKER_EVIDENCE_REQUIRED")
    for name, worker in by_name.items():
        if (
            worker.get("state") not in {"RUNNING", "WAITING_NETWORK"}
            or not worker_snapshot_fresh(worker, uptime_seconds, runtime)
            or (
                name in {"legacy_add_delivery", "legacy_ords_delivery", "storage_owner"}
                and worker.get("operation") != "idle"
            )
        ):
            raise ValueError("HANDOFF_WORKERS_NOT_QUIESCENT")
    queues = diagnostics.get("queues") or []
    legacy = [row for row in queues if row.get("name") == "legacy_migration"]
    if (
        len(legacy) != 1
        or legacy[0].get("count_known") is not True
        or type(legacy[0].get("records")) is not int
        or legacy[0]["records"] != 0
        or legacy[0].get("count_reason") != "VERIFIED_EMPTY"
    ):
        raise ValueError("HANDOFF_LEGACY_CUSTODY_PENDING")
    return inventory.model_dump()


def commit_handoff(session, connector_id, *, release_id, actor, idempotency_key, now=None):
    """All writes, the authority and its audit commit in the caller transaction.

    A lost response replays the same receipt. Existing permits cannot be moved
    to a later ordinal or replaced by a different request. Two independently
    received healthy samples bind the same owner generation and immutable NVS
    boundary; accepted legacy producers invalidate that owner generation.
    """
    now = ensure_utc(now or utc_now())
    connector = _lock(session, connector_id, actor, idempotency_key)
    prior = session.scalar(
        select(ZktLegacyHandoff).where(ZktLegacyHandoff.connector_id == connector.id)
    )
    if prior is not None:
        if (prior.actor, prior.idempotency_key) != (actor, idempotency_key):
            raise ValueError("HANDOFF_ALREADY_COMMITTED")
        evidence = _retained_evidence(prior)
        if evidence.get("release_id") != release_id:
            raise ValueError("HANDOFF_RETAINED_EVIDENCE_CHANGED")
        retained = handoff_status(session, connector)
        if retained["state"] != "COMMITTED":
            raise ValueError("HANDOFF_RETAINED_EVIDENCE_CHANGED")
        return retained
    if session.scalar(
        select(ZktSourceCutover.id).where(ZktSourceCutover.connector_id == connector.id)
    ):
        raise ValueError("HANDOFF_EXISTING_AUTHORITY_REQUIRES_REVIEW")
    release = _release(session, connector, release_id)
    if not connector.zkt_custody_enabled:
        raise ValueError("HANDOFF_CUSTODY_NOT_ENABLED")
    boundary = boundary_status(session, connector, now=now)
    if boundary["state"] != "VERIFIED_SOURCE_ANCHOR":
        raise ValueError(boundary["reason"])
    if _application_sha256(release) != connector.ota_image_sha256:
        raise ValueError("HANDOFF_WRITER_IMAGE_MISMATCH")
    deployment = session.scalar(
        select(FirmwareDeployment)
        .where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if (
        deployment is None
        or deployment.release_id != release.id
        or deployment.status not in {"RECONCILING", "SUCCEEDED"}
    ):
        raise ValueError("HANDOFF_VERIFIED_INSTALL_REQUIRED")
    campaign = session.get(FirmwareCampaign, deployment.campaign_id, populate_existing=True)
    if (campaign is None or campaign.release_id != release.id or campaign.zone_id != connector.zone_id
            or campaign.status not in {"ACTIVE", "COMPLETED"}):
        raise ValueError("HANDOFF_VERIFIED_INSTALL_REQUIRED")
    from zk_add.zkt_reader_evidence import admitted_reader, qualified_reader_proof
    selection = admitted_reader(session, deployment, release)
    reader_proofs = []
    samples = session.scalars(
        select(DeviceTelemetry)
        .where(DeviceTelemetry.connector_id == connector.id)
        .order_by(DeviceTelemetry.id.desc())
        .limit(2)
        .execution_options(populate_existing=True)
    ).all()
    if len(samples) != 2:
        raise ValueError("HANDOFF_STABLE_SAMPLES_REQUIRED")
    current, previous = samples
    if (
        any(row.boot_id != connector.boot_id or row.sequence is None for row in samples)
        or current.sequence != connector.firmware_diagnostics.get("sample_sequence")
        or not 0 < previous.sequence < current.sequence
        or not 0 <= (now - ensure_utc(current.created_at)).total_seconds() <= 45
        or not 10
        <= (ensure_utc(current.created_at) - ensure_utc(previous.created_at)).total_seconds()
        <= 90
    ):
        raise ValueError("HANDOFF_STABLE_SAMPLES_REQUIRED")
    proofs = []
    sampled_times = []
    for row in samples:
        diagnostics = (row.payload or {}).get("diagnostics") or {}
        ota = (row.payload or {}).get("ota") or {}
        try:
            sampled = datetime.fromisoformat(
                row.payload["_trusted_envelope_sent_at"].replace("Z", "+00:00")
            )
            if (
                sampled.tzinfo is None
                or not 0 <= (ensure_utc(row.created_at) - ensure_utc(sampled)).total_seconds() <= 45
            ):
                raise ValueError()
            sampled_times.append(ensure_utc(sampled))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("HANDOFF_SAMPLE_TIME_INVALID") from exc
        if (
            ota.get("image_sha256") != connector.ota_image_sha256
            or ota.get("secure_boot") is not True
            or ota.get("running_version") != release.version
            or ota.get("rollback_enabled") is not True
            or diagnostics.get("source_boundary")
            != connector.firmware_diagnostics.get("source_boundary")
        ):
            raise ValueError("HANDOFF_SAMPLE_BINDING_CHANGED")
        reader_proofs.append(qualified_reader_proof(diagnostics, selection, row.uptime_seconds))
        proofs.append(_local_proof(diagnostics, row.uptime_seconds))
    if not 10 <= (sampled_times[0] - sampled_times[1]).total_seconds() <= 90:
        raise ValueError("HANDOFF_STABLE_SAMPLES_REQUIRED")
    if reader_proofs[0] != reader_proofs[1]:
        raise ValueError("HANDOFF_RETAINED_READER_CHANGED")
    if proofs[0] != proofs[1]:
        raise ValueError("HANDOFF_LEGACY_GENERATION_CHANGED")
    epoch = session.scalar(
        select(TerminalSourceEpoch).where(
            TerminalSourceEpoch.zkt_device_id == connector.zkt_device.id,
            TerminalSourceEpoch.epoch_id == boundary["source_epoch"],
        )
    )
    coverage = session.scalar(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.zkt_device_id == connector.zkt_device.id,
            ReconciliationCoverage.active.is_(True),
        )
    )
    if boundary["record_size"] not in {16, 40}:
        # A zero boundary has no inferred layout; eight-byte attendance has no
        # independently supported textual user reference in this contract.
        raise ValueError("HANDOFF_SOURCE_LAYOUT_NOT_DELIVERABLE")
    evidence = {
        "schema_version": 1,
        "scope": SCOPE,
        "reader_admission": selection,
        "qualified_reader": reader_proofs[0],
        "connector_id": connector.connector_id,
        "release_id": release.release_id,
        "artifact_sha256": release.image_sha256,
        "writer_digest": connector.ota_image_sha256,
        "boot_id": connector.boot_id,
        "source_boundary": deepcopy(connector.firmware_diagnostics["source_boundary"]),
        "source_epoch": epoch.epoch_id,
        "terminal_generation": epoch.terminal_generation,
        "source_cursor": coverage.source_committed_cursor,
        "source_chain": coverage.source_committed_chain_digest,
        "telemetry": [
            {
                "id": row.id,
                "sequence": row.sequence,
                "received_at": ensure_utc(row.created_at).isoformat(),
                "uptime_seconds": row.uptime_seconds,
                "payload_digest": _protected_digest(row.payload),
            }
            for row in reversed(samples)
        ],
        "legacy_inventory": proofs[0],
        "historical_completeness": "NOT_ASSERTED",
        "oracle_delivery": "NOT_ASSERTED",
        "profile_qualification": "NOT_ASSERTED",
        "physical_qualification": "NOT_PERFORMED",
    }
    digest = _protected_digest(evidence)
    cutover = ZktSourceCutover(
        connector_id=connector.id,
        source_epoch_id=epoch.id,
        first_new_ordinal=boundary["next_ordinal"],
        terminal_serial=connector.zkt_device.serial,
        model=connector.zkt_device.model,
        record_size=boundary["record_size"],
    )
    authority = cutover_material(
        cutover,
        connector,
        epoch,
        migration_digest=digest,
        writer_digest=connector.ota_image_sha256,
        boot_id=connector.boot_id,
    )
    cutover.authority_digest, cutover.protected_authority = (
        _protected_digest(authority),
        encrypt_json(authority),
    )
    session.add(cutover)
    session.flush()
    receipt = ZktLegacyHandoff(
        connector_id=connector.id,
        cutover_id=cutover.id,
        receipt_id=str(uuid4()),
        evidence_digest=digest,
        protected_evidence=encrypt_json(evidence),
        actor=actor,
        idempotency_key=idempotency_key,
        wake_cursor=0,
        wake_through=session.scalar(
            select(ZktCustodyWork.id)
            .where(
                ZktCustodyWork.connector_id == connector.id, ZktCustodyWork.kind == "SOURCE_LEDGER"
            )
            .order_by(ZktCustodyWork.id.desc())
            .limit(1)
        )
        or 0,
    )
    session.add(receipt)
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="ZKT_EXPERIMENTAL_HANDOFF_COMMITTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="SOURCE_AUTHORITY_GRANTED",
        after={
            "receipt_id": receipt.receipt_id,
            "evidence_digest": digest,
            "first_new_ordinal": cutover.first_new_ordinal,
            "scope": SCOPE,
            "oracle_delivery": "NOT_ASSERTED",
        },
    )
    return serialize_handoff(receipt)


def serialize_handoff(receipt):
    return {
        "receipt_id": receipt.receipt_id,
        "evidence_digest": receipt.evidence_digest,
        "scope": SCOPE,
        "state": "COMMITTED",
        "created_at": ensure_utc(receipt.created_at),
        "oracle_delivery": "NOT_ASSERTED",
        "historical_completeness": "NOT_ASSERTED",
    }


def _retained_evidence(receipt):
    try:
        evidence = decrypt_json(receipt.protected_evidence)
        if (
            not isinstance(evidence, dict)
            or evidence.get("scope") != SCOPE
            or _protected_digest(evidence) != receipt.evidence_digest
        ):
            raise ValueError()
        return evidence
    except (InvalidToken, TypeError, ValueError) as exc:
        raise ValueError("HANDOFF_RETAINED_EVIDENCE_CHANGED") from exc


def handoff_status(session, connector):
    """Describe the committed, historical receipt without claiming live health."""
    receipt = session.scalar(
        select(ZktLegacyHandoff).where(ZktLegacyHandoff.connector_id == connector.id)
    )
    if receipt is None:
        return {"state": "NOT_COMMITTED"}
    try:
        evidence = _retained_evidence(receipt)
        cutover = session.get(ZktSourceCutover, receipt.cutover_id)
        if (
            evidence.get("connector_id") != connector.connector_id
            or cutover is None
            or cutover.connector_id != connector.id
        ):
            raise ValueError("HANDOFF_RETAINED_EVIDENCE_CHANGED")
        authority = decrypt_json(cutover.protected_authority)
        if (
            not isinstance(authority, dict)
            or authority.get("migration_digest") != receipt.evidence_digest
            or _protected_digest(authority) != cutover.authority_digest
        ):
            raise ValueError("HANDOFF_RETAINED_EVIDENCE_CHANGED")
    except (InvalidToken, ValueError, TypeError):
        return {"state": "HELD", "reason": "HANDOFF_RETAINED_EVIDENCE_CHANGED"}
    return serialize_handoff(receipt)


def wake_source_page(session, connector_id, *, limit=100):
    """Resume existing holds once, with a durable bounded migration cursor.

    New intake already sees the permit. Existing packets and old attendance
    keep their separate work; this only revisits retained source interpretations.
    The caller holds the same connector lock as intake and delivery inspection.
    """
    receipt = session.scalar(
        select(ZktLegacyHandoff).where(ZktLegacyHandoff.connector_id == connector_id)
    )
    if receipt is None or receipt.wake_cursor >= receipt.wake_through:
        return
    rows = session.scalars(
        select(ZktCustodyWork)
        .where(
            ZktCustodyWork.connector_id == connector_id,
            ZktCustodyWork.kind == "SOURCE_LEDGER",
            ZktCustodyWork.id > receipt.wake_cursor,
            ZktCustodyWork.id <= receipt.wake_through,
        )
        .order_by(ZktCustodyWork.id)
        .limit(max(1, min(limit, 500)))
    ).all()
    now = utc_now()
    for row in rows:
        if row.next_attempt_at is None and row.state == "WAIT_PROFILE":
            row.next_attempt_at = now
    receipt.wake_cursor = rows[-1].id if rows else receipt.wake_through
    session.flush()

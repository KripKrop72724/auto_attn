"""Server-owned, exact first-OTA trials; device proof is not hardware attestation."""
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import secrets
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select, update

from zk_add.hil_scope import target_matches
from zk_add.models import (Connector, DeviceAlert, DeviceCommand, DeviceTelemetry,
    ReconciliationJob, TemporaryAdminLease, ZKTDevice, ZktSourceCutover, ZktObservationReceipt)
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID, TARGETS
from zk_add.zkt_factory_contract import (FACTORY_ADDRESS, FACTORY_BRIDGE_VERSION,
    FACTORY_SIZE, FACTORY_TARGETS, FACTORY_TRIAL_SECONDS, FACTORY_LAYOUT_SHA256, factory_trial_exposure)

RESERVED = "FACTORY_TRIAL_RESERVED"
PROOF_EVENTS = {"FACTORY_VERIFIED", "FACTORY_FALLBACK_REVOKED"}
SHA = r"^[0-9a-f]{64}$"


class FactoryTrialProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    trial_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    challenge: str = Field(min_length=64, max_length=64, pattern=SHA)
    deployment_id: str = Field(min_length=1, max_length=100)
    boot_id: str = Field(min_length=1, max_length=100)
    proof_state: Literal["FACTORY_VERIFIED", "FACTORY_FALLBACK_REVOKED"]
    reader_application_sha256: str = Field(min_length=64, max_length=64, pattern=SHA)
    onboarding_generation: int = Field(ge=1, le=0x7FFFFFFF)
    factory_application_sha256: str = Field(min_length=64, max_length=64, pattern=SHA)
    factory_signed_image_sha256: str = Field(min_length=64, max_length=64, pattern=SHA)
    factory_signed_image_bytes: int = Field(ge=8192, le=FACTORY_SIZE, multiple_of=4096)
    layout_sha256: str = Field(min_length=64, max_length=64, pattern=SHA)
    factory_address: Literal[FACTORY_ADDRESS]
    factory_size: Literal[FACTORY_SIZE]
    checkpoint_sha256: str = Field(min_length=64, max_length=64, pattern=SHA)
    checkpoint_verified: Literal[True]
    secure_boot_verified: Literal[True]
    signature_verified: Literal[True]
    encrypted_nvs: Literal[True]
    rollback_enabled: Literal[True]
    anti_rollback_disabled: Literal[True]
    factory_fallback_verified: Literal[True]
    legacy_only: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def wire_types(cls, value):
        if not isinstance(value, dict):
            raise ValueError("FACTORY_PROOF_TYPES")
        integer_fields = ("schema_version", "onboarding_generation", "factory_signed_image_bytes",
                          "factory_address", "factory_size")
        boolean_fields = ("checkpoint_verified", "secure_boot_verified", "signature_verified", "encrypted_nvs",
                          "rollback_enabled", "anti_rollback_disabled", "factory_fallback_verified", "legacy_only")
        if any(type(value.get(key)) is not int for key in integer_fields) or any(
            value.get(key) is not True for key in boolean_fields
        ):
            raise ValueError("FACTORY_PROOF_TYPES")
        return value


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _seal(value):
    from zk_add.ota import _scope_signing_key
    return hmac.new(_scope_signing_key(), _digest(value).encode(), hashlib.sha256).hexdigest()


def _sealed(value):
    return {**value, "authorization": _seal(value)}


def _verified(value):
    _require(isinstance(value, dict), "FACTORY_TRIAL_EVIDENCE_SHAPE")
    body = dict(value)
    authorization = body.pop("authorization", None)
    _require(isinstance(authorization, str) and hmac.compare_digest(authorization, _seal(body)),
             "FACTORY_TRIAL_EVIDENCE_SEAL")
    return body


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "FACTORY_TRIAL_TIME_UNKNOWN")
    return ensure_utc(parsed)


def _release_identity(release):
    from zk_add.hil_runs import _release_identity as identity
    return {"release_id": release.release_id, **identity(release).model_dump(mode="json")}


def _fresh_terminal(session, connector):
    terminal = session.scalar(select(ZKTDevice).where(ZKTDevice.connector_id == connector.id)
        .execution_options(populate_existing=True))
    _require(terminal is not None and terminal is connector.zkt_device, "FACTORY_EXACT_TARGET_REQUIRED")


def _target(session, connector):
    _fresh_terminal(session, connector)
    pin = next((item for item in FACTORY_TARGETS if item["connector_id"] == connector.connector_id), None)
    _require(pin is not None and connector.firmware_family == "zkt"
        and target_matches(BY_ID[connector.connector_id].identity, connector)
        and connector.onboarding_generation == pin["onboarding_generation"], "FACTORY_EXACT_TARGET_REQUIRED")
    return pin


def dependencies(session, release):
    """No22-on3FL certificate: require the actual final writer on qualified23."""
    from zk_add.ota import FirmwareRelease, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareCampaign
    from zk_add.hil_runs import accepted_full_event_matches
    from zk_add.bridge_observation import ready_event_matches
    try:
        from zk_add.zkt_reader_evidence import reader_entry_for_manifest, stored_reader_evidence_matches
    except ImportError as exc:
        raise ValueError("FACTORY_FINAL_READER_MATRIX_UNAVAILABLE") from exc
    writer = session.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.7.0")
        .execution_options(populate_existing=True))
    _require(writer is not None and writer.state == "HIL_ONLY" and writer.revoked_at is None,
             "FACTORY_FINAL_WRITER_REQUIRED")
    entry23 = reader_entry_for_manifest(writer.manifest, "2.6.23")
    entry22 = reader_entry_for_manifest(writer.manifest, FACTORY_BRIDGE_VERSION)
    def matches(entry, row):
        item = entry["reader"]
        identity = _release_identity(row)
        return all(item[key] == identity[other] for key, other in (
            ("release_id", "release_id"), ("version", "version"), ("source_sha", "git_sha"),
            ("application_sha256", "application_sha256"), ("artifact_sha256", "artifact_sha256"),
            ("signing_key_id", "signing_key_id")))
    _require(matches(entry22, release), "FACTORY_WRITER_MATRIX_READER_MISMATCH")
    reader = session.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.6.23")
        .execution_options(populate_existing=True))
    _require(reader is not None and reader.state == "HIL_ONLY" and reader.revoked_at is None
             and matches(entry23, reader), "FACTORY_QUALIFIED23_ARTIFACT_REQUIRED")
    canary = session.scalar(select(Connector).where(Connector.connector_id == TARGETS[0].identity.connector_id)
        .execution_options(populate_existing=True))
    _require(canary is not None, "FACTORY_3FL_IDENTITY_REQUIRED")
    _fresh_terminal(session, canary)
    _require(target_matches(TARGETS[0].identity, canary), "FACTORY_3FL_IDENTITY_REQUIRED")
    proofs = []
    for item, check in ((reader, ready_event_matches), (writer, accepted_full_event_matches)):
        row = session.execute(select(FirmwareEvent, FirmwareDeployment)
            .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
            .where(FirmwareDeployment.connector_id == canary.id, FirmwareDeployment.release_id == item.id,
                   FirmwareEvent.state.in_(["BRIDGE_READY", "BRIDGE_FAILED", "BRIDGE_INCOMPLETE",
                                            "HIL_ACCEPTED", "HIL_FAILED", "HIL_INCOMPLETE"]))
            .order_by(FirmwareEvent.id.desc()).limit(1).execution_options(populate_existing=True)).first()
        _require(row is not None, "FACTORY_3FL_VERDICT_REQUIRED")
        event, deployment = row
        campaign = session.get(FirmwareCampaign, deployment.campaign_id, populate_existing=True)
        run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == (event.details or {}).get("run_id"))
            .execution_options(populate_existing=True))
        _require(campaign is not None and campaign.status in {"ACTIVE", "COMPLETED"}
            and campaign.release_id == item.id and deployment.status == "SUCCEEDED"
            and check(session, event, deployment, item)
            and run is not None and run.target == TARGETS[0].identity.model_dump()
            and run.release_identity == {key: value for key, value in _release_identity(item).items() if key != "release_id"}
            and ensure_utc(run.completed_at) <= utc_now(), "FACTORY_3FL_VERDICT_UNVERIFIED")
        if item is writer:
            _require(stored_reader_evidence_matches(run, writer, "2.6.23"), "FACTORY_3FL_RETAINED23_UNVERIFIED")
        proofs.append({"release": _release_identity(item), "event_id": event.id,
                       "deployment_id": deployment.deployment_id, "run_id": run.run_id})
    from zk_add.zkt_writer_contract import qualified_bridge_hold
    _require(qualified_bridge_hold(session, canary, entry23["reader"]) is None,
             "FACTORY_QUALIFIED23_VERDICT_UNVERIFIED")
    from zk_add.zkt_hil_schedule import _post_verdict_hold
    _require(not _post_verdict_hold(session, canary, run_completed_at=ensure_utc(run.completed_at).isoformat()),
             "FACTORY_3FL_POST_VERDICT_HOLD")
    return {"matrix_sha256": entry23["matrix_sha256"], "proofs": proofs}


def _no_authority_history(session, connector):
    from zk_add.ota import FirmwareDeployment
    _require(not connector.zkt_custody_enabled, "FACTORY_ADD_CUSTODY_ALREADY_ENABLED")
    for model in (ZktSourceCutover, ZktObservationReceipt):
        _require(not session.scalar(select(model.id).where(model.connector_id == connector.id).limit(1)),
                 "FACTORY_ADD_HISTORY_PRESENT")
    _require(not session.scalar(select(FirmwareDeployment.id).where(
        FirmwareDeployment.connector_id == connector.id, FirmwareDeployment.target_version == "2.7.0").limit(1)),
        "FACTORY_WRITER_HISTORY_PRESENT")


def predecessor_snapshot(session, release, connector, *, own_deployment_id=None):
    from zk_add.ota import capability_is_eligible, _versions_match, FirmwareDeployment, ACTIVE_DEPLOYMENT_STATES
    from zk_add.reconciliation import active_coverage, ACTIVE_COMMAND_STATES, ACTIVE_LEASE_STATES
    from zk_add.zkt_bridge_contract import validate_bridge_manifest
    pin = _target(session, connector)
    validate_bridge_manifest(release.manifest)
    _require(release.version == FACTORY_BRIDGE_VERSION and release.state == "HIL_ONLY" and release.revoked_at is None,
             "FACTORY_TRIAL_RELEASE_UNAVAILABLE")
    _require(capability_is_eligible(connector) and connector.connected and connector.boot_id
        and connector.ota_state in {"OTA_READY", "UPDATING"} and _versions_match(connector.firmware_version, "2.5.2")
        and connector.ota_running_partition == "factory"
        and connector.ota_image_sha256 == pin["factory_application_sha256"], "FACTORY_PREDECESSOR_MISMATCH")
    now = utc_now()
    _require(connector.last_seen_at is not None and 0 <= (now - ensure_utc(connector.last_seen_at)).total_seconds() <= 45,
             "FACTORY_PREDECESSOR_STALE")
    terminal = connector.zkt_device
    _require(terminal is not None and terminal.online and terminal.connection_state in {"ONLINE", "STABLE"}
        and not terminal.writes_disabled_reason and not connector.last_error_code
        and type(terminal.attendance_count) is int and terminal.attendance_count >= 0, "FACTORY_TERMINAL_NOT_READY")
    _no_authority_history(session, connector)
    for model, clauses in (
        (DeviceCommand, [DeviceCommand.connector_id == connector.id, DeviceCommand.status.in_(ACTIVE_COMMAND_STATES)]),
        (TemporaryAdminLease, [TemporaryAdminLease.zkt_device_id == terminal.id, TemporaryAdminLease.state.in_(ACTIVE_LEASE_STATES)]),
        (ReconciliationJob, [ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status.not_in(["COMPLETED", "CANCELLED", "FAILED", "INVALIDATED"])]),
        (DeviceAlert, [DeviceAlert.connector_id == connector.id, DeviceAlert.state != "RESOLVED",
            DeviceAlert.code.in_(["ESP_DURABILITY_FAULT", "TERMINAL_IDENTITY_MISMATCH"])])
    ):
        _require(not session.scalar(select(model.id).where(*clauses).limit(1)), "FACTORY_EXISTING_SAFETY_HOLD")
    prior = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == connector.id,
        FirmwareDeployment.id != (own_deployment_id or -1)).order_by(FirmwareDeployment.id.desc()).limit(1))
    _require(prior is None or (prior.status not in ACTIVE_DEPLOYMENT_STATES | {"FAILED", "ROLLED_BACK"}
        and not (prior.offered_at is not None and prior.status in {"CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"})),
        "FACTORY_UNSETTLED_INSTALLATION")
    diagnostics = connector.firmware_diagnostics
    _require(diagnostics is None or isinstance(diagnostics, dict), "FACTORY_DIAGNOSTICS_INVALID")
    storage = (diagnostics or {}).get("storage") or {}
    _require(isinstance(storage, dict) and storage.get("durability") in {None, "HEALTHY"}
        and not any(storage.get(key) for key in ("error_code", "upgrade_error", "persistence_probe_error",
            "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults",
            "read_failures", "write_failures"))
        and not any(storage.get(key) is False for key in ("persistence_verified", "recovery_complete", "upgrade_ready")),
        "FACTORY_REPORTED_PERSISTENCE_FAULT")
    coverage = active_coverage(session, terminal)
    source = None if coverage is None else {"coverage_id": coverage.coverage_id,
        "epoch_id": coverage.source_epoch_id, "generation": coverage.terminal_generation,
        "cursor": coverage.source_committed_cursor, "chain": coverage.source_committed_chain_digest}
    return {"target": pin, "old_boot_id": connector.boot_id, "factory_application_sha256": connector.ota_image_sha256,
        "source": source, "terminal_count": terminal.attendance_count,
        "diagnostics_at": ensure_utc(connector.firmware_diagnostics_at).isoformat() if connector.firmware_diagnostics_at else None,
        "legacy_storage": storage or None, "queues": (diagnostics or {}).get("queues"),
        "pretrial_preservation": "NOT_ASSERTED", "pretrial_factory_signature": "NOT_ASSERTED",
        "unknown_legacy_diagnostics": diagnostics is None}


def admission_hold(session, release, connector, *, own_deployment_id=None):
    try:
        dependencies(session, release)
        predecessor_snapshot(session, release, connector, own_deployment_id=own_deployment_id)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return str(exc) if isinstance(exc, ValueError) else "FACTORY_EVIDENCE_INVALID"
    return None


def reserve_trial(session, release, campaign, deployment, connector):
    from zk_add.ota import FirmwareEvent
    baseline = predecessor_snapshot(session, release, connector, own_deployment_id=deployment.id)
    now = utc_now()
    body = {"schema_version": 1, "trial_id": str(uuid4()), "challenge": secrets.token_hex(32),
        "deployment_id": deployment.deployment_id, "campaign_id": campaign.campaign_id,
        "release": _release_identity(release), "baseline": baseline, "dependencies": dependencies(session, release),
        "reserved_at": now.isoformat(), "expires_at": (now + timedelta(seconds=FACTORY_TRIAL_SECONDS)).isoformat()}
    session.add(FirmwareEvent(deployment_id=deployment.id, state=RESERVED, details=_sealed(body)))
    session.flush()
    return body


def _trial(session, connector, deployment_id, *, require_new_boot):
    from zk_add.ota import FirmwareDeployment, FirmwareCampaign, FirmwareEvent, FirmwareRelease
    connector = session.scalar(select(Connector).where(Connector.id == connector.id).with_for_update()
        .execution_options(populate_existing=True))
    pin = _target(session, connector)
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.deployment_id == deployment_id,
        FirmwareDeployment.connector_id == connector.id).with_for_update().execution_options(populate_existing=True))
    _require(deployment is not None, "FACTORY_TRIAL_DEPLOYMENT_UNKNOWN")
    release = session.get(FirmwareRelease, deployment.release_id, populate_existing=True)
    campaign = session.get(FirmwareCampaign, deployment.campaign_id, populate_existing=True)
    _require(release is not None and release.version == FACTORY_BRIDGE_VERSION and release.state == "HIL_ONLY"
        and release.revoked_at is None and campaign is not None and campaign.status in {"ACTIVE", "COMPLETED"}
        and deployment.status not in {"FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"},
        "FACTORY_TRIAL_NO_LONGER_ACTIVE")
    latest = session.scalar(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc()).limit(1))
    _require(latest == deployment.id, "FACTORY_TRIAL_SUPERSEDED")
    rows = list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state == RESERVED).limit(2)))
    _require(len(rows) == 1, "FACTORY_TRIAL_RESERVATION_REQUIRED")
    trial = _verified(rows[0].details)
    _require(trial["deployment_id"] == deployment_id and trial["campaign_id"] == campaign.campaign_id
        and trial["release"] == _release_identity(release) and trial["baseline"]["target"] == pin,
        "FACTORY_TRIAL_BINDING_CHANGED")
    _require(_time(trial["reserved_at"]) <= utc_now() <= _time(trial["expires_at"])
        and _time(trial["expires_at"]) - _time(trial["reserved_at"]) == timedelta(seconds=FACTORY_TRIAL_SECONDS),
        "FACTORY_TRIAL_EXPIRED")
    _require(pin["connector_id"] in {item["connector_id"] for item in factory_trial_exposure(
        (release.manifest or {}).get("_hil_targets"))}, "FACTORY_TRIAL_EXPOSURE_CHANGED")
    _require(dependencies(session, release) == trial["dependencies"], "FACTORY_TRIAL_DEPENDENCY_CHANGED")
    _no_authority_history(session, connector)
    if require_new_boot:
        _require(deployment.status in {"READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING", "SUCCEEDED"}
            and deployment.offered_at is not None and deployment.bytes_written == release.image_size
            and release.image_size > 0, "FACTORY_TRIAL_INSTALLATION_UNVERIFIED")
        from zk_add.ota import _application_sha256, _versions_match
        row = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == connector.id)
            .order_by(DeviceTelemetry.id.desc()).limit(1))
        ota = (row.payload or {}).get("ota") or {} if row else {}
        _require(row is not None and connector.boot_id and row.boot_id == connector.boot_id
            and row.boot_id != trial["baseline"]["old_boot_id"]
            and 0 <= (utc_now() - ensure_utc(row.created_at)).total_seconds() <= 45
            and _versions_match(connector.firmware_version, FACTORY_BRIDGE_VERSION)
            and ota.get("running_version") == FACTORY_BRIDGE_VERSION
            and connector.ota_image_sha256 == ota.get("image_sha256") == _application_sha256(release)
            and ota.get("running_partition") in {"ota_0", "ota_1"}
            and ota.get("secure_boot") is True and ota.get("rollback_enabled") is True,
            "FACTORY_TRIAL_NEW_BOOT_UNVERIFIED")
    else:
        current = predecessor_snapshot(session, release, connector, own_deployment_id=deployment.id)
        baseline = trial["baseline"]
        _require(current["old_boot_id"] == baseline["old_boot_id"]
            and current["terminal_count"] >= baseline["terminal_count"], "FACTORY_TRIAL_PREDECESSOR_CHANGED")
        old, new = baseline["source"], current["source"]
        _require((old is None and new is None) or (old is not None and new is not None
            and all(old[key] == new[key] for key in ("coverage_id", "epoch_id", "generation"))
            and new["cursor"] >= old["cursor"] and (new["cursor"] != old["cursor"] or new["chain"] == old["chain"])),
            "FACTORY_TRIAL_SOURCE_CHANGED")
    return connector, deployment, release, trial


def context(session, connector, deployment_id):
    connector, deployment, release, trial = _trial(session, connector, deployment_id, require_new_boot=True)
    return {key: trial[key] for key in ("trial_id", "challenge", "deployment_id", "expires_at")} | {
        "onboarding_generation": connector.onboarding_generation,
        "expires_epoch": int(_time(trial["expires_at"]).timestamp())}


def accept_proof(session, connector, deployment_id, proof):
    from zk_add.ota import FirmwareEvent, FirmwareDeployment, FirmwareCampaign, FirmwareRelease
    value = FactoryTrialProof.model_validate(proof).model_dump()
    # Strict literals in Pydantic compare equal to integers; forbid such wire coercion.
    _require(type(proof.get("schema_version")) is int and all(type(proof.get(key)) is bool for key in (
        "checkpoint_verified", "secure_boot_verified", "signature_verified", "encrypted_nvs", "rollback_enabled",
        "anti_rollback_disabled", "factory_fallback_verified", "legacy_only")), "FACTORY_PROOF_TYPES")
    connector, deployment, release, trial = _trial(session, connector, deployment_id, require_new_boot=True)
    _require(value["deployment_id"] == deployment_id and value["trial_id"] == trial["trial_id"]
        and value["challenge"] == trial["challenge"] and value["boot_id"] == connector.boot_id
        and value["onboarding_generation"] == connector.onboarding_generation
        and value["reader_application_sha256"] == trial["release"]["application_sha256"]
        and value["factory_application_sha256"] == trial["baseline"]["factory_application_sha256"]
        and value["layout_sha256"] == FACTORY_LAYOUT_SHA256
        and all(value[key] != "0" * 64 for key in ("factory_signed_image_sha256", "layout_sha256", "checkpoint_sha256")),
        "FACTORY_PROOF_BINDING_MISMATCH")
    reports = list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(PROOF_EVENTS)).order_by(FirmwareEvent.id).limit(3)))
    _require(len(reports) <= 2, "FACTORY_PROOF_HISTORY_INVALID")
    for report in reports:
        body = _verified(report.details)
        prior = body["proof"]
        if prior["proof_state"] == value["proof_state"]:
            _require(prior == value and body["trial_id"] == trial["trial_id"], "FACTORY_PROOF_REPLAY_CHANGED")
            return _receipt(value)
        _require(prior["proof_state"] != "FACTORY_FALLBACK_REVOKED", "FACTORY_PROOF_CANNOT_REGRESS")
        _require(prior["checkpoint_sha256"] != value["checkpoint_sha256"], "FACTORY_REVOCATION_CHECKPOINT_UNCHANGED")
        _require(all(prior[key] == value[key] for key in value if key not in {"proof_state", "checkpoint_sha256"}),
                 "FACTORY_PROOF_CONTENT_CHANGED")
    # Cancellation/revocation are checked in the same write that linearizes proof acceptance.
    allowed = exists(select(FirmwareCampaign.id).where(FirmwareCampaign.id == deployment.campaign_id,
        FirmwareCampaign.status.in_(["ACTIVE", "COMPLETED"]))) & exists(select(FirmwareRelease.id).where(
        FirmwareRelease.id == release.id, FirmwareRelease.state == "HIL_ONLY", FirmwareRelease.revoked_at.is_(None)))
    serial = trial["baseline"]["target"]["terminal_serial"]
    allowed &= exists(select(ZKTDevice.id).where(ZKTDevice.connector_id == connector.id,
        ZKTDevice.serial == serial, ZKTDevice.expected_serial == serial, ZKTDevice.confirmed_serial == serial,
        ZKTDevice.terminal_binding_state == "CONFIRMED"))
    changed = session.execute(update(FirmwareDeployment).where(FirmwareDeployment.id == deployment.id,
        FirmwareDeployment.status.not_in(["FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"]),
        allowed).values(updated_at=utc_now()).execution_options(synchronize_session=False))
    _require(changed.rowcount == 1, "FACTORY_TRIAL_NO_LONGER_ACTIVE")
    body = {"schema_version": 1, "trial_id": trial["trial_id"], "reservation_sha256": _digest(trial),
        "release": trial["release"], "target": trial["baseline"]["target"], "proof": value,
        "received_at": utc_now().isoformat(), "scope": "AUTHENTICATED_DEVICE_SOFTWARE_PROOF",
        "physical_qualification": "NOT_PERFORMED"}
    session.add(FirmwareEvent(deployment_id=deployment.id, state=value["proof_state"], details=_sealed(body)))
    session.flush()
    return _receipt(value)


def _receipt(value):
    return {"accepted": True, **{key: value[key] for key in
        ("trial_id", "challenge", "deployment_id", "boot_id", "checkpoint_sha256", "proof_state")}}


def revoked_evidence(session, connector, deployment, release, *, current_boot=False):
    """Historical permanent revocation is separate from fresh current reader health."""
    from zk_add.ota import FirmwareEvent
    _require(release.version == FACTORY_BRIDGE_VERSION and release.state == "HIL_ONLY" and release.revoked_at is None,
             "FACTORY_TRIAL_RELEASE_UNAVAILABLE")
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state == "FACTORY_FALLBACK_REVOKED").order_by(FirmwareEvent.id.desc()).limit(1))
    _require(event is not None, "FACTORY_FALLBACK_REVOCATION_REQUIRED")
    body = _verified(event.details)
    value = FactoryTrialProof.model_validate(body["proof"]).model_dump()
    reserved = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state == RESERVED).order_by(FirmwareEvent.id).limit(1))
    _require(reserved is not None, "FACTORY_TRIAL_RESERVATION_REQUIRED")
    reservation = _verified(reserved.details)
    _require(body["reservation_sha256"] == _digest(reservation)
        and body["trial_id"] == reservation["trial_id"] == value["trial_id"]
        and value["challenge"] == reservation["challenge"]
        and reservation["release"] == body["release"]
        and reservation["baseline"]["target"] == body["target"]
        and value["factory_application_sha256"] == body["target"]["factory_application_sha256"]
        and value["reader_application_sha256"] == body["release"]["application_sha256"]
        and value["layout_sha256"] == FACTORY_LAYOUT_SHA256
        and value["onboarding_generation"] == body["target"]["onboarding_generation"]
        and _time(reservation["reserved_at"]) <= _time(body["received_at"]) <= _time(reservation["expires_at"])
        and _time(body["received_at"]) <= utc_now(), "FACTORY_REVOCATION_RESERVATION_MISMATCH")
    _require(body["target"] == _target(session, connector) and body["release"] == _release_identity(release)
        and value["deployment_id"] == deployment.deployment_id and value["proof_state"] == "FACTORY_FALLBACK_REVOKED"
        and (not current_boot or value["boot_id"] == connector.boot_id), "FACTORY_REVOCATION_BINDING_CHANGED")
    return {"event_id": event.id, "trial_id": body["trial_id"], "checkpoint_sha256": value["checkpoint_sha256"],
            "proof_sha256": _digest(body), "boot_id": value["boot_id"]}

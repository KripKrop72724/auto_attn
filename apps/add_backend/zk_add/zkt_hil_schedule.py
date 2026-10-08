"""Bounded connectivity-only scheduling; deferred devices never become passed."""
from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import select

from zk_add.hil_scope import target_matches
from zk_add.models import Connector, DeviceAlert, ReconciliationJob
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import TARGETS

POLICY = "ZKT_CONNECTIVITY_DEFERRAL_V1"
RESERVATION = "HIL_SCOPE_RESERVED"
RESERVATION_LIMIT = 128


def applies(release):
    return release.state == "HIL_ONLY" and release.version in {"2.6.20", "2.6.21", "2.7.0"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(release):
    from zk_add.hil_runs import _release_identity
    return {"release_id": release.release_id, **_release_identity(release).model_dump(mode="json")}


def _seal(value):
    from zk_add.ota import _scope_signing_key
    return hmac.new(_scope_signing_key(), digest(value).encode(), hashlib.sha256).hexdigest()


def _acceptance(session, release, target, connector):
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun
    from zk_add.bridge_observation import EVENTS, ready_event_matches
    from zk_add.hil_runs import accepted_full_event_matches
    if connector is None or not target_matches(target, connector):
        return None
    row = session.execute(select(FirmwareEvent, FirmwareDeployment, FirmwareCampaign)
        .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(FirmwareCampaign, FirmwareDeployment.campaign_id == FirmwareCampaign.id)
        .where(FirmwareDeployment.release_id == release.id,
               FirmwareDeployment.connector_id == connector.id,
               FirmwareEvent.state.in_(["HIL_ACCEPTED", "HIL_FAILED", "HIL_INCOMPLETE", *EVENTS]))
        .order_by(FirmwareEvent.id.desc()).limit(1)).first()
    if row is None:
        return None
    event, deployment, campaign = row
    detail = event.details or {}
    expected = _identity(release)
    if (deployment.status != "SUCCEEDED" or campaign.status not in {"ACTIVE", "COMPLETED"}
            or detail.get("target") != target.model_dump()
            or any(detail.get(key) != value for key, value in expected.items() if key != "release_id")):
        return None
    latest = session.scalar(select(FirmwareDeployment.id)
        .where(FirmwareDeployment.release_id == release.id, FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc()).limit(1))
    if latest != deployment.id:
        return None
    check = accepted_full_event_matches if release.version == "2.7.0" else ready_event_matches
    if not check(session, event, deployment, release):
        return None
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == detail["run_id"]))
    if run is None or ensure_utc(run.completed_at) > utc_now():
        return None
    return {"event_id": event.id, "deployment_id": deployment.id, "run_id": detail["run_id"],
            "completed_at": ensure_utc(run.completed_at).isoformat()}


def _capability_untested(connector):
    # report_firmware_capability always changes this state to OTA_READY or
    # OTA_BLOCKED. Only the untouched legacy default means no such report.
    return (connector.ota_state == "LEGACY_MANUAL_UPDATE" and not connector.ota_capable
            and not connector.ota_secure_boot and not connector.ota_rollback_enabled)


def _known_hold(session, connector):
    """Never reinterpret a recorded safety defect or uncertain install as offline."""
    from zk_add.ota import ACTIVE_DEPLOYMENT_STATES, FirmwareCampaign, FirmwareDeployment, capability_is_eligible
    if connector.firmware_family != "zkt" or (not capability_is_eligible(connector)
                                               and not _capability_untested(connector)):
        return "SECURITY_OR_CAPABILITY_UNVERIFIED"
    if connector.last_error_code:
        return "CONNECTOR_ERROR_REQUIRES_REVIEW"
    if connector.zkt_device.writes_disabled_reason:
        return "TERMINAL_IDENTITY_HOLD"
    if session.scalar(select(ReconciliationJob.id).where(ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status == "NEEDS_ATTENTION").limit(1)):
        return "SOURCE_OR_IDENTITY_REVIEW_REQUIRED"
    diagnostics = connector.firmware_diagnostics or {}
    storage = diagnostics.get("storage") or {}
    if (storage.get("durability") not in {None, "HEALTHY"}
            or any(storage.get(name) is False for name in
                   ("persistence_verified", "recovery_complete", "upgrade_ready"))
            or any(storage.get(name) for name in ("error_code", "upgrade_error", "persistence_probe_error",
                "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))):
        return "PRESERVATION_NOT_VERIFIED"
    runtime = diagnostics.get("journal_runtime")
    if runtime is not None and (not isinstance(runtime, dict) or runtime.get("phase") != "READY"
            or runtime.get("reader_ready") is not True or runtime.get("compatibility") != ""):
        return "JOURNAL_READER_HOLD"
    journal = diagnostics.get("journal_storage") or {}
    if journal.get("hil_reboot_persistence_incident") is True:
        return "JOURNAL_PERSISTENCE_INCIDENT"
    from zk_add.zkt_ota_admission import reservation_snapshot
    snapshot = reservation_snapshot(session)
    if not snapshot["scan_complete"]:
        return "INSTALLATION_RESERVATION_SCAN_LIMIT"
    if any(row["connector_id"] == connector.connector_id and not row["later_installation"]
           for row in snapshot["reservations"]):
        return "INSTALLATION_FAILED_OR_UNSETTLED"
    last = session.execute(select(FirmwareDeployment, FirmwareCampaign)
        .join(FirmwareCampaign, FirmwareDeployment.campaign_id == FirmwareCampaign.id)
        .where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc()).limit(1)).first()
    if last:
        deployment, campaign = last
        if (deployment.status in {"FAILED", "ROLLED_BACK"}
                or deployment.status in ACTIVE_DEPLOYMENT_STATES
                or (deployment.offered_at is not None and deployment.status in
                    {"CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"})):
            return "INSTALLATION_FAILED_OR_UNSETTLED"
        if deployment.status == "PENDING" and campaign.status in {"ACTIVE", "PAUSED"}:
            return "INSTALLATION_RESERVED"
    return None


def _row(session, release, item, connector, exposed):
    target = item.identity
    row = {"target": target.model_dump(), "name": item.name, "wave": item.wave,
           "exposed": exposed, "status": "PENDING", "reason": "AWAITING_TURN", "accepted": None,
           "prerequisites": [], "current_hold": None,
           "connected": connector.connected if connector else None,
           "last_seen_at": ensure_utc(connector.last_seen_at).isoformat() if connector and connector.last_seen_at else None}
    if connector is None or not target_matches(target, connector) or connector.firmware_family != "zkt":
        row.update(status="BLOCKED", reason="EXACT_ACTIVE_INVENTORY_REQUIRED")
        return row
    if _capability_untested(connector):
        row["prerequisites"].append("OTA_CAPABILITY_NOT_OBSERVED")
    accepted = _acceptance(session, release, target, connector)
    if accepted:
        row.update(status="PASSED", reason="STORED_QUALIFIED_VERDICT", accepted=accepted)
        row["current_hold"] = _post_verdict_hold(session, connector, run_completed_at=accepted["completed_at"])
        return row
    hold = _known_hold(session, connector)
    if release.version == "2.7.0" and not hold:
        from zk_add.zkt_writer_contract import qualified_bridge_hold
        qualification = qualified_bridge_hold(session, connector)
        if qualification in {"JOURNAL_BRIDGE_INSTALL_NOT_VERIFIED", "JOURNAL_BRIDGE_READY_MISSING",
                              "JOURNAL_BRIDGE_ARTIFACT_MISSING"}:
            row["prerequisites"].append(qualification)
        elif qualification:
            hold = qualification
    # An online target is never skipped, even when it needs prerequisite work.
    if connector.connected:
        row.update(reason=hold or "AWAITING_QUALIFIED_VERDICT")
        if hold:
            row["status"] = "BLOCKED"
        return row
    if hold:
        row.update(status="BLOCKED", reason=hold)
        return row
    now = utc_now()
    if connector.last_seen_at is None or not settings.offline_after_seconds < (
            now - ensure_utc(connector.last_seen_at)).total_seconds():
        row.update(status="BLOCKED", reason="OFFLINE_NOT_ESTABLISHED")
        return row
    diagnostics = connector.firmware_diagnostics or {}
    reported = connector.firmware_diagnostics_at
    if ((reported is not None and not ensure_utc(reported) <= ensure_utc(connector.last_seen_at) <= now)
            or (diagnostics.get("boot_id") is not None and diagnostics.get("boot_id") != connector.boot_id)):
        row.update(status="BLOCKED", reason="LAST_KNOWN_HEALTH_UNVERIFIED")
        return row
    storage = diagnostics.get("storage") or {}
    if (reported is None or not connector.boot_id or not diagnostics.get("boot_id")
            or storage.get("durability") is None or any(storage.get(key) is None for key in
                ("persistence_verified", "recovery_complete", "upgrade_ready"))):
        row["prerequisites"].append("PRESERVATION_DIAGNOSTICS_NOT_OBSERVED")
    row.update(status="DEFERRED_OFFLINE", reason="CONNECTOR_OFFLINE_ONLY")
    return row


def _post_verdict_hold(session, connector, *, run_completed_at):
    """Preserve historical acceptance, but stop expansion on new proven defects.

    Missing telemetry, an unobserved capability, and normal boot recovery are
    not invented failures. Release-specific boot/HIL checks own those states.
    """
    from datetime import datetime
    completed = datetime.fromisoformat(run_completed_at)
    if connector.zkt_device.writes_disabled_reason:
        return "TERMINAL_IDENTITY_HOLD"
    if session.scalar(select(ReconciliationJob.id).where(ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status == "NEEDS_ATTENTION", ReconciliationJob.updated_at >= completed).limit(1)):
        return "POST_VERDICT_SOURCE_OR_IDENTITY_HOLD"
    # Accepted readiness already proves these capabilities. A later explicit
    # false report remains a regression even though report_firmware_capability
    # also sets ota_capable=false. Age or transport loss cannot clear it.
    if connector.ota_secure_boot is False or connector.ota_rollback_enabled is False:
        return "SECURITY_CAPABILITY_REGRESSED"
    if session.scalar(select(DeviceAlert.id).where(DeviceAlert.connector_id == connector.id,
            DeviceAlert.code == "ESP_DURABILITY_FAULT", DeviceAlert.resolved_at.is_(None),
            DeviceAlert.state != "RESOLVED", DeviceAlert.last_seen_at >= completed).limit(1)):
        return "POST_VERDICT_PERSISTENCE_FAULT"
    diagnostics = connector.firmware_diagnostics or {}
    sampled = connector.firmware_diagnostics_at
    if (sampled is None or not diagnostics.get("boot_id")
            or not completed <= ensure_utc(sampled) <= utc_now()):
        return None
    storage = diagnostics.get("storage") or {}
    runtime = diagnostics.get("journal_runtime") or {}
    if (any(storage.get(key) for key in ("error_code", "persistence_probe_error", "legacy_error_code",
            "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))
            or (storage.get("durability") in {"FAILED", "FULL", "DEGRADED"}
                and runtime.get("phase") not in {"INITIALIZING", "RECOVERING"})
            or (diagnostics.get("journal_storage") or {}).get("hil_reboot_persistence_incident") is True):
        return "POST_VERDICT_PERSISTENCE_FAULT"
    return None


def _reservation(session, release, rows):
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent
    from zk_add.zkt_ota_admission import _later_installation
    records = session.execute(select(FirmwareEvent, FirmwareDeployment, FirmwareCampaign)
        .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(FirmwareCampaign, FirmwareDeployment.campaign_id == FirmwareCampaign.id)
        .where(FirmwareDeployment.release_id == release.id, FirmwareEvent.state == RESERVATION)
        .order_by(FirmwareEvent.id.desc()).limit(RESERVATION_LIMIT + 1)).all()
    if len(records) > RESERVATION_LIMIT:
        return None, "RESERVATION_SCAN_LIMIT"
    active = []
    for event, deployment, campaign in records:
        body = dict(event.details or {})
        seal = body.pop("authorization", None)
        if (not isinstance(seal, str) or not hmac.compare_digest(seal, _seal(body))
                or body.get("policy") != POLICY or body.get("release") != _identity(release)
                or not isinstance(body.get("decision_sha256"), str) or len(body["decision_sha256"]) != 64
                or body.get("deployment_id") != deployment.deployment_id
                or body.get("campaign_id") != campaign.campaign_id):
            return None, "RESERVATION_EVIDENCE_INVALID"
        row = next((row for row in rows if row["target"] == body.get("target")), None)
        connector = session.get(Connector, deployment.connector_id)
        if row is None or connector is None or connector.connector_id != row["target"]["connector_id"]:
            return None, "RESERVATION_TARGET_INVALID"
        if row["accepted"] and row["accepted"]["deployment_id"] == deployment.id:
            continue
        if _historical_verdict(session, release, deployment, row["target"]):
            continue
        if campaign.status == "CANCELLED" and deployment.offered_at is None and deployment.status == "CANCELLED":
            continue
        if _later_installation(session, deployment, campaign):
            continue
        if (not row["exposed"] or not target_matches(TARGETS[rows.index(row)].identity, connector)
                or (body.get("canary") != rows[0]["accepted"] and row is not rows[0])):
            return None, "RESERVATION_SCOPE_CHANGED"
        if campaign.status not in {"ACTIVE", "COMPLETED"} or deployment.status in {
                "FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"}:
            return None, "RESERVATION_INSTALLATION_HELD"
        latest = session.scalar(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == connector.id)
            .order_by(FirmwareDeployment.id.desc()).limit(1))
        if latest != deployment.id:
            return None, "RESERVATION_SUPERSEDED"
        active.append({"event_id": event.id, "deployment_id": deployment.id,
                       "target": row["target"], "decision_sha256": body["decision_sha256"]})
    if len(active) > 1:
        return None, "MULTIPLE_UNSETTLED_SCOPE_RESERVATIONS"
    return (active[0] if active else None), None


def _historical_verdict(session, release, deployment, target):
    """A later attempt does not resurrect an already settled scope reservation.

    This only closes ownership; current acceptance and all admission checks
    remain separate and never inherit this historical result.
    """
    from zk_add.ota import FirmwareEvent, FirmwareHilRun
    from zk_add.bridge_observation import EVENTS, ready_event_matches
    from zk_add.hil_runs import accepted_full_record_matches
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["HIL_ACCEPTED", "HIL_FAILED", "HIL_INCOMPLETE", *EVENTS]))
        .order_by(FirmwareEvent.id.desc()).limit(1))
    if event is None or deployment.status != "SUCCEEDED":
        return False
    detail = event.details or {}
    if (detail.get("target") != target or any(detail.get(key) != value
            for key, value in _identity(release).items() if key != "release_id")):
        return False
    valid = accepted_full_record_matches if release.version == "2.7.0" else ready_event_matches
    if not valid(session, event, deployment, release):
        return False
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == detail["run_id"]))
    return run is not None and ensure_utc(run.completed_at) <= utc_now()


def schedule(session, release):
    """One shared bounded decision for preflight, assignment, download and HIL."""
    if not applies(release):
        return None
    from zk_add.zkt_bridge_contract import bridge_hil_targets, signed_hil_targets
    from zk_add.storage_contract import validate_storage_contract
    validate_storage_contract(release.manifest or {}, release.version)
    if (release.manifest or {}).get("hil_targets") != signed_hil_targets():
        raise ValueError("Signed nationwide HIL scope is incomplete.")
    exposed = bridge_hil_targets((release.manifest or {}).get("_hil_targets"))
    connectors = {row.connector_id: row for row in session.scalars(select(Connector)
        .where(Connector.connector_id.in_([item.identity.connector_id for item in TARGETS])))}
    rows = [_row(session, release, item, connectors.get(item.identity.connector_id), index < len(exposed))
            for index, item in enumerate(TARGETS)]
    reservation, hold = _reservation(session, release, rows)
    expansion_hold = next((row["target"]["connector_id"] + ":" + row["current_hold"]
                           for row in rows if row["current_hold"]), None)
    if not hold and not reservation and expansion_hold:
        hold = "EXPANSION_HELD:" + expansion_hold
    selected = None
    if not hold and reservation:
        selected = reservation["target"]
        row = next(row for row in rows if row["target"] == selected)
        # An owned installation is pending work, never offline deferral. Other
        # safety defects remain visible and downstream admission still applies.
        if row["reason"] in {"INSTALLATION_RESERVED", "INSTALLATION_FAILED_OR_UNSETTLED"}:
            row.update(status="PENDING", reason="RESERVED_INSTALLATION_IN_PROGRESS")
    elif not hold:
        for index, row in enumerate(rows[:len(exposed)]):
            if row["status"] == "PASSED":
                continue
            if index and rows[0]["status"] != "PASSED":
                break
            if index and row["status"] == "DEFERRED_OFFLINE":
                continue
            # Do not skip a hard hold. The normal target preflight explains
            # the prerequisite; the first canary remains mandatory offline.
            selected = row["target"]
            break
    counts = {state: sum(row["status"] == state for row in rows)
              for state in ("PASSED", "PENDING", "DEFERRED_OFFLINE", "BLOCKED")}
    normalized = {"policy": POLICY, "release": _identity(release), "exposed": [t.model_dump() for t in exposed],
        "selected": selected, "reservation": reservation, "hold": hold,
        "rows": [{key: row[key] for key in ("target", "exposed", "status", "reason", "prerequisites", "current_hold", "accepted", "connected")}
                 for row in rows]}
    return {"policy": POLICY, "denominator": len(TARGETS), "rows": rows, "counts": counts,
            "selected": selected, "reservation": reservation, "hold": hold,
            "decision_sha256": digest(normalized)}


def lock_campaign_scope(session, release_public_id):
    """Lock the bounded inventory before the release, matching HIL lock order."""
    from zk_add.ota import FirmwareRelease
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_public_id))
    if release is None or not applies(release):
        return
    list(session.scalars(select(Connector).where(Connector.connector_id.in_(
        [item.identity.connector_id for item in TARGETS])).order_by(Connector.id).with_for_update()
        .execution_options(populate_existing=True)))
    session.scalar(select(FirmwareRelease).where(FirmwareRelease.id == release.id).with_for_update()
        .execution_options(populate_existing=True))


def reserve(session, release, campaign, deployment, decision):
    from zk_add.ota import FirmwareEvent
    if decision is None:
        return
    if decision["hold"] or decision["reservation"] or decision["selected"] is None:
        raise ValueError("Nationwide HIL scope already has an unsettled reservation.")
    row = next(row for row in decision["rows"] if row["target"] == decision["selected"])
    connector = session.get(Connector, deployment.connector_id)
    if connector is None or connector.connector_id != row["target"]["connector_id"]:
        raise ValueError("Nationwide HIL scope target changed.")
    body = {"policy": POLICY, "release": _identity(release), "campaign_id": campaign.campaign_id,
        "deployment_id": deployment.deployment_id, "target": row["target"],
        "canary": decision["rows"][0]["accepted"], "decision_sha256": decision["decision_sha256"],
        "deferred": [dict(other) for other in decision["rows"][:decision["rows"].index(row)]
                     if other["status"] == "DEFERRED_OFFLINE"]}
    session.add(FirmwareEvent(deployment_id=deployment.id, state=RESERVATION,
                              details={**body, "authorization": _seal(body)}))


def campaign_audit_scope(session, campaign):
    from zk_add.ota import FirmwareEvent, FirmwareDeployment
    event = session.scalar(select(FirmwareEvent).join(FirmwareDeployment,
        FirmwareEvent.deployment_id == FirmwareDeployment.id).where(
        FirmwareDeployment.campaign_id == campaign.id, FirmwareEvent.state == RESERVATION)
        .order_by(FirmwareEvent.id.desc()).limit(1))
    if event is None:
        return {}
    return {"hil_schedule_policy": event.details["policy"],
            "hil_schedule_decision_sha256": event.details["decision_sha256"],
            "hil_scope_reservation_event_id": event.id}

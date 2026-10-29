"""Bridge the original 2.4.12 protocol on the exact BLD5, Lahore and BLD9 K50 ZKTs.

Legacy boot reports are transport acknowledgements, not installation proof.
The ordinary checked transitions run only once one boot has supplied all three
parts of the proof: fresh telemetry naming that boot on 2.4.12, the device's
own RUNTIME_HEALTHY and RECONCILING reports from that boot, and a signed
capability report from that boot naming the published application in an OTA
partition. The boot ID binds them, so they may arrive in either order and at
any distance apart: 2.4.12 sends its capability once per boot, only after its
first SUCCEEDED report, and never again once ADD has accepted it.
"""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from zk_add.hil_scope import target_matches
from zk_add.hil_2615_cities import SIGNED_BRIDGE_IDENTITIES
from zk_add import zkt274_bld9_k50 as bld9_k50
from zk_add.models import Connector, DeviceTelemetry
from zk_add.ota import (
    FACTORY_BRIDGE_PREDECESSORS, FACTORY_BRIDGE_TARGETS, FirmwareCampaign, FirmwareDeployment, FirmwareEvent,
    FirmwareRelease, HIL_2615_BLD5_TARGET, _application_sha256, _factory_3fl_bridge_target, _versions_match,
    record_progress,
)
from zk_add.time_utils import ensure_utc, utc_now


BRIDGE_IDENTITY = SIGNED_BRIDGE_IDENTITIES["2.4.12"]
BRIDGE_TARGETS = {"LF-ZONE-BLD5-01": HIL_2615_BLD5_TARGET, **{
    zone: FACTORY_BRIDGE_TARGETS[zone] for zone, predecessor in FACTORY_BRIDGE_PREDECESSORS.items()
    if predecessor[2] == "2.4.12"
}, bld9_k50.ZONE: bld9_k50.TARGET}
# (version, application digest) each factory-gated bridge zone ran before 2.4.12.
FACTORY_PREDECESSORS = {
    zone: predecessor[:2] for zone, predecessor in
    {**FACTORY_BRIDGE_PREDECESSORS, bld9_k50.ZONE: bld9_k50.FACTORY_PREDECESSOR}.items()
    if zone in BRIDGE_TARGETS
}
REPORT, CAPABILITY, VERIFIED = (
    "LEGACY_BOOT_REPORT", "LEGACY_BOOT_CAPABILITY_REPORT", "LEGACY_BOOT_CAPABILITY_VERIFIED")
# One deployment's deduplicated bridge events and one predecessor boot's
# heartbeats are far below these; reaching either refuses rather than guesses.
EVENT_LIMIT = 200
TELEMETRY_LIMIT = 500


def _bridge(session: Session, connector: Connector, deployment_id: str | None = None, *, active: bool = True):
    """Return the exact bridge deployment and its release, or None.

    With active=False the campaign need not be ACTIVE nor the release
    AVAILABLE. Such a bridge may only have evidence recorded against it; it
    must never move the deployment.
    """
    target = BRIDGE_TARGETS.get(connector.zone_id)
    if (connector.firmware_family != "zkt" or target is None
            or not target_matches(target, connector)):
        return None
    query = select(FirmwareDeployment).join(FirmwareCampaign).where(
        FirmwareDeployment.connector_id == connector.id,
        FirmwareDeployment.status.in_(["READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"]),
        FirmwareCampaign.zone_id == connector.zone_id,
        FirmwareCampaign.release_id == FirmwareDeployment.release_id,
    )
    if active:
        query = query.where(FirmwareCampaign.status == "ACTIVE")
    if deployment_id is not None:
        query = query.where(FirmwareDeployment.deployment_id == deployment_id)
    deployment = session.scalar(query.order_by(FirmwareDeployment.id.desc()).limit(1).with_for_update())
    if deployment is None or not _versions_match(deployment.previous_version, "2.5.2"):
        return None
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or (active and release.state != "AVAILABLE") or (
        release.release_id, release.version, release.git_sha, release.image_sha256,
        _application_sha256(release),
    ) != BRIDGE_IDENTITY or deployment.target_version != release.version or deployment.bytes_written != release.image_size:
        return None
    return deployment, release


def _fresh_boot(session: Session, connector: Connector) -> str:
    sample = session.scalar(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == connector.id,
    ).order_by(DeviceTelemetry.id.desc()).limit(1))
    if (not connector.connected or sample is None or not sample.boot_id
            or sample.boot_id != connector.boot_id
            or not 0 <= (utc_now() - ensure_utc(sample.created_at)).total_seconds() <= 45
            or not _versions_match((sample.payload or {}).get("firmware_version"), "2.4.12")):
        raise ValueError("Fresh current-boot telemetry is required for the exact legacy bridge.")
    return sample.boot_id


def _events(session: Session, deployment: FirmwareDeployment) -> list[FirmwareEvent]:
    """This deployment's bridge events, newest first; callers flush new ones first."""
    return list(session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_([REPORT, CAPABILITY, VERIFIED]),
    ).order_by(FirmwareEvent.id.desc()).limit(EVENT_LIMIT)))


def _of_boot(events: list[FirmwareEvent], boot_id: str, state: str) -> list[FirmwareEvent]:
    return [event for event in events if event.state == state and (event.details or {}).get("boot_id") == boot_id]


def _capability_matches(release: FirmwareRelease, capability: dict) -> bool:
    return (capability.get("running_version") == release.version
            and capability.get("running_partition") in {"ota_0", "ota_1"}
            and capability.get("image_sha256") == _application_sha256(release)
            and all(capability.get(key) is True for key in ("capable", "secure_boot", "rollback_enabled"))
            and capability.get("partition_layout") == release.partition_layout)


def _stored_capability_evidence(
    session: Session, connector: Connector, deployment: FirmwareDeployment, release: FirmwareRelease,
    boot_id: str, events: list[FirmwareEvent],
) -> dict | None:
    """Attribute ADD's stored capability fields to this boot when it sent no labelled report.

    Until 19 October 2026 ADD kept an accepted capability report only in these
    connector fields, and 2.4.12 never repeats an accepted report in the same
    boot. The fields belong to this boot only when every other writer is
    excluded: the offer and every download grant of a factory-gated bridge
    required the factory predecessor in them, the factory image itself staged
    2.4.12 after the offer, this boot was first heard after that staging, every
    bridge event of this deployment comes from this boot after that staging,
    and the boot before this one and every other boot heard since the offer
    ran the predecessor version.
    """
    predecessor = FACTORY_PREDECESSORS.get(connector.zone_id)
    try:
        gated = _factory_3fl_bridge_target(release, connector.zone_id) == BRIDGE_TARGETS[connector.zone_id]
    except ValueError:
        return None
    if predecessor is None or not gated or deployment.offered_at is None:
        return None
    if not (_versions_match(connector.firmware_version, release.version)
            and connector.ota_capable and connector.ota_secure_boot and connector.ota_rollback_enabled
            and connector.ota_partition_layout == release.partition_layout
            and connector.ota_running_partition in {"ota_0", "ota_1"}
            and connector.ota_image_sha256 == _application_sha256(release)):
        return None
    offered_at = ensure_utc(deployment.offered_at)
    staged = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "READY_TO_BOOT",
    ).order_by(FirmwareEvent.id.desc()).limit(1))
    staged_details = (staged.details or {}) if staged is not None else {}
    if (staged is None or ensure_utc(staged.created_at) < offered_at
            or not _versions_match(staged_details.get("running_version"), predecessor[0])
            or staged_details.get("running_partition") != "factory"
            or staged_details.get("image_sha256") != predecessor[1]):
        return None
    if len(events) >= EVENT_LIMIT or any(
        (event.details or {}).get("boot_id") != boot_id or event.id < staged.id for event in events
    ):
        return None
    # The factory image reboots only after ADD has recorded its staging, so
    # the 2.4.12 boot that staging started cannot have been heard before it.
    first = session.scalar(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.boot_id == boot_id,
    ).order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(1))
    if first is None or ensure_utc(first.created_at) < ensure_utc(staged.created_at):
        return None
    other_boot = or_(DeviceTelemetry.boot_id.is_(None), DeviceTelemetry.boot_id != boot_id)
    previous = session.scalar(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == connector.id, other_boot,
    ).order_by(DeviceTelemetry.id.desc()).limit(1))
    since_offer = list(session.scalars(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == connector.id, other_boot, DeviceTelemetry.created_at >= offered_at,
    ).order_by(DeviceTelemetry.id.desc()).limit(TELEMETRY_LIMIT)))
    if previous is None or len(since_offer) >= TELEMETRY_LIMIT or not all(
        _versions_match((sample.payload or {}).get("firmware_version"), predecessor[0])
        for sample in (previous, *since_offer)
    ):
        return None
    return {"source": "STORED_CAPABILITY", "staged_event_id": staged.id,
            "boot_first_heard_at": ensure_utc(first.created_at).isoformat(),
            "previous_boot_id": previous.boot_id, "previous_firmware": predecessor[0],
            "previous_image_sha256": predecessor[1], "other_boot_samples_since_offer": len(since_offer)}


def _proof(
    session: Session, connector: Connector, deployment: FirmwareDeployment, release: FirmwareRelease,
    boot_id: str, events: list[FirmwareEvent],
) -> dict | None:
    """Return this boot's proof, recording it once all three parts are present."""
    verified = _of_boot(events, boot_id, VERIFIED)
    if verified:
        return verified[0].details
    reports = _of_boot(events, boot_id, REPORT)
    if deployment.status != "READY_TO_BOOT" or not (
        any(event.details.get("error_code") == "RUNTIME_HEALTHY" for event in reports)
        and any(event.details.get("reported_state") == "RECONCILING" for event in reports)
    ):
        return None
    reported = _of_boot(events, boot_id, CAPABILITY)
    if reported:
        partition, digest = reported[0].details["running_partition"], reported[0].details["image_sha256"]
        evidence = {"source": "CAPABILITY_REPORT", "capability_event_id": reported[0].id}
    else:
        evidence = _stored_capability_evidence(session, connector, deployment, release, boot_id, events)
        if evidence is None:
            return None
        partition, digest = connector.ota_running_partition, connector.ota_image_sha256
    proof = {"boot_id": boot_id, "running_partition": partition, "image_sha256": digest, "evidence": evidence}
    session.add(FirmwareEvent(deployment_id=deployment.id, state=VERIFIED, details=proof))
    for state in ("BOOTED_PENDING", "RECONCILING"):
        record_progress(
            session, connector=connector, deployment_public_id=deployment.deployment_id,
            state=state, bytes_written=release.image_size, running_version=release.version,
            running_partition=partition, image_sha256=digest,
        )
        # record_progress re-reads the deployment with populate_existing, which
        # discards an unflushed transition, and ADD sessions do not autoflush:
        # without this the second step read READY_TO_BOOT again and raised.
        session.flush()
    return proof


def accept_legacy_progress(
    session: Session, *, connector: Connector, deployment_id: str, report: dict,
) -> dict | None:
    state = report.get("state")
    if (state not in {"BOOTED_PENDING", "RECONCILING", "SUCCEEDED"}
            or report.get("running_partition") is not None or report.get("image_sha256") is not None):
        return None
    bridge = _bridge(session, connector, deployment_id)
    if bridge is None:
        return None
    deployment, release = bridge
    if (not _versions_match(report.get("running_version"), release.version)
            or report.get("bytes_written") != release.image_size):
        raise ValueError("The legacy bridge report differs from the complete signed target.")
    boot_id = _fresh_boot(session, connector)
    events = _events(session, deployment)
    if state == "SUCCEEDED":
        proof = _proof(session, connector, deployment, release, boot_id, events)
        if (proof is None or deployment.status != "RECONCILING"
                or connector.ota_running_partition != proof["running_partition"]
                or connector.ota_image_sha256 != proof["image_sha256"]):
            raise ValueError("Signed current-boot capability proof is required before bridge completion.")
        record_progress(
            session, connector=connector, deployment_public_id=deployment_id,
            state=state, bytes_written=release.image_size, running_version=release.version,
            running_partition=proof["running_partition"], image_sha256=proof["image_sha256"],
        )
        return {"deployment_id": deployment_id, "state": deployment.status, "confirm": False}
    error = report.get("error_code")
    reports = _of_boot(events, boot_id, REPORT)
    if state == "BOOTED_PENDING" and error not in {"WAITING_FOR_RUNTIME_HEALTH", "RUNTIME_HEALTHY"}:
        raise ValueError("The legacy bridge must report its runtime-health handshake.")
    if state == "RECONCILING" and not any(
        event.details.get("error_code") == "RUNTIME_HEALTHY" for event in reports
    ):
        raise ValueError("The legacy bridge has not reported healthy runtime.")
    details = {"boot_id": boot_id, "reported_state": state, "error_code": error}
    if not reports or reports[0].details != details:
        session.add(FirmwareEvent(deployment_id=deployment.id, state=REPORT, details=details))
        session.flush()  # ADD sessions do not autoflush; the proof reads this report.
    proof = _proof(session, connector, deployment, release, boot_id, _events(session, deployment))
    return {"deployment_id": deployment_id, "state": deployment.status,
            "confirm": False, "awaiting_signed_capability": proof is None}


def verify_legacy_capability(session: Session, *, connector: Connector, capability: dict) -> None:
    active = _bridge(session, connector)
    # ADD answers this report 200 whether or not the bridge is active, and
    # 2.4.12 never repeats an accepted report in the same boot. So a matching
    # report is kept even while the campaign is paused or the release is not
    # AVAILABLE; only an active bridge builds the proof from it and moves the
    # deployment, on its next report after the bridge is active again.
    bridge = active or _bridge(session, connector, active=False)
    if bridge is None:
        return
    deployment, release = bridge
    if deployment.status != "READY_TO_BOOT" or not _capability_matches(release, capability):
        return
    boot_id = _fresh_boot(session, connector)
    details = {"boot_id": boot_id, "running_partition": capability["running_partition"],
               "image_sha256": capability["image_sha256"]}
    if not any(event.details == details for event in _of_boot(_events(session, deployment), boot_id, CAPABILITY)):
        session.add(FirmwareEvent(deployment_id=deployment.id, state=CAPABILITY, details=details))
        session.flush()  # ADD sessions do not autoflush; the proof reads this report.
    if active is not None:
        _proof(session, connector, deployment, release, boot_id, _events(session, deployment))

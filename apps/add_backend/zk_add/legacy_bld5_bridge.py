"""Bridge the original 2.4.12 protocol on the exact BLD5 and Lahore ZKTs.

Legacy boot reports are transport acknowledgements, not installation proof.
The ordinary checked transitions run only after a fresh signed capability
report identifies the published application and its OTA partition.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.hil_scope import target_matches
from zk_add.hil_2615_cities import CITY_FACTORY_PREDECESSORS, CITY_TARGETS, SIGNED_BRIDGE_IDENTITIES
from zk_add.models import Connector, DeviceTelemetry
from zk_add.ota import (
    FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease,
    HIL_2615_BLD5_TARGET, _application_sha256, _versions_match, record_progress,
)
from zk_add.time_utils import ensure_utc, utc_now


BRIDGE_IDENTITY = SIGNED_BRIDGE_IDENTITIES["2.4.12"]
BRIDGE_TARGETS = {"LF-ZONE-BLD5-01": HIL_2615_BLD5_TARGET, **{
    zone: CITY_TARGETS[zone] for zone, predecessor in CITY_FACTORY_PREDECESSORS.items()
    if predecessor[2] == "2.4.12"
}}


def _bridge(session: Session, connector: Connector, deployment_id: str | None = None):
    target = BRIDGE_TARGETS.get(connector.zone_id)
    if (connector.firmware_family != "zkt" or target is None
            or not target_matches(target, connector)):
        return None
    query = select(FirmwareDeployment).join(FirmwareCampaign).where(
        FirmwareDeployment.connector_id == connector.id,
        FirmwareDeployment.status.in_(["READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"]),
        FirmwareCampaign.status == "ACTIVE", FirmwareCampaign.zone_id == connector.zone_id,
        FirmwareCampaign.release_id == FirmwareDeployment.release_id,
    )
    if deployment_id is not None:
        query = query.where(FirmwareDeployment.deployment_id == deployment_id)
    deployment = session.scalar(query.order_by(FirmwareDeployment.id.desc()).limit(1).with_for_update())
    if deployment is None or not _versions_match(deployment.previous_version, "2.5.2"):
        return None
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or release.state != "AVAILABLE" or (
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


def _reports(session: Session, deployment: FirmwareDeployment, boot_id: str) -> list[FirmwareEvent]:
    return [event for event in session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["LEGACY_BOOT_REPORT", "LEGACY_BOOT_CAPABILITY_VERIFIED"]),
    ).order_by(FirmwareEvent.id.desc()).limit(30)) if (event.details or {}).get("boot_id") == boot_id
            and 0 <= (utc_now() - ensure_utc(event.created_at)).total_seconds() <= 180]


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
    reports = _reports(session, deployment, boot_id)
    if state == "SUCCEEDED":
        proof = next((event for event in reports if event.state == "LEGACY_BOOT_CAPABILITY_VERIFIED"), None)
        if (proof is None or deployment.status != "RECONCILING"
                or connector.ota_running_partition != proof.details["running_partition"]
                or connector.ota_image_sha256 != proof.details["image_sha256"]):
            raise ValueError("Signed current-boot capability proof is required before bridge completion.")
        record_progress(
            session, connector=connector, deployment_public_id=deployment_id,
            state=state, bytes_written=release.image_size, running_version=release.version,
            running_partition=proof.details["running_partition"],
            image_sha256=proof.details["image_sha256"],
        )
        return {"deployment_id": deployment_id, "state": deployment.status, "confirm": False}
    error = report.get("error_code")
    if state == "BOOTED_PENDING" and error not in {"WAITING_FOR_RUNTIME_HEALTH", "RUNTIME_HEALTHY"}:
        raise ValueError("The legacy bridge must report its runtime-health handshake.")
    if state == "RECONCILING" and not any(
        event.details.get("error_code") == "RUNTIME_HEALTHY" for event in reports
    ):
        raise ValueError("The legacy bridge has not reported healthy runtime.")
    details = {"boot_id": boot_id, "reported_state": state, "error_code": error}
    if not reports or reports[0].details != details:
        session.add(FirmwareEvent(deployment_id=deployment.id, state="LEGACY_BOOT_REPORT", details=details))
    return {"deployment_id": deployment_id, "state": deployment.status,
            "confirm": False, "awaiting_signed_capability": True}


def verify_legacy_capability(session: Session, *, connector: Connector, capability: dict) -> None:
    bridge = _bridge(session, connector)
    if bridge is None:
        return
    deployment, release = bridge
    if (deployment.status != "READY_TO_BOOT"
            or capability.get("running_version") != release.version
            or capability.get("running_partition") not in {"ota_0", "ota_1"}
            or capability.get("image_sha256") != _application_sha256(release)
            or any(capability.get(key) is not True for key in ("capable", "secure_boot", "rollback_enabled"))
            or capability.get("partition_layout") != release.partition_layout):
        return
    boot_id = _fresh_boot(session, connector)
    reports = _reports(session, deployment, boot_id)
    if not (any(event.details.get("error_code") == "RUNTIME_HEALTHY" for event in reports)
            and any(event.details.get("reported_state") == "RECONCILING" for event in reports)):
        return
    proof = {"boot_id": boot_id, "running_partition": capability["running_partition"],
             "image_sha256": capability["image_sha256"]}
    session.add(FirmwareEvent(deployment_id=deployment.id, state="LEGACY_BOOT_CAPABILITY_VERIFIED", details=proof))
    for state in ("BOOTED_PENDING", "RECONCILING"):
        record_progress(
            session, connector=connector, deployment_public_id=deployment.deployment_id,
            state=state, bytes_written=release.image_size, running_version=release.version,
            running_partition=capability["running_partition"], image_sha256=capability["image_sha256"],
        )

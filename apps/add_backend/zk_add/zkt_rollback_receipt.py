"""Evidence for a failed writer returning to its previously accepted bridge.

This acknowledges recovery only. Publication, installation and HIL qualification
remain subject to their separate gates, including the unreleased storage guard.
"""

from sqlalchemy import select

from zk_add.terminal_families import require_family_match
from zk_add.time_utils import ensure_utc
from zk_add.zkt_writer_contract import REQUIRED_BRIDGE_VERSION


def verify_failed_boot_return(
    session,
    connector,
    deployment,
    release,
    *,
    bytes_written,
    running_version,
    running_partition,
    image_sha256,
):
    from zk_add.ota import FirmwareDeployment, FirmwareEvent, FirmwareRelease, _application_sha256

    message = "Failed-boot recovery requires the exact previously accepted bridge."
    if (
        connector.firmware_family != "zkt"
        or release.version != "2.7.0"
        or deployment.target_version != "2.7.0"
        or deployment.previous_version != REQUIRED_BRIDGE_VERSION
        or running_version != REQUIRED_BRIDGE_VERSION
        or running_partition not in {"ota_0", "ota_1"}
        or bytes_written != release.image_size
        or deployment.bytes_written != release.image_size
    ):
        raise ValueError(message)
    require_family_match("zkt", release.manifest or {})
    latest = session.scalar(
        select(FirmwareDeployment.id)
        .where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc())
        .limit(1)
    )
    if latest != deployment.id:
        raise ValueError(message)
    # Inspect the immediately preceding attempt, not any convenient historical
    # success. Failed, cancelled or changed bridge attempts cannot be skipped.
    bridge = session.scalar(
        select(FirmwareDeployment)
        .where(
            FirmwareDeployment.connector_id == connector.id, FirmwareDeployment.id < deployment.id
        )
        .order_by(FirmwareDeployment.id.desc())
        .limit(1)
    )
    if (
        bridge is None
        or bridge.status != "SUCCEEDED"
        or bridge.target_version != REQUIRED_BRIDGE_VERSION
        or ensure_utc(bridge.created_at) >= ensure_utc(deployment.created_at)
    ):
        raise ValueError(message)
    reader = session.get(FirmwareRelease, bridge.release_id)
    if (
        reader is None
        or reader.version != REQUIRED_BRIDGE_VERSION
        or reader.state not in {"HIL_ONLY", "AVAILABLE"}
        or not _application_sha256(reader)
        or image_sha256 != _application_sha256(reader)
    ):
        raise ValueError(message)
    require_family_match("zkt", reader.manifest or {})
    event = session.scalar(
        select(FirmwareEvent)
        .where(FirmwareEvent.deployment_id == bridge.id, FirmwareEvent.state == "SUCCEEDED")
        .order_by(FirmwareEvent.id.desc())
        .limit(1)
    )
    details = (event.details or {}) if event else {}
    if (
        event is None
        or ensure_utc(event.created_at) >= ensure_utc(deployment.created_at)
        or details.get("image_sha256") != image_sha256
        or details.get("running_version") != REQUIRED_BRIDGE_VERSION
        or details.get("running_partition") != running_partition
        or details.get("bytes_written") != reader.image_size
        or bridge.bytes_written != reader.image_size
    ):
        raise ValueError(message)
    failure = None
    if deployment.status == "FAILED":
        failure = session.scalar(
            select(FirmwareEvent)
            .where(FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "FAILED")
            .order_by(FirmwareEvent.id.desc())
            .limit(1)
        )
        failed = (failure.details or {}) if failure else {}
        if (
            failure is None
            or failed.get("error_code") != "BOOT_HEALTH_TIMEOUT"
            or failed.get("running_version") != "2.7.0"
            or failed.get("image_sha256") != _application_sha256(release)
            or failed.get("bytes_written") != release.image_size
            or failed.get("running_partition") not in {"ota_0", "ota_1"}
            or failed.get("running_partition") == running_partition
        ):
            raise ValueError(message)
    return {
        "schema_version": 1,
        "bridge_deployment_id": bridge.deployment_id,
        "bridge_boot_event_id": event.id,
        "failure_event_id": failure.id if failure else None,
        "failure_report_received": failure is not None,
    }

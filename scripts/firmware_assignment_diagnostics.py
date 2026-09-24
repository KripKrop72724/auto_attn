#!/usr/bin/env python3
"""Read-only, bounded diagnosis of one firmware campaign's assignment gate."""

import argparse
import json
import re
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from zk_add.db import engine
from zk_add.hil_scope import target_matches
from zk_add.models import Connector, DeviceTelemetry
from zk_add.ota import (
    ACTIVE_DEPLOYMENT_STATES,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    _application_sha256,
    _ordered_hil_target,
    _storage_predecessor_exclusion,
    _validated_firmware_public_base,
    capability_is_eligible,
    require_family_match,
    version_at_least,
)
from zk_add.settings import settings


def diagnose(campaign_id: str) -> dict:
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=connection, autoflush=False) as session:
            campaign = session.scalar(select(FirmwareCampaign).where(
                FirmwareCampaign.campaign_id == campaign_id
            ))
            if campaign is None:
                return {"campaign_found": False}
            deployments = list(session.scalars(select(FirmwareDeployment).where(
                FirmwareDeployment.campaign_id == campaign.id
            ).order_by(FirmwareDeployment.id).limit(10)))
            release = session.get(FirmwareRelease, campaign.release_id)
            result = {
                "campaign_found": True,
                "campaign_status": campaign.status,
                "deployment_count": len(deployments),
                "release_version": release.version if release else None,
                "release_state": release.state if release else None,
                "hil_enabled": settings.firmware_hil_enabled,
            }
            if not release:
                return result
            try:
                _validated_firmware_public_base(settings.firmware_public_base_url)
                result["public_base_valid"] = True
            except ValueError:
                result["public_base_valid"] = False
            try:
                target = _ordered_hil_target(session, release) if release.state == "HIL_ONLY" else None
                result["ordered_target_valid"] = True
            except ValueError as error:
                target = None
                result["ordered_target_valid"] = False
                result["ordered_target_error"] = str(error)
            result["application_digest_present"] = _application_sha256(release) is not None
            summaries = []
            for deployment in deployments:
                connector = session.get(Connector, deployment.connector_id)
                if connector is None:
                    summaries.append({"connector_found": False})
                    continue
                try:
                    require_family_match(connector.firmware_family, release.manifest or {})
                    family_match = True
                except ValueError:
                    family_match = False
                other_active = session.scalar(select(FirmwareDeployment)
                    .join(FirmwareCampaign)
                    .where(FirmwareCampaign.zone_id == connector.zone_id,
                           FirmwareCampaign.status == "ACTIVE",
                           FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES))
                    .order_by(FirmwareDeployment.id))
                latest = session.scalar(select(DeviceTelemetry)
                    .where(DeviceTelemetry.connector_id == connector.id)
                    .order_by(DeviceTelemetry.id.desc()).limit(1))
                ota = ((latest.payload or {}).get("ota") or {}) if latest else {}
                ota_error = ota.get("last_error")
                if not isinstance(ota_error, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", ota_error):
                    ota_error = "OTHER_OR_UNAVAILABLE" if ota_error else None
                summaries.append({
                    "connector_name": connector.display_name,
                    "deployment_status": deployment.status,
                    "offer_attempts": deployment.attempt_count,
                    "bytes_written": deployment.bytes_written,
                    "connector_connected": connector.connected,
                    "last_seen_at": connector.last_seen_at.isoformat() if connector.last_seen_at else None,
                    "running_version": connector.firmware_version,
                    "ota_state": connector.ota_state,
                    "capability_eligible": capability_is_eligible(connector),
                    "family_match": family_match,
                    "minimum_version_met": version_at_least(
                        connector.firmware_version, release.minimum_bootstrap_version),
                    "predecessor_exclusion": _storage_predecessor_exclusion(session, release, connector),
                    "ordered_target_match": target_matches(target, connector) if target else None,
                    "other_active_deployment": bool(other_active and other_active.connector_id != connector.id),
                    "latest_telemetry_at": latest.created_at.isoformat() if latest else None,
                    "telemetry_ota_state": ota.get("state"),
                    "telemetry_ota_last_error": ota_error,
                    "telemetry_ota_capable": ota.get("capable"),
                })
            result["deployments"] = summaries
            return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-id", required=True)
    args = parser.parse_args()
    json.dump(diagnose(args.campaign_id), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")

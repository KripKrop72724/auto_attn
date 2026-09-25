#!/usr/bin/env python3
"""Read-only, bounded diagnosis of one firmware campaign's assignment gate."""

import argparse
from collections import Counter
import hashlib
import json
import re
import sys
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from zk_add.db import engine
from zk_add.hil_scope import target_matches
from zk_add.models import Connector, ConnectorNonce, DeviceTelemetry
from zk_add.ota import (
    ACTIVE_DEPLOYMENT_STATES,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    _application_sha256,
    _ordered_hil_target,
    _storage_predecessor_exclusion,
    _validated_firmware_public_base,
    _versions_match,
    capability_is_eligible,
    require_family_match,
    version_at_least,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now


def _worker_summary(payload: dict) -> list[dict]:
    diagnostics = payload.get("diagnostics") or {}
    summary = []
    for worker in diagnostics.get("workers") or []:
        if not isinstance(worker, dict) or worker.get("name") not in {
            "add_delivery", "ords_delivery", "hikvision_source",
        }:
            continue
        summary.append({
            "name": worker.get("name"),
            "state": worker.get("state") if worker.get("state") in {
                "STOPPED", "FAULT", "WAITING_RESOURCE", "WAITING_NETWORK", "RUNNING",
            } else "OTHER",
            "restart_attempts": worker.get("restart_attempts")
            if isinstance(worker.get("restart_attempts"), int) else None,
        })
    return summary


def _recent_worker_evidence(samples: list[DeviceTelemetry], version: str) -> dict:
    """Bounded, credential-free evidence for intermittent worker health alerts."""
    states: Counter[str] = Counter()
    anomalies = []
    matched = 0
    for sample in samples:
        payload = sample.payload or {}
        if not _versions_match(payload.get("firmware_version"), version):
            continue
        matched += 1
        diagnostics = payload.get("diagnostics") or {}
        memory = diagnostics.get("memory") or {}
        for worker in diagnostics.get("workers") or []:
            name = worker.get("name")
            if name not in {"add_delivery", "ords_delivery"}:
                continue
            state = worker.get("state")
            states[f"{name}:{state}"] += 1
            tick = worker.get("last_activity_uptime_ms")
            uptime = sample.uptime_seconds
            delta = uptime * 1000 - tick if isinstance(uptime, int) and isinstance(tick, int) else None
            if state in {"STOPPED", "FAULT", "WAITING_RESOURCE"} or delta is None or not -999 <= delta <= 90_000:
                if len(anomalies) < 30:
                    operation = worker.get("operation")
                    anomalies.append({
                        "at": sample.created_at.isoformat(),
                        "worker": name,
                        "state": state,
                        "operation": operation if operation in {
                            "idle", "reading queue", "waiting for acknowledgement",
                            "committing receipt", "allocating delivery buffer",
                        } else "OTHER_OR_UNAVAILABLE",
                        "tick_delta_ms": delta,
                        "internal_free_bytes": memory.get("internal_free_bytes"),
                        "internal_largest_block_bytes": memory.get("internal_largest_block_bytes"),
                        "led_state": payload.get("led_state"),
                    })
    return {"samples_examined": len(samples), "target_version_samples": matched,
            "worker_states": dict(states), "anomalies": anomalies}


def _health_summary(payload: dict) -> dict:
    """Expose only bounded firmware health fields needed to explain a HIL gate."""
    diagnostics = payload.get("diagnostics") or {}
    storage = diagnostics.get("storage") or {}
    memory = diagnostics.get("memory") or {}
    led = payload.get("led_state")
    operation = storage.get("error_operation")
    upgrade_error = storage.get("upgrade_error")
    failure_source = storage.get("local_failure_source")
    return {
        "led_state": led if led in {"HEALTHY", "LOCAL_FAILURE", "FATAL", "ZKT_FAILURE", "ORDS_FAILURE"} else "OTHER_OR_UNAVAILABLE",
        "storage_durability": storage.get("durability"),
        "storage_recovery_complete": storage.get("recovery_complete"),
        "storage_persistence_verified": storage.get("persistence_verified"),
        "storage_write_failures": storage.get("write_failures"),
        "storage_read_failures": storage.get("read_failures"),
        "storage_error_code": storage.get("error_code") if isinstance(storage.get("error_code"), int) else None,
        "storage_error_operation": operation if isinstance(operation, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", operation) else None,
        "storage_upgrade_error": upgrade_error if isinstance(upgrade_error, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", upgrade_error) else None,
        "storage_local_failure_source": failure_source if isinstance(failure_source, str) and re.fullmatch(r"[A-Za-z0-9_]+\.c:[0-9]{1,5}", failure_source) else None,
        "internal_free_bytes": memory.get("internal_free_bytes") if isinstance(memory.get("internal_free_bytes"), int) else None,
        "internal_largest_block_bytes": memory.get("internal_largest_block_bytes") if isinstance(memory.get("internal_largest_block_bytes"), int) else None,
    }


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
                target_sample = next((row for row in session.scalars(
                    select(DeviceTelemetry)
                    .where(DeviceTelemetry.connector_id == connector.id)
                    .order_by(DeviceTelemetry.id.desc()).limit(256)
                ) if _versions_match((row.payload or {}).get("firmware_version"), release.version)), None)
                recent_samples = list(session.scalars(select(DeviceTelemetry)
                    .where(DeviceTelemetry.connector_id == connector.id)
                    .order_by(DeviceTelemetry.id.desc()).limit(512)))
                recent_authenticated_requests = session.scalar(select(func.count(ConnectorNonce.id)).where(
                    ConnectorNonce.connector_id == connector.id,
                    ConnectorNonce.created_at >= utc_now() - timedelta(minutes=5),
                ))
                ota = ((latest.payload or {}).get("ota") or {}) if latest else {}
                diagnostics = ((latest.payload or {}).get("diagnostics") or {}) if latest else {}
                storage = diagnostics.get("storage") or {}
                target_diagnostics = ((target_sample.payload or {}).get("diagnostics") or {}) if target_sample else {}
                target_storage = target_diagnostics.get("storage") or {}
                clock_offset_seconds = None
                if latest:
                    clock_sample = (latest.payload or {}).get("_trusted_envelope_sent_at")
                    if isinstance(clock_sample, str):
                        try:
                            clock_offset_seconds = int((
                                ensure_utc(latest.created_at) - parse_datetime(clock_sample)
                            ).total_seconds())
                        except ValueError:
                            pass
                ota_error = ota.get("last_error")
                if not isinstance(ota_error, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", ota_error):
                    ota_error = "OTHER_OR_UNAVAILABLE" if ota_error else None
                ota_counters = {
                    key: ota.get(key) if isinstance(ota.get(key), int) and 0 <= ota[key] <= 2**32 - 1 else None
                    for key in ("boot_health_checks", "progress_attempts", "progress_successes")
                }
                ota_status = ota.get("progress_last_http_status")
                summaries.append({
                    "connector_name": connector.display_name,
                    "connector_fingerprint": hashlib.sha256(connector.connector_id.encode()[:120]).hexdigest()[:12],
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
                    "latest_telemetry_uptime_seconds": latest.uptime_seconds if latest else None,
                    "latest_free_heap_bytes": latest.free_heap if latest else None,
                    "latest_workers": _worker_summary(latest.payload or {}) if latest else [],
                    "recent_worker_evidence": _recent_worker_evidence(recent_samples, release.version),
                    "latest_health": _health_summary(latest.payload or {}) if latest else None,
                    "storage_upgrade_ready": storage.get("upgrade_ready"),
                    "storage_durability": storage.get("durability"),
                    "last_target_sample_at": target_sample.created_at.isoformat() if target_sample else None,
                    "last_target_free_heap_bytes": target_sample.free_heap if target_sample else None,
                    "last_target_workers": _worker_summary(target_sample.payload or {}) if target_sample else [],
                    "last_target_health": _health_summary(target_sample.payload or {}) if target_sample else None,
                    "last_target_storage_upgrade_ready": target_storage.get("upgrade_ready"),
                    "last_target_storage_durability": target_storage.get("durability"),
                    "device_clock_offset_seconds": clock_offset_seconds,
                    "authenticated_requests_last_5m": recent_authenticated_requests,
                    "telemetry_ota_state": ota.get("state"),
                    "telemetry_ota_last_error": ota_error,
                    "telemetry_ota_capable": ota.get("capable"),
                    "telemetry_ota_boot_health_checks": ota_counters["boot_health_checks"],
                    "telemetry_ota_boot_health_last_ready": ota.get("boot_health_last_ready")
                    if isinstance(ota.get("boot_health_last_ready"), bool) else None,
                    "telemetry_ota_progress_attempts": ota_counters["progress_attempts"],
                    "telemetry_ota_progress_successes": ota_counters["progress_successes"],
                    "telemetry_ota_progress_last_http_status": ota_status
                    if isinstance(ota_status, int) and -1 <= ota_status <= 599 else None,
                })
            result["deployments"] = summaries
            return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-id", required=True)
    args = parser.parse_args()
    json.dump(diagnose(args.campaign_id), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")

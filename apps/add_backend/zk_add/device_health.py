"""Derived device health: one evaluation from OPEN alerts and the terminal link.

lifecycle_state and last_error_code are the output of evaluate_health(). Every
OPEN alert maps through POLICY to a tier (DEGRADED, WARNING or none), a currency
(is the evidence from this boot, held, from an ended boot, or latched) and a
gating flag. Only gating reasons become last_error_code, which the HIL and
factory holds key on, so data codes and ESP_OFFLINE never create a hold.

Evaluation is read-only apart from a flush (sessions run with autoflush off).
ENFORCED mode (ADD_DEVICE_HEALTH_DERIVED_ENABLED) writes the result; SHADOW
mode leaves the legacy writers authoritative and only exposes it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.hil_startup import PHASES as HIL_STARTUP_PHASES, STORAGE_COUNTERS, STORAGE_ERRORS
from zk_add.models import Connector, DeviceAlert, ZKTDevice
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now

CURRENT, HELD, PREVIOUS_BOOT, LATCHED = "CURRENT", "HELD", "PREVIOUS_BOOT", "LATCHED"
DEGRADED, WARNING = "DEGRADED", "WARNING"
RULE = "RULE"  # the tier depends on currency, latch, details or the terminal link
TIER_RANK = {DEGRADED: 0, WARNING: 1, None: 2}

# Rejected custody or heartbeat messages mean evidence is being lost now.
CUSTODY_MESSAGE_TYPES = frozenset({
    "heartbeat", "attendance_batch", "queue_evidence", "zkt_observation_batch",
    "reconcile_source_manifest", "reconcile_chunk", "source_tail_chunk", "user_snapshot",
    "hikvision_observation", "hikvision_history_page",
})
# rejection.rejection_category: the device sent something ADD cannot accept.
# Every other category is a server-side processing failure.
DEVICE_REJECTION_CATEGORIES = frozenset({"SCHEMA_INVALID", "EVIDENCE_INVALID"})
STARTUP_PHASES = frozenset(HIL_STARTUP_PHASES - {"READY"}) | {"BRIDGE_VALIDATION", "AUTHORITY_HOLD"}
# Closed only by the workflow that owns them, never by an operator reason.
WORKFLOW_OWNED_CODES = frozenset({
    "ADMIN_REVOKE_OVERDUE", "ATTENDANCE_EVENT_QUARANTINED", "ATTENDANCE_REPAIR_NEEDS_ATTENTION",
    "ATTENDANCE_REPAIR_STALLED", "ORDS_DELIVERY_FAILED", "DUPLICATE_USER_CNIC",
    "ORACLE_RECEIPT_CONNECTOR_MISMATCH", "HISTORY_BACKFILL_BLOCKED",
})
DIAGNOSTICS_CODES = ("ESP_DURABILITY_FAULT", "ESP_DELIVERY_WORKER_FAULT")


@dataclass(frozen=True)
class Policy:
    priority: int
    gating: bool
    # DIAGNOSTICS, LED, POSITIVE (a heartbeat resolves it on positive evidence),
    # REJECTION, TRANSPORT or LATCHED (not re-evaluated by heartbeats).
    currency: str
    clear_condition: str
    tier: str | None = None  # DEGRADED, WARNING, RULE, or None for no health effect


POLICY = {
    # Device faults, in priority order.
    "QUARANTINED_DUPLICATE_SERIAL": Policy(
        5, True, "POSITIVE", "Clears when no other connector claims this terminal serial.", DEGRADED),
    "ESP_FATAL": Policy(10, True, "LED", "Clears when the ESP reports a non-fatal LED state.", DEGRADED),
    "ZKT_SERIAL_MISMATCH": Policy(
        20, True, "POSITIVE", "Clears when the terminal reports its assigned serial.", DEGRADED),
    "ESP_DURABILITY_FAULT": Policy(
        30, True, "DIAGNOSTICS",
        "Clears when firmware that reports storage diagnostics verifies healthy, persisted and "
        "recovered storage.", RULE),
    "ESP_PRESERVATION_UNVERIFIED": Policy(
        32, True, "LATCHED",
        "Clears when firmware that reports storage diagnostics verifies healthy, persisted and "
        "recovered storage.", WARNING),
    "ESP_RESTART_LOOP": Policy(35, True, "POSITIVE", "Clears once the ESP stays up for 30 minutes.", DEGRADED),
    "ESP_DELIVERY_WORKER_FAULT": Policy(
        40, True, "DIAGNOSTICS",
        "Clears when every required delivery worker reports running with a fresh tick.", RULE),
    "HEARTBEAT_STALE": Policy(45, False, "TRANSPORT", "Clears when ADD accepts the next heartbeat.", DEGRADED),
    "ESP_LOCAL_FAILURE": Policy(
        50, True, "LED", "Clears when the ESP reports a healthy LED state, or at the next reboot.", RULE),
    "HIK_CAPTURE_UNHEALTHY": Policy(
        55, True, "POSITIVE", "Clears when the Hikvision terminal is online and its last check succeeded.",
        RULE),
    "DEVICE_MESSAGE_REJECTED": Policy(
        60, True, "REJECTION", "Clears when ADD accepts the next message of each rejected type.", RULE),
    "ADD_MESSAGE_PROCESSING_FAILED": Policy(
        65, False, "REJECTION", "Clears when ADD processes the next message of each failed type.", RULE),
    "HIK_LIGHT_RECONCILE_BLOCKED": Policy(
        70, True, "POSITIVE", "Clears when the Hikvision history audit is scanning or complete.", WARNING),
    "OTA_DEVICE_ROLLED_BACK": Policy(
        75, True, "LATCHED", "Clears when a later deployment to this device succeeds.", WARNING),
    "OTA_DEVICE_REPORTED_FAILURE": Policy(
        75, True, "LATCHED", "Clears when a later deployment to this device succeeds.", WARNING),
    "ZKT_CONNECTION_FLAPPING": Policy(
        80, True, "POSITIVE", "Clears after three consecutive successful terminal connections.", WARNING),
    "TERMINAL_DISCONNECTED": Policy(85, False, "TRANSPORT", "Clears when the terminal link reconnects.", WARNING),
    "TERMINAL_STABILIZING_STALLED": Policy(
        86, False, "TRANSPORT", "Clears when the terminal link reports ONLINE.", WARNING),
    "DELIVERY_AUTHORITY_UNKNOWN": Policy(
        90, False, "POSITIVE", "Clears when the journal reports valid ADD delivery authority.", WARNING),
    # No health effect; listed under other_active_alerts.
    "ESP_OFFLINE": Policy(100, False, "TRANSPORT", "Clears when ADD accepts a heartbeat."),
    "TERMINAL_LINK_DOWN": Policy(
        101, False, "POSITIVE", "Clears when the terminal link is connected or stabilizing."),
    "ZKT_CLOCK_DRIFT": Policy(
        102, False, "POSITIVE", "Clears when the terminal clock is within two minutes of trusted time."),
    "HIK_CLOCK_DRIFT": Policy(
        102, False, "POSITIVE", "Clears when the terminal clock is within two minutes of trusted time."),
    "USER_SNAPSHOT_TRUNCATED": Policy(
        103, False, "LATCHED", "Clears when a complete, stable user snapshot is received."),
    "HISTORY_BACKFILL_BLOCKED": Policy(
        104, False, "POSITIVE", "Clears when history backfill is running or complete."),
    "ORDS_DELIVERY_FAILED": Policy(105, False, "LATCHED", "Clears when Oracle delivery succeeds."),
    "ORDS_EVENT_REJECTED": Policy(105, False, "LATCHED", "Clears through the Oracle rejection review."),
    "ATTENDANCE_EVENT_QUARANTINED": Policy(
        106, False, "LATCHED", "Clears when the quarantined attendance events are released."),
    "ATTENDANCE_TIMESTAMP_QUARANTINED": Policy(
        106, False, "LATCHED", "Clears through the attendance timestamp review."),
    "ATTENDANCE_REPAIR_NEEDS_ATTENTION": Policy(
        107, False, "LATCHED", "Clears when the attendance repair completes."),
    "ATTENDANCE_REPAIR_STALLED": Policy(
        107, False, "LATCHED", "Clears when the attendance repair completes."),
    "ADD_SOURCE_COVERAGE_INVALIDATED": Policy(
        108, False, "LATCHED", "Clears through a source coverage review."),
    "TERMINAL_SOURCE_EXCEPTION": Policy(108, False, "LATCHED", "Clears through a source exception review."),
    "USER_DELETION_JOB_INCOMPLETE": Policy(
        109, False, "LATCHED", "Clears when the user deletion job completes."),
    "DUPLICATE_USER_CNIC": Policy(109, False, "LATCHED", "Clears when the duplicate CNIC is resolved."),
    "ORACLE_RECEIPT_CONNECTOR_MISMATCH": Policy(
        109, False, "LATCHED", "Clears when Oracle receipts match this connector."),
    "ADMIN_REVOKE_OVERDUE": Policy(109, False, "LATCHED", "Clears when the temporary admin is revoked."),
}
UNKNOWN_POLICY = Policy(110, False, "LATCHED", "No automatic clear condition is known for this code.")
ZKT_LINK = {
    "ONLINE": "CONNECTED", "RECOVERING": "STABILIZING",
    "SESSION_REFRESH": "MAINTENANCE", "RESTARTING": "MAINTENANCE", "BOOTING": "STARTING",
    "SUSPECT": "RECONNECTING", "CONNECTING": "RECONNECTING", "DISCOVERING": "RECONNECTING",
    "RETRY_WAIT": "RECONNECTING", "OFFLINE": "RECONNECTING", "FLAPPING": "FLAPPING",
}
HIK_POLL_ERRORS = {
    1: "HIK_CONFIGURATION", 2: "HIK_NETWORK", 3: "HIK_AUTH", 4: "HIK_HTTP_STATUS",
    5: "HIK_OVERSIZED", 6: "HIK_SOURCE_CHANGED", 7: "HIK_INVALID_RESPONSE", 8: "HIK_STORAGE",
}


def mode() -> str:
    return "ENFORCED" if settings.device_health_derived_enabled else "SHADOW"


def _age(now: datetime, value: datetime | None) -> float | None:
    return None if value is None else (now - ensure_utc(value)).total_seconds()


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return parse_datetime(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def connector_fresh(connector: Connector, now: datetime) -> bool:
    age = _age(now, connector.last_seen_at)
    return bool(connector.connected) and age is not None and age <= settings.offline_after_seconds


def latched_led_sources() -> frozenset[str]:
    return frozenset(part.strip() for part in settings.device_health_latched_led_sources.split(",")
                     if part.strip())


def plain_version(value: str | None) -> str | None:
    """"zone-lite-2.6.15" and "2.6.15" are the same release; firmware reports the former."""
    from zk_add.ota import semantic_version  # ota is a heavy module; keep this import lazy

    version = semantic_version(value)
    return ".".join(str(part) for part in version) if version else (value or "").strip() or None


def diagnostics_reporting(connector: Connector) -> str:
    """Whether the running firmware can report storage and worker diagnostics."""
    from zk_add.ota import semantic_version  # ota is a heavy module; keep this import lazy

    version = semantic_version(connector.firmware_version)
    if (connector.firmware_family or "zkt") == "zkt" and version is not None and version <= (2, 5, 2):
        return "NOT_REPORTED_BY_FIRMWARE"
    return "REPORTED" if connector.firmware_diagnostics else "MISSING"


def _estimated_uptime(connector: Connector, now: datetime) -> float | None:
    diagnostics = connector.firmware_diagnostics or {}
    sampled = diagnostics.get("sampled_uptime_ms")
    if type(sampled) is not int or connector.firmware_diagnostics_at is None:
        return None
    return sampled / 1000 + (_age(now, connector.firmware_diagnostics_at) or 0)


def _epoch(value) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None  # a terminal-reported epoch never breaks the heartbeat


def _link(state, raw, since, reason, message) -> dict:
    return {"state": state, "raw_state": raw, "since": ensure_utc(since) if since else None,
            "reason": reason, "message": message}


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def terminal_link(connector: Connector, now: datetime | None = None, *,
                  uptime_seconds: float | None = None) -> dict:
    """The terminal's own status. It never makes the ESP DEGRADED (HIK_STORAGE aside)."""
    now = now or utc_now()
    zkt = connector.zkt_device
    if zkt is None:
        return _link("UNKNOWN", None, None, "NO_TERMINAL", "No terminal is bound to this connector.")
    raw = (zkt.connection_state or "UNKNOWN").upper()
    if not connector_fresh(connector, now):
        return _link("UNKNOWN", raw, connector.last_disconnect_at, "ESP_DISCONNECTED",
                     "The ESP is not connected, so the terminal link is unknown.")
    if (connector.firmware_family or "zkt") == "hikvision":
        if uptime_seconds is None:
            uptime_seconds = _estimated_uptime(connector, now)
        return _hikvision_link(zkt, raw, now, uptime_seconds)
    mapped = ZKT_LINK.get(raw, "UNKNOWN")
    transition_age = _age(now, zkt.last_transition_at)
    if mapped == "CONNECTED":
        return _link("CONNECTED", raw, zkt.last_transition_at, "LINK_UP", "The terminal link is up.")
    if mapped == "STABILIZING":
        if transition_age is not None and transition_age > settings.terminal_stabilizing_warning_seconds:
            return _link("STABILIZING", raw, zkt.last_transition_at, "STABILIZING_STALLED",
                         f"The terminal link has been stabilizing for {_minutes(transition_age)}.")
        return _link("STABILIZING", raw, zkt.last_transition_at, "STABILIZING",
                     "The terminal link is back and proving it is stable.")
    if mapped == "MAINTENANCE":
        if transition_age is None or transition_age <= settings.terminal_maintenance_budget_seconds:
            return _link("MAINTENANCE", raw, zkt.last_transition_at, raw,
                         "The terminal session is in a planned refresh or protocol restart.")
        return _link("DISCONNECTED", raw, zkt.last_transition_at, "MAINTENANCE_OVERRAN",
                     f"A planned terminal session {raw.lower().replace('_', ' ')} has lasted "
                     f"{_minutes(transition_age)}.")
    if mapped in {"STARTING", "RECONNECTING"}:
        since = zkt.offline_since or zkt.last_transition_at
        age = _age(now, since)
        if age is not None and age > settings.terminal_disconnected_warning_seconds:
            return _link("DISCONNECTED", raw, since, raw,
                         f"The terminal has been unreachable for {_minutes(age)} ({raw}).")
        message = ("The connector is starting its terminal session." if mapped == "STARTING"
                   else f"The connector is reconnecting to the terminal ({raw}).")
        return _link(mapped, raw, since, raw, message)
    if mapped == "FLAPPING":
        return _link("FLAPPING", raw, zkt.last_transition_at, "FLAPPING",
                     "The terminal link is unstable; the connector is in protective backoff.")
    return _link("UNKNOWN", raw, zkt.last_transition_at, raw, "The terminal link state was not reported.")


def _hikvision_link(zkt: ZKTDevice, raw: str, now: datetime, uptime_seconds: float | None) -> dict:
    health = (zkt.capability_profile or {}).get("hikvision_health") or {}
    try:
        error = int(health.get("poll_error") or 0)
    except (TypeError, ValueError):
        error = 0
    if error not in {0, 2, 8}:
        reason = HIK_POLL_ERRORS.get(error, "HIK_POLL_ERROR")
        return _link("ERROR", raw, zkt.last_transition_at, reason,
                     f"The Hikvision terminal check is failing ({reason}).")
    if raw == "ONLINE" and error != 2:
        return _link("CONNECTED", raw, zkt.last_transition_at, "LINK_UP", "The terminal link is up.")
    since = zkt.offline_since or _epoch(health.get("last_successful_poll_epoch")) or zkt.last_transition_at
    age = _age(now, since)
    reason = "HIK_NETWORK" if error == 2 else raw
    if age is not None and age > settings.terminal_disconnected_warning_seconds:
        return _link("DISCONNECTED", raw, since, reason,
                     f"The Hikvision terminal has been unreachable for {_minutes(age)}.")
    if uptime_seconds is not None and uptime_seconds < settings.terminal_disconnected_warning_seconds:
        return _link("STARTING", raw, since, "HIK_WAITING",
                     "The connector is waiting for its first successful Hikvision check.")
    return _link("RECONNECTING", raw, since, reason,
                 "The connector is reconnecting to the Hikvision terminal.")


def storage_recovery_role(session: Session, connector: Connector, payload) -> bool:
    """A registered one-shot storage recovery image running on its reviewed target.

    Those images start no delivery workers by design. All three must hold: the
    version, the connector, and the exact application digest of the HIL_ONLY
    release; a device's own report can never waive required workers.
    """
    from zk_add import storage_recovery
    from zk_add.ota import FirmwareRelease, _application_sha256

    version = plain_version(payload.firmware_version)
    if version not in storage_recovery.VERSIONS or not any(
            target.connector_id == connector.connector_id and target.mac == (connector.hardware_id or "").lower()
            for target in storage_recovery.TARGETS):
        return False
    release = session.scalar(select(FirmwareRelease).where(
        FirmwareRelease.release_id == storage_recovery.RELEASE_IDS[version]))
    digest = _application_sha256(release) if release is not None and release.state == "HIL_ONLY" else None
    return digest is not None and payload.ota.image_sha256 == digest


def diagnostics_starting(evidence: dict, uptime_seconds: int | None) -> bool:
    """A journal image inside its startup grace, still choosing its delivery authority."""
    if uptime_seconds is None or uptime_seconds >= settings.journal_startup_grace_seconds:
        return False
    runtime = evidence.get("journal_runtime")
    phase = runtime.get("phase") if isinstance(runtime, dict) else None
    return phase in STARTUP_PHASES or (
        evidence.get("runtime_profile") == "ZKT_JOURNAL_V1" and evidence.get("delivery_authority") == "UNKNOWN")


def soft_owner_not_ready(evidence: dict) -> bool:
    journal = evidence.get("journal_storage")
    return isinstance(journal, dict) and (
        journal.get("ready") is False or journal.get("checkpoint_recovery_pending") is True)


def _reported(value) -> bool:
    return value not in (None, "", 0, False)


def hard_storage_evidence(evidence: dict) -> bool:
    """Storage faults that raise at once, even while the journal owner starts."""
    storage = evidence.get("storage") if isinstance(evidence.get("storage"), dict) else {}
    journal = evidence.get("journal_storage") if isinstance(evidence.get("journal_storage"), dict) else {}
    return (any(_reported(storage.get(name)) for name in STORAGE_ERRORS)
            or any(type(storage.get(name)) is int and storage[name] > 0 for name in STORAGE_COUNTERS)
            or journal.get("last_append_result") not in {None, "OK", "EMPTY"}
            or any(_reported(journal.get(name))
                   for name in ("last_failure_operation", "last_filesystem_error", "last_nvs_error"))
            or "FULL" in {storage.get("durability"), journal.get("durability")})


STORAGE_EVIDENCE_FIELDS = (
    "durability", "persistence_verified", "recovery_complete", "error_operation", "error_code",
    "write_failures", "read_failures", "persistence_probe_failures", "persistence_probe_error",
    "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults", "legacy_error_code",
    "legacy_error_operation", "local_failure_source", "fault_class", "upgrade_ready", "upgrade_error",
)
JOURNAL_EVIDENCE_FIELDS = ("ready", "durability", "last_append_result", "last_failure_operation",
                           "last_filesystem_error")


def _storage_summary(storage: dict, journal: dict, phase) -> str:
    parts = [f"storage {storage.get('durability') or 'UNKNOWN'}"]
    for name, label in (("write_failures", "write failures"), ("read_failures", "read failures"),
                        ("persistence_probe_failures", "probe failures")):
        if type(storage.get(name)) is int and storage[name]:
            parts.append(f"{label} {storage[name]}")
    if storage.get("error_operation") or _reported(storage.get("error_code")):
        parts.append(f"{storage.get('error_operation') or 'operation'} error {storage.get('error_code')}")
    if storage.get("legacy_error_operation") or _reported(storage.get("legacy_error_code")):
        parts.append(f"{storage.get('legacy_error_operation') or 'legacy'} error {storage.get('legacy_error_code')}")
    for flag, label in (("persistence_verified", "persistence not verified"),
                        ("recovery_complete", "recovery incomplete")):
        if storage and storage.get(flag) is False:
            parts.append(label)
    if storage.get("local_failure_source"):
        parts.append(f"LED source {storage['local_failure_source']}")
    if journal:
        parts.append(f"journal {journal.get('durability') or 'UNKNOWN'}"
                     + (f", last append {journal['last_append_result']}" if journal.get("last_append_result") else "")
                     + ("" if journal.get("ready") is not False else ", owner not ready"))
    if phase:
        parts.append(f"phase {phase}")
    if storage.get("used_percent") is not None:
        parts.append(f"{storage['used_percent']}% used")
    return "; ".join(parts)


def diagnostics_evidence(code: str, evidence: dict, uptime_seconds: int | None,
                         failing_workers: list[tuple[dict, str]]) -> dict:
    """A bounded record of the sample that raised or refreshed a diagnostics alert."""
    raw_storage = evidence.get("storage") if isinstance(evidence.get("storage"), dict) else {}
    raw_journal = evidence.get("journal_storage") if isinstance(evidence.get("journal_storage"), dict) else {}
    runtime = evidence.get("journal_runtime") if isinstance(evidence.get("journal_runtime"), dict) else {}
    phase = runtime.get("phase")
    record: dict = {}
    if code == "ESP_DURABILITY_FAULT":
        storage = {name: raw_storage[name] for name in STORAGE_EVIDENCE_FIELDS
                   if raw_storage.get(name) is not None}
        used, total = raw_storage.get("used_bytes"), raw_storage.get("total_bytes")
        if type(used) is int and type(total) is int and total:
            storage["used_percent"] = round(100 * used / total, 1)
        journal = {name: raw_journal[name] for name in JOURNAL_EVIDENCE_FIELDS if raw_journal.get(name) is not None}
        record = {"storage": storage, **({"journal": {**journal, "phase": phase}} if journal or phase else {})}
        summary = _storage_summary(storage, journal, phase)
    else:
        now_ms = uptime_seconds * 1000 if uptime_seconds is not None else None
        failing_names = {row.get("name") for row, _why in failing_workers}
        workers = []
        for row in [row for row, _why in failing_workers] + [
                row for row in evidence.get("workers") or []
                if isinstance(row, dict) and row.get("name") not in failing_names]:
            tick = row.get("last_activity_uptime_ms")
            workers.append({key: value for key, value in {
                "name": row.get("name"), "state": row.get("state"), "operation": row.get("operation"),
                "tick_age_ms": now_ms - tick if now_ms is not None and type(tick) is int else None,
                "restart_count": row.get("restart_count")}.items() if value is not None})
        record = {"workers": workers[:8], **({"journal": {"phase": phase}} if phase else {})}
        summary = "; ".join(f"{row.get('name')} {why}" for row, why in failing_workers[:4]) or "no failing worker"
    return {"summary": summary[:300], **record}


def classify_led_latch(led_state: str | None, evidence: dict | None, *, storage_verified: bool,
                       workers_verified: bool, firmware_version: str | None) -> dict | None:
    """Explain a LOCAL_FAILURE LED that firmware latches until reboot.

    Signed 2.6.15 forces durability DEGRADED whenever the LED latched, so a
    sample with zero I/O counters and verified persistence shows a latch with
    no storage error behind it. The alert stays OPEN, HIGH and gating.
    """
    if (led_state or "").strip().upper() != "LOCAL_FAILURE" or not isinstance(evidence, dict):
        return None
    storage = evidence.get("storage") if isinstance(evidence.get("storage"), dict) else {}
    latch = {"source": storage.get("local_failure_source"), "firmware_version": plain_version(firmware_version)}
    if storage_verified and workers_verified:
        return {"kind": "LED_LATCH_STORAGE_VERIFIED", **latch}
    if (storage.get("durability") == "DEGRADED" and latch["source"]
            and not _reported(storage.get("error_code")) and not _reported(storage.get("persistence_probe_error"))
            and storage.get("write_failures") == 0 and storage.get("read_failures") == 0
            and not any(_reported(storage.get(name)) for name in (
                "persistence_probe_failures", "persistence_probe_total_failures", "legacy_read_faults",
                "legacy_append_faults", "legacy_retire_faults", "legacy_error_code"))
            and storage.get("persistence_verified") is True and storage.get("recovery_complete") is True
            and workers_verified):
        return {"kind": "LED_LATCH_NO_IO_ERRORS", **latch}
    return None


def bind_unbound_row(row: DeviceAlert | None, connector: Connector, *, uptime_seconds: int | None,
                     now: datetime) -> None:
    """Attribute a legacy diagnostics alert to a boot without moving last_seen_at."""
    details = (row.details or {}) if row is not None else {}
    if row is None or uptime_seconds is None or details.get("binding") or row.last_seen_at is None:
        return
    boot_started = now - timedelta(seconds=uptime_seconds)
    if ensure_utc(row.last_seen_at) < boot_started + timedelta(seconds=5):
        row.details = {**details, "binding": "INFERRED_PREVIOUS", "bound_at": now.isoformat()}
    else:
        row.details = {**details, "binding": "INFERRED_CURRENT", "boot_id": connector.boot_id,
                       "firmware_version": connector.firmware_version, "bound_at": now.isoformat()}


def _rejection_entries(row: DeviceAlert) -> dict[str, dict]:
    details = row.details or {}
    types = details.get("types")
    if isinstance(types, dict) and types:
        return {str(name): entry for name, entry in types.items() if isinstance(entry, dict)}
    # Rows written before per-type tracking carry one top-level message type.
    return {str(details.get("message_type") or "unknown"): {
        "category": details.get("error_category"), "count": 1,
        "first_at": row.first_seen_at, "last_at": row.last_seen_at}}


def _entry_current(entry: dict, now: datetime) -> bool:
    age = _age(now, _when(entry.get("last_at")))
    return age is not None and age <= settings.message_rejection_current_seconds


def _refreshed_by_last_heartbeat(row: DeviceAlert, connector: Connector) -> bool:
    # A heartbeat stamps its marker before it refreshes any alert.
    zkt = connector.zkt_device
    marker = (zkt.last_seen_at if zkt is not None and zkt.last_seen_at else None) or connector.last_seen_at
    return marker is None or row.last_seen_at is None or ensure_utc(row.last_seen_at) >= ensure_utc(marker)


def alert_currency(row: DeviceAlert, connector: Connector, *, now: datetime | None = None) -> str:
    """CURRENT, HELD, PREVIOUS_BOOT or LATCHED for one OPEN alert."""
    now = now or utc_now()
    policy = POLICY.get(row.code, UNKNOWN_POLICY)
    details = row.details or {}
    if policy.currency == "DIAGNOSTICS":
        binding = details.get("binding")
        if binding is None:
            return CURRENT  # unbound rows stay current until evidence binds them
        if binding == "INFERRED_PREVIOUS" or details.get("boot_id") != connector.boot_id:
            return PREVIOUS_BOOT
        return CURRENT if _refreshed_by_last_heartbeat(row, connector) else HELD
    if policy.currency == "LED":
        return HELD if details.get("led_clear_since") else CURRENT
    if policy.currency == "POSITIVE":
        return CURRENT
    if policy.currency == "REJECTION":
        return CURRENT if any(_entry_current(entry, now) for entry in _rejection_entries(row).values()) \
            else LATCHED
    if policy.currency == "TRANSPORT":
        return LATCHED if connector_fresh(connector, now) else CURRENT
    return LATCHED


def _latched_led_tier(details: dict) -> str | None:
    latch = details.get("latch") or {}
    if latch.get("kind") != "LED_LATCH_NO_IO_ERRORS":
        return None
    if f"{plain_version(latch.get('firmware_version'))}:{latch.get('source')}" not in latched_led_sources():
        return DEGRADED
    return settings.device_health_latched_led_tier


def _rule_tier(row: DeviceAlert, connector: Connector, currency: str, *, now: datetime,
               link: dict) -> str | None:
    details = row.details or {}
    if row.code == "ESP_DURABILITY_FAULT":
        if currency == PREVIOUS_BOOT:
            # Firmware that cannot report storage can never verify it, so the
            # fault stays a gating warning; capable firmware keeps it DEGRADED
            # until one of its boots verifies or fails storage.
            return WARNING if diagnostics_reporting(connector) == "NOT_REPORTED_BY_FIRMWARE" else DEGRADED
        return _latched_led_tier(details) or DEGRADED
    if row.code == "ESP_DELIVERY_WORKER_FAULT":
        return WARNING if currency == PREVIOUS_BOOT else DEGRADED
    if row.code == "ESP_LOCAL_FAILURE":
        if (details.get("latch") or {}).get("kind") == "LED_LATCH_STORAGE_VERIFIED":
            return WARNING
        return _latched_led_tier(details) or (WARNING if currency == HELD else DEGRADED)
    if row.code == "HIK_CAPTURE_UNHEALTHY":
        reason = details.get("reason")
        if reason == "HIK_STORAGE":
            return DEGRADED
        if reason in {None, "HIK_NETWORK", "HIK_WAITING"}:
            return WARNING if link["state"] == "DISCONNECTED" else None
        return WARNING
    entries = _rejection_entries(row)
    if row.code == "DEVICE_MESSAGE_REJECTED":
        if any(name in CUSTODY_MESSAGE_TYPES and _entry_current(entry, now) for name, entry in entries.items()):
            return DEGRADED
        return WARNING
    if row.code == "ADD_MESSAGE_PROCESSING_FAILED":
        heartbeat = entries.get("heartbeat")
        if heartbeat and _entry_current(heartbeat, now):
            persisted = _age(now, _when(heartbeat.get("first_at")))
            if persisted is not None and persisted >= settings.heartbeat_failure_degraded_seconds:
                return DEGRADED
        for entry in entries.values():
            first, last = _when(entry.get("first_at")), _when(entry.get("last_at"))
            if (int(entry.get("count") or 0) >= settings.message_failure_warning_count and first and last
                    and (last - first).total_seconds() >= settings.message_failure_warning_seconds):
                return WARNING
        return None
    return DEGRADED  # a RULE code without a rule fails closed


def _error_code(row: DeviceAlert) -> str:
    details = row.details or {}
    if row.code == "HIK_CAPTURE_UNHEALTHY":
        return str(details.get("reason") or row.code)[:120]
    if row.code == "OTA_DEVICE_ROLLED_BACK":
        return "OTA_PREVIOUS_FIRMWARE_OBSERVED"
    if row.code == "OTA_DEVICE_REPORTED_FAILURE" and details.get("error_code"):
        return f"OTA_{details['error_code']}"[:120]
    return row.code


def _evidence_summary(row: DeviceAlert) -> str | None:
    details = row.details or {}
    evidence = details.get("evidence")
    summary = evidence.get("summary") if isinstance(evidence, dict) else None
    latch = details.get("latch")
    if not summary and isinstance(latch, dict) and latch.get("kind"):
        summary = (f"{latch['kind']}: latched by {latch.get('source') or 'an unreported source'} on firmware "
                   f"{latch.get('firmware_version') or 'unknown'}.")
    if not summary and row.code in {"DEVICE_MESSAGE_REJECTED", "ADD_MESSAGE_PROCESSING_FAILED"}:
        summary = "; ".join(
            f"{name} x{int(entry.get('count') or 1)} ({entry.get('category') or 'UNKNOWN'})"
            for name, entry in sorted(_rejection_entries(row).items()))
    if not summary and row.code == "HIK_CAPTURE_UNHEALTHY" and details.get("reason"):
        summary = f"{details['reason']} (poll error {details.get('poll_error')})."
    if not summary and row.code.startswith("OTA_DEVICE_") and details.get("error_code"):
        summary = f"OTA error {details['error_code']} while {details.get('runtime_state') or 'updating'}."
    return str(summary)[:300] if summary else None


@dataclass
class Reason:
    code: str
    error_code: str | None
    tier: str | None
    currency: str
    gating: bool
    severity: str | None
    message: str
    since: datetime | None
    last_seen_at: datetime | None
    clear_condition: str
    priority: int
    alert: DeviceAlert | None = None
    evidence_summary: str | None = None
    operator: dict = field(default_factory=dict)

    @property
    def alert_id(self) -> int | None:
        return self.alert.id if self.alert is not None else None


@dataclass
class Health:
    mode: str
    source: str
    evaluated_at: datetime
    tier: str
    lifecycle_state: str
    derived_lifecycle: str
    last_error_code: str | None
    last_error_message: str | None
    reasons: list[Reason]
    other_active_alerts: list[Reason]
    terminal_link: dict

    @property
    def primary(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None


def _sort_key(reason: Reason):
    since = reason.since.timestamp() if reason.since else float("inf")
    return (reason.code != "QUARANTINED_DUPLICATE_SERIAL", TIER_RANK[reason.tier], reason.priority,
            since, reason.alert_id or 0)


def operator_policy(reason: Reason, *, duplicate_claimed: bool | None = None) -> dict:
    """Whether a person may resolve this reason with a recorded reason."""
    def refuse(code, text):
        return {"resolvable": False, "refusal_code": code, "refusal": text}

    if reason.alert is None:
        return refuse("DERIVED_REASON", "Derived from live state; it clears on its own.")
    if reason.code in WORKFLOW_OWNED_CODES:
        return refuse("ALERT_WORKFLOW_OWNED", "Its own review workflow resolves this alert.")
    if reason.code not in POLICY:
        return refuse("ALERT_WORKFLOW_OWNED", "This alert code has no reviewed operator resolution.")
    if reason.code == "ESP_PRESERVATION_UNVERIFIED":
        return refuse("ALERT_CONDITION_CURRENT",
                      "Preservation stays unverified until firmware that reports storage verifies it.")
    if reason.code == "QUARANTINED_DUPLICATE_SERIAL":
        if duplicate_claimed is False:
            return {"resolvable": True, "refusal_code": None, "refusal": None}
        return refuse("ALERT_CONDITION_CURRENT", "Another connector may still claim this terminal serial.")
    if reason.currency == CURRENT:
        return refuse("ALERT_CONDITION_CURRENT", "The latest evidence still asserts this condition.")
    if reason.currency == HELD and reason.code in DIAGNOSTICS_CODES:
        return refuse("ALERT_CONDITION_CURRENT",
                      "Held on this boot until the device reports verified evidence.")
    return {"resolvable": True, "refusal_code": None, "refusal": None}


def alert_reason(row: DeviceAlert, connector: Connector, *, now: datetime, link: dict) -> Reason:
    policy = POLICY.get(row.code, UNKNOWN_POLICY)
    currency = alert_currency(row, connector, now=now)
    tier = _rule_tier(row, connector, currency, now=now, link=link) if policy.tier == RULE else policy.tier
    details = row.details or {}
    reason = Reason(
        code=row.code, error_code=_error_code(row), tier=tier, currency=currency, gating=policy.gating,
        severity=row.severity, message=row.message,
        since=_when(details.get("boot_first_seen_at")) or ensure_utc(row.first_seen_at),
        last_seen_at=ensure_utc(row.last_seen_at), clear_condition=policy.clear_condition,
        priority=policy.priority, alert=row, evidence_summary=_evidence_summary(row))
    reason.operator = operator_policy(reason)
    return reason


def _derived(code: str, *, message: str, since: datetime | None) -> Reason:
    policy = POLICY[code]
    reason = Reason(
        code=code, error_code=None, tier=policy.tier, currency=CURRENT, gating=policy.gating,
        severity=None, message=message, since=ensure_utc(since) if since else None, last_seen_at=None,
        clear_condition=policy.clear_condition, priority=policy.priority)
    reason.operator = operator_policy(reason)
    return reason


def derived_reasons(connector: Connector, *, now: datetime, link: dict) -> list[Reason]:
    if not connector_fresh(connector, now):
        return []
    reasons = []
    zkt = connector.zkt_device
    stale = _age(now, zkt.last_seen_at) if zkt is not None else None
    if stale is not None and stale > settings.heartbeat_stale_degraded_seconds:
        reasons.append(_derived(
            "HEARTBEAT_STALE", since=zkt.last_seen_at,
            message=f"The ESP is connected, but ADD has not accepted a heartbeat for {int(stale)} s."))
    if link["state"] == "DISCONNECTED":
        reasons.append(_derived("TERMINAL_DISCONNECTED", message=link["message"], since=link["since"]))
    elif link["reason"] == "STABILIZING_STALLED":
        reasons.append(_derived("TERMINAL_STABILIZING_STALLED", message=link["message"], since=link["since"]))
    return reasons


def derive_health(connector: Connector, alerts, *, now: datetime, source: str = "READ",
                  uptime_seconds: float | None = None) -> Health:
    """Pure derivation from a connector, its OPEN alerts and the clock."""
    link = terminal_link(connector, now, uptime_seconds=uptime_seconds)
    reasons, others = [], []
    for row in alerts:
        reason = alert_reason(row, connector, now=now, link=link)
        (reasons if reason.tier else others).append(reason)
    reasons.extend(derived_reasons(connector, now=now, link=link))
    reasons.sort(key=_sort_key)
    others.sort(key=_sort_key)
    stored = connector.lifecycle_state
    held_offline = source != "HEARTBEAT" and stored == "OFFLINE"
    if held_offline or not connector_fresh(connector, now):
        tier = "OFFLINE"  # only the sweep sets OFFLINE and only a heartbeat clears it
    elif any(reason.tier == DEGRADED for reason in reasons):
        tier = "DEGRADED"
    elif reasons:
        tier = "ONLINE_WITH_WARNINGS"
    else:
        tier = "ONLINE"
    if any(reason.code == "QUARANTINED_DUPLICATE_SERIAL" for reason in reasons):
        lifecycle = "QUARANTINED_DUPLICATE_SERIAL"
    elif source != "HEARTBEAT" and stored == "ONBOARDING":
        lifecycle = stored
    else:
        lifecycle = tier
    error = next((reason for reason in reasons if reason.gating), None)
    return Health(
        mode=mode(), source=source, evaluated_at=now, tier=tier, lifecycle_state=stored,
        derived_lifecycle=lifecycle, last_error_code=error.error_code if error else None,
        last_error_message=error.message if error else None, reasons=reasons,
        other_active_alerts=others, terminal_link=link)


def _open_alerts(session: Session, connector_ids, exclude_alert_ids=()):
    statement = select(DeviceAlert).where(DeviceAlert.connector_id.in_(list(connector_ids)),
                                          DeviceAlert.state == "OPEN")
    if exclude_alert_ids:
        statement = statement.where(DeviceAlert.id.not_in(list(exclude_alert_ids)))
    return session.scalars(statement.order_by(DeviceAlert.id)).all()


def evaluate_health(session: Session, connector: Connector, *, now: datetime | None = None,
                    source: str = "READ", exclude_alert_ids=(), uptime_seconds: float | None = None) -> Health:
    """The single derivation; its only side effect is a flush of pending alert writes."""
    session.flush()
    return derive_health(connector, _open_alerts(session, [connector.id], exclude_alert_ids),
                         now=now or utc_now(), source=source, uptime_seconds=uptime_seconds)


def evaluate_health_batch(session: Session, connectors, *, now: datetime | None = None,
                          source: str = "READ") -> dict[int, Health]:
    """Health for a page of connectors with one OPEN-alert query."""
    now = now or utc_now()
    connectors = list(connectors)
    grouped: dict[int, list[DeviceAlert]] = {connector.id: [] for connector in connectors}
    if connectors:
        session.flush()
        for row in _open_alerts(session, grouped):
            grouped[row.connector_id].append(row)
    return {connector.id: derive_health(connector, grouped[connector.id], now=now, source=source)
            for connector in connectors}


def apply_device_health(session: Session, connector: Connector, *, source: str, now: datetime | None = None,
                        uptime_seconds: float | None = None) -> Health:
    """Evaluate, and in ENFORCED mode write lifecycle and the device error when they differ."""
    health = evaluate_health(session, connector, now=now, source=source, uptime_seconds=uptime_seconds)
    if health.mode == "ENFORCED":
        if connector.lifecycle_state != health.derived_lifecycle:
            connector.lifecycle_state = health.derived_lifecycle
        if (connector.last_error_code, connector.last_error_message) != (
                health.last_error_code, health.last_error_message):
            connector.last_error_code = health.last_error_code
            connector.last_error_message = health.last_error_message
    return health


def health_telemetry(health: Health, connector: Connector) -> dict:
    """The per-heartbeat record that lets every DEGRADED or WARNING minute be attributed later."""
    return {
        "v": 1, "mode": health.mode, "lifecycle": connector.lifecycle_state,
        "derived_lifecycle": health.derived_lifecycle, "tier": health.tier,
        "reasons": [f"{reason.code}:{reason.tier}:{reason.currency}" for reason in health.reasons[:12]],
        "terminal": health.terminal_link["state"],
    }


def duplicate_serial_claimed(session: Session, connector: Connector) -> bool:
    zkt = connector.zkt_device
    if zkt is None or not zkt.serial:
        return False
    return session.scalar(select(ZKTDevice.id).where(
        ZKTDevice.serial == zkt.serial, ZKTDevice.id != zkt.id).limit(1)) is not None


def coverage(connector: Connector) -> list[dict]:
    """What ADD can verify about this connector from the firmware it runs."""
    reporting = diagnostics_reporting(connector)
    version = plain_version(connector.firmware_version) or "unknown"
    diagnostics = connector.firmware_diagnostics or {}
    storage = diagnostics.get("storage") if isinstance(diagnostics.get("storage"), dict) else {}
    workers = [row for row in diagnostics.get("workers") or [] if isinstance(row, dict)]

    def item(key, label, status, detail):
        return {"key": key, "label": label, "status": status, "detail": detail}

    if reporting == "NOT_REPORTED_BY_FIRMWARE":
        missing = f"Not reported by firmware {version}."
        rows = [item("storage", "Storage durability", reporting, missing),
                item("workers", "Delivery workers", reporting, missing),
                item("preservation", "Upgrade preservation", reporting, missing)]
    elif reporting == "MISSING":
        missing = "The latest heartbeat carried no diagnostics."
        rows = [item("storage", "Storage durability", reporting, missing),
                item("workers", "Delivery workers", reporting, missing),
                item("preservation", "Upgrade preservation", reporting, missing)]
    else:
        used, total = storage.get("used_bytes"), storage.get("total_bytes")
        usage = f"; {round(100 * used / total, 1)}% used" if type(used) is int and type(total) is int and total \
            else ""
        rows = [
            item("storage", "Storage durability", reporting,
                 f"{storage.get('durability') or 'UNKNOWN'}; persistence "
                 f"{'verified' if storage.get('persistence_verified') else 'not verified'}; recovery "
                 f"{'complete' if storage.get('recovery_complete') else 'incomplete'}{usage}."),
            item("workers", "Delivery workers", reporting,
                 ", ".join(f"{row.get('name')} {row.get('state')}" for row in workers[:8])
                 or "No workers reported."),
            item("preservation", "Upgrade preservation", reporting,
                 "Verified." if storage.get("upgrade_ready") is True else
                 "Not verified; the HIL scheduler keeps its preservation hold." if storage.get("upgrade_ready")
                 is False else "Not reported."),
        ]
    if (connector.firmware_family or "zkt") == "zkt":
        rows.append(item("led", "LED fault channel", "ACTIVE",
                         "Local storage or resource failures and fatal boot faults are reported through "
                         "the LED state."))
    return rows


def reason_payload(reason: Reason) -> dict:
    alert = reason.alert
    details = (alert.details or {}) if alert is not None else {}
    return {
        "code": reason.code, "error_code": reason.error_code, "tier": reason.tier,
        "currency": reason.currency, "gating": reason.gating, "severity": reason.severity,
        "message": reason.message, "since": reason.since, "last_seen_at": reason.last_seen_at,
        "alert_id": reason.alert_id, "alert_state": alert.state if alert is not None else None,
        "acknowledged_at": alert.acknowledged_at if alert is not None else None,
        "acknowledged_by": details.get("acknowledged_by"),
        "boot_id": details.get("boot_id"), "firmware_version": details.get("firmware_version"),
        "binding": details.get("binding"), "latch": details.get("latch"),
        "evidence_summary": reason.evidence_summary, "clear_condition": reason.clear_condition,
        "operator": reason.operator,
    }


def _primary_payload(health: Health) -> dict | None:
    primary = health.primary
    return None if primary is None else {
        "code": primary.code, "error_code": primary.error_code, "tier": primary.tier,
        "message": primary.message, "since": primary.since}


def health_summary(health: Health) -> dict:
    """The compact object for fleet lists."""
    return {
        "mode": health.mode, "tier": health.tier, "derived_lifecycle": health.derived_lifecycle,
        "last_error_code": health.last_error_code, "primary": _primary_payload(health),
        "degraded_count": sum(reason.tier == DEGRADED for reason in health.reasons),
        "warning_count": sum(reason.tier == WARNING for reason in health.reasons),
    }


def health_detail(session: Session, connector: Connector, health: Health) -> dict:
    """The full object for one device, including operator eligibility."""
    for reason in health.reasons + health.other_active_alerts:
        if reason.code == "QUARANTINED_DUPLICATE_SERIAL" and reason.alert is not None:
            reason.operator = operator_policy(
                reason, duplicate_claimed=duplicate_serial_claimed(session, connector))
    stored = connector.last_error_code
    return {
        **health_summary(health),
        "evaluated_at": health.evaluated_at, "lifecycle_state": health.lifecycle_state,
        "reasons": [reason_payload(reason) for reason in health.reasons],
        "other_active_alerts": [reason_payload(reason) for reason in health.other_active_alerts],
        "terminal_link": health.terminal_link, "coverage": coverage(connector),
        "device_error": {
            "code": stored, "message": connector.last_error_message,
            "derived_code": health.last_error_code,
            "backed": stored is not None and stored == health.last_error_code,
            "backing_alert_ids": [reason.alert_id for reason in health.reasons
                                  if stored and reason.error_code == stored and reason.alert_id],
        },
    }


def gate_effect(stored_code: str | None, derived_code: str | None) -> str:
    """HIL and factory holds both key on whether last_error_code is set."""
    if stored_code and not derived_code:
        return "HOLD_LIFTED"
    if derived_code and not stored_code:
        return "HOLD_ADDED"
    return "HOLD_CODE_CHANGED" if stored_code != derived_code else "UNCHANGED"


def shadow_report(session: Session, *, now: datetime | None = None) -> dict:
    """Every active connector whose stored lifecycle or error differs from the derived pair."""
    now = now or utc_now()
    connectors = session.scalars(select(Connector).where(Connector.active == True)  # noqa: E712
                                 .order_by(Connector.display_name, Connector.id)).all()
    healths = evaluate_health_batch(session, connectors, now=now)
    rows, transitions, effects = [], {}, {}
    for connector in connectors:
        health = healths[connector.id]
        stored = (connector.lifecycle_state, connector.last_error_code)
        derived = (health.derived_lifecycle, health.last_error_code)
        if stored == derived:
            continue
        effect = gate_effect(connector.last_error_code, health.last_error_code)
        pair = f"{stored[0]}->{derived[0]}"
        transitions[pair] = transitions.get(pair, 0) + 1
        effects[effect] = effects.get(effect, 0) + 1
        rows.append({
            "connector_id": connector.connector_id, "display_name": connector.display_name,
            "connected": connector.connected,
            "stored": {"lifecycle_state": stored[0], "last_error_code": stored[1]},
            "derived": {"lifecycle_state": derived[0], "last_error_code": derived[1], "tier": health.tier},
            "gate_effect": effect, "primary_reason": _primary_payload(health),
            "reasons": [f"{reason.code}:{reason.tier}:{reason.currency}" for reason in health.reasons],
        })
    return {"mode": mode(), "evaluated_at": now, "rows": rows,
            "counts": {"connectors": len(connectors), "differences": len(rows),
                       "transitions": transitions, "gate_effects": effects}}

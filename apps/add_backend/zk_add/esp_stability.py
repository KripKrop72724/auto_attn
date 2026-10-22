"""ESP reset history from ADD's own heartbeat telemetry.

Evidence only. A boot count, online time or a neighbour's reset never names a
cause: 2.6.x firmware does not report why it reset, so ADD cannot tell a
crash from a brownout or an external power/EN reset. Telemetry is retained
for settings.telemetry_retention_days (30), so a 7-day window is complete
unless the connector was onboarded more recently. Reboots ADD itself caused
(an OTA's READY_TO_BOOT restart, a dispatched ESP_REBOOT) are not resets.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from zk_add.models import Connector, DeviceCommand, DeviceTelemetry
from zk_add.time_utils import ensure_utc, utc_now

HISTORY_DAYS = 7
# A longer gap between two samples of one boot is time the ESP was not
# heartbeating (offline or busy), so it is not counted as online time.
ONLINE_GAP_SECONDS = 120
# Owner rule (A3): warn before a campaign when the ESP resets more than this
# often per online hour on its current firmware, or has been up less long.
RESET_RATE_WARNING_PER_HOUR = 0.5
MINIMUM_UPTIME_SECONDS = 30 * 60
PEER_RESET_WINDOW_SECONDS = 120
# Seven days at the 15 s heartbeat is about 40,000 samples.
SAMPLE_LIMIT = 200_000
PAYLOAD_CHUNK = 500
ESP_REBOOT_COMMAND = "ESP_REBOOT"


def _versions_match(value: str | None, version: str | None) -> bool:
    from zk_add.ota import _versions_match as match  # ota is a heavy module

    return bool(version) and match(value, version)


def _boot_sequence(session: Session, connector: Connector, *, since: datetime,
                   until: datetime) -> tuple[list[dict], int]:
    """Boots in [since, until] in order of their first sample, with online time and firmware."""
    rows = session.execute(
        select(DeviceTelemetry.id, DeviceTelemetry.boot_id, DeviceTelemetry.created_at)
        .where(DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.created_at >= since,
               DeviceTelemetry.created_at <= until)
        .order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(SAMPLE_LIMIT)).all()
    boots: dict[str, dict] = {}
    for row_id, boot_id, created_at in rows:
        if not boot_id:
            continue
        created_at = ensure_utc(created_at)
        boot = boots.get(boot_id)
        if boot is None:
            boots[boot_id] = {"boot_id": boot_id, "first_id": row_id, "first_at": created_at,
                              "last_at": created_at, "online": 0.0, "version": None}
            continue
        gap = (created_at - boot["last_at"]).total_seconds()
        if 0 <= gap <= ONLINE_GAP_SECONDS:
            boot["online"] += gap
        boot["last_at"] = created_at
    sequence = list(boots.values())
    first_ids = [boot["first_id"] for boot in sequence]
    versions: dict[int, str | None] = {}
    for offset in range(0, len(first_ids), PAYLOAD_CHUNK):
        for row_id, payload in session.execute(select(DeviceTelemetry.id, DeviceTelemetry.payload).where(
                DeviceTelemetry.id.in_(first_ids[offset:offset + PAYLOAD_CHUNK]))):
            versions[row_id] = (payload or {}).get("firmware_version") if isinstance(payload, dict) else None
    for boot in sequence:
        boot["version"] = versions.get(boot["first_id"])
    return sequence, len(rows)


def _add_reboots(session: Session, connector: Connector, *, since: datetime,
                 until: datetime) -> list[tuple[datetime, str | None]]:
    """Restarts ADD caused: READY_TO_BOOT reports (with the target booted next) and dispatched ESP_REBOOTs."""
    from zk_add.ota import FirmwareDeployment, FirmwareEvent

    reboots = [(ensure_utc(at), target) for at, target in session.execute(
        select(FirmwareEvent.created_at, FirmwareDeployment.target_version)
        .join(FirmwareDeployment, FirmwareDeployment.id == FirmwareEvent.deployment_id)
        .where(FirmwareDeployment.connector_id == connector.id, FirmwareEvent.state == "READY_TO_BOOT",
               FirmwareEvent.created_at >= since, FirmwareEvent.created_at <= until))]
    reboots += [(ensure_utc(at), None) for at in session.scalars(
        select(DeviceCommand.dispatched_at).where(
            DeviceCommand.connector_id == connector.id, DeviceCommand.command_type == ESP_REBOOT_COMMAND,
            DeviceCommand.dispatched_at.is_not(None), DeviceCommand.dispatched_at >= since,
            DeviceCommand.dispatched_at <= until))]
    return sorted(reboots, key=lambda item: item[0])


def _between(reboots: list[tuple[datetime, str | None]], previous: dict, current: dict) -> list[str | None]:
    return [target for at, target in reboots if previous["first_at"] <= at <= current["first_at"]]


def _summarize(sequence: list[dict], reboots: list[tuple[datetime, str | None]], *, since: datetime,
               until: datetime, days: int, firmware_version: str | None, samples: int) -> dict:
    """resets counts a boot only when the boot before it ran the same firmware and ADD caused no
    restart in between: the first boot of the window may be a planned power-on, and an OTA's own
    restart, the target boot and its return are not resets of the firmware being measured."""
    def matches(boot: dict) -> bool:
        return not firmware_version or _versions_match(boot["version"], firmware_version)

    resets = commanded = version_changes = 0
    for previous, current in zip(sequence, sequence[1:]):
        if not matches(current):
            continue
        if not matches(previous):
            version_changes += 1
        elif _between(reboots, previous, current):
            commanded += 1
        else:
            resets += 1
    counted = [boot for boot in sequence if matches(boot)]
    online_hours = sum(boot["online"] for boot in counted) / 3600
    return {
        "window_started_at": since.isoformat(), "window_ended_at": until.isoformat(), "days": days,
        "firmware_version": firmware_version, "boot_ids": len(counted), "resets": resets,
        "add_restarts_excluded": commanded, "firmware_changes_excluded": version_changes,
        "online_hours": round(online_hours, 2),
        # At least one online hour, so a handful of samples cannot inflate it.
        "reset_rate_per_online_hour": round(resets / max(online_hours, 1.0), 3),
        "samples": samples, "complete": samples < SAMPLE_LIMIT,
    }


def boot_history(session: Session, connector: Connector, *, until: datetime,
                 firmware_version: str | None = None, days: int = HISTORY_DAYS) -> dict:
    """Distinct boots, resets and online time in [until - days, until]."""
    until = ensure_utc(until)
    since = until - timedelta(days=days)
    sequence, samples = _boot_sequence(session, connector, since=since, until=until)
    reboots = _add_reboots(session, connector, since=since, until=until)
    return _summarize(sequence, reboots, since=since, until=until, days=days,
                      firmware_version=firmware_version, samples=samples)


def current_uptime_seconds(session: Session, connector: Connector, *, now: datetime | None = None) -> int | None:
    """The current boot's uptime from its latest heartbeat; None when offline or unknown."""
    if not connector.connected or not connector.boot_id:
        return None
    now = now or utc_now()
    row = session.execute(select(DeviceTelemetry.created_at, DeviceTelemetry.uptime_seconds).where(
        DeviceTelemetry.connector_id == connector.id, DeviceTelemetry.boot_id == connector.boot_id,
    ).order_by(DeviceTelemetry.id.desc()).limit(1)).first()
    if row is None or row.uptime_seconds is None or row.uptime_seconds < 0:
        return None
    return int(max(0.0, row.uptime_seconds + (now - ensure_utc(row.created_at)).total_seconds()))


def _started_by_add(sequence: list[dict], reboots: list[tuple[datetime, str | None]], boot_id: str | None) -> bool:
    """Whether ADD's own restart started this boot: an ESP_REBOOT, or an OTA whose target it runs."""
    for previous, current in zip(sequence, sequence[1:]):
        if current["boot_id"] == boot_id:
            return any(target is None or _versions_match(current["version"], target)
                       for target in _between(reboots, previous, current))
    return False


def stability_warnings(session: Session, connector: Connector, *, now: datetime | None = None) -> list[dict]:
    """Owner-overridable preflight warnings for one connector (A3); never a verdict."""
    now = ensure_utc(now or utc_now())
    since = now - timedelta(days=HISTORY_DAYS)
    sequence, samples = _boot_sequence(session, connector, since=since, until=now)
    reboots = _add_reboots(session, connector, since=since, until=now)
    version = connector.firmware_version
    history = _summarize(sequence, reboots, since=since, until=now, days=HISTORY_DAYS,
                         firmware_version=version, samples=samples)
    warnings = []
    rate = history["reset_rate_per_online_hour"]
    if rate > RESET_RATE_WARNING_PER_HOUR:
        warnings.append({
            "code": "ESP_RESET_RATE_HIGH", "connector_id": connector.connector_id,
            "display_name": connector.display_name, "reset_rate_per_online_hour": rate,
            "threshold_per_hour": RESET_RATE_WARNING_PER_HOUR, "history": history,
            "message": (f"{connector.display_name}: {history['resets']} ESP resets in "
                        f"{history['online_hours']} online hours on {version or 'its firmware'} over "
                        f"{history['days']} days ({rate}/h; the warning threshold is "
                        f"{RESET_RATE_WARNING_PER_HOUR}/h)."),
        })
    uptime = current_uptime_seconds(session, connector, now=now)
    if (uptime is not None and uptime < MINIMUM_UPTIME_SECONDS
            and not _started_by_add(sequence, reboots, connector.boot_id)):
        warnings.append({
            "code": "ESP_UPTIME_SHORT", "connector_id": connector.connector_id,
            "display_name": connector.display_name, "uptime_seconds": uptime,
            "threshold_seconds": MINIMUM_UPTIME_SECONDS,
            "message": (f"{connector.display_name}: the ESP has been up {uptime // 60} min since a reset ADD "
                        f"did not cause (the warning threshold is {MINIMUM_UPTIME_SECONDS // 60} min)."),
        })
    return warnings


def warnings_digest(warnings: list[dict]) -> str:
    """Binds an owner's override to the warnings shown: which ESP, which rule; not the live figures."""
    keys = sorted({(str(warning.get("connector_id")), str(warning.get("code"))) for warning in warnings})
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()


def location_peers(session: Session, connector: Connector) -> tuple[str | None, list[Connector]]:
    """Other active ESPs at the same place: the reviewed nationwide location, or the same site."""
    from zk_add.zkt270_scope import BY_ID

    target = BY_ID.get(connector.connector_id)
    clauses = []
    if target is not None:
        clauses.append(Connector.connector_id.in_(
            sorted(key for key, other in BY_ID.items() if other.location == target.location)))
    if connector.site_id is not None:
        clauses.append(Connector.site_id == connector.site_id)
    if not clauses:
        return None, []
    peers = list(session.scalars(select(Connector).where(
        or_(*clauses), Connector.id != connector.id, Connector.active == True,  # noqa: E712
    ).order_by(Connector.id)).all())
    label = target.location if target is not None else connector.zone_id
    return label, peers


def peer_resets(session: Session, connector: Connector, *, at: datetime,
                window_seconds: int = PEER_RESET_WINDOW_SECONDS, now: datetime | None = None) -> dict:
    """Boots of the other ESPs at this location that started within +-window of `at`.

    A boot's start is estimated from its first sample in the search window
    (received time minus uptime); its first heartbeat can arrive minutes later.
    """
    at = ensure_utc(at)
    now = now or utc_now()
    label, peers = location_peers(session, connector)
    window = timedelta(seconds=window_seconds)
    found = []
    for peer in peers:
        seen: set[str] = set()
        for boot_id, created_at, uptime in session.execute(
                select(DeviceTelemetry.boot_id, DeviceTelemetry.created_at, DeviceTelemetry.uptime_seconds)
                .where(DeviceTelemetry.connector_id == peer.id, DeviceTelemetry.created_at >= at - window,
                       DeviceTelemetry.created_at <= at + window + timedelta(minutes=15))
                .order_by(DeviceTelemetry.created_at, DeviceTelemetry.id).limit(500)):
            if not boot_id or boot_id in seen or uptime is None or uptime < 0:
                continue
            seen.add(boot_id)
            started = ensure_utc(created_at) - timedelta(seconds=uptime)
            offset = (started - at).total_seconds()
            if abs(offset) <= window_seconds:
                found.append({"connector_id": peer.connector_id, "display_name": peer.display_name,
                              "boot_id": boot_id, "estimated_started_at": started.isoformat(),
                              "offset_seconds": round(offset)})
    return {"location": label, "peers_checked": len(peers), "window_seconds": window_seconds,
            "observed_until": now.isoformat(), "complete": now >= at + window + timedelta(minutes=15),
            "resets": found}

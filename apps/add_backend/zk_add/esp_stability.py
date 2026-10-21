"""ESP reset history from ADD's own heartbeat telemetry.

Evidence only. A boot count, online time or a neighbour's reset never names a
cause: 2.6.x firmware does not report why it reset, so ADD cannot tell a
crash from a brownout or an external power/EN reset. Telemetry is retained
for settings.telemetry_retention_days (30), so a 7-day window is complete
unless the connector was onboarded more recently.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from zk_add.models import Connector, DeviceTelemetry
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


def _versions_match(value: str | None, version: str | None) -> bool:
    from zk_add.ota import _versions_match as match  # ota is a heavy module

    return bool(version) and match(value, version)


def boot_history(session: Session, connector: Connector, *, until: datetime,
                 firmware_version: str | None = None, days: int = HISTORY_DAYS) -> dict:
    """Distinct boots and online time in [until - days, until].

    resets counts every boot after the first one in the window: the first boot
    may be a planned power-on and is not evidence of a reset. The rate divides
    by at least one online hour so a handful of samples cannot inflate it.
    """
    until = ensure_utc(until)
    since = until - timedelta(days=days)
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
            boots[boot_id] = {"first_id": row_id, "last_at": created_at, "online": 0.0}
            continue
        gap = (created_at - boot["last_at"]).total_seconds()
        if 0 <= gap <= ONLINE_GAP_SECONDS:
            boot["online"] += gap
        boot["last_at"] = created_at
    if firmware_version and boots:
        first_ids = [boot["first_id"] for boot in boots.values()]
        versions: dict[int, str | None] = {}
        for offset in range(0, len(first_ids), PAYLOAD_CHUNK):
            for row_id, payload in session.execute(select(DeviceTelemetry.id, DeviceTelemetry.payload).where(
                    DeviceTelemetry.id.in_(first_ids[offset:offset + PAYLOAD_CHUNK]))):
                versions[row_id] = (payload or {}).get("firmware_version") if isinstance(payload, dict) else None
        boots = {boot_id: boot for boot_id, boot in boots.items()
                 if _versions_match(versions.get(boot["first_id"]), firmware_version)}
    online_hours = sum(boot["online"] for boot in boots.values()) / 3600
    resets = max(0, len(boots) - 1)
    return {
        "window_started_at": since.isoformat(), "window_ended_at": until.isoformat(), "days": days,
        "firmware_version": firmware_version, "boot_ids": len(boots), "resets": resets,
        "online_hours": round(online_hours, 2),
        "reset_rate_per_online_hour": round(resets / max(online_hours, 1.0), 3),
        "samples": len(rows), "complete": len(rows) < SAMPLE_LIMIT,
    }


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


def stability_warnings(session: Session, connector: Connector, *, now: datetime | None = None) -> list[dict]:
    """Owner-overridable preflight warnings for one connector (A3); never a verdict."""
    now = now or utc_now()
    warnings = []
    version = connector.firmware_version
    history = boot_history(session, connector, until=now, firmware_version=version)
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
    if uptime is not None and uptime < MINIMUM_UPTIME_SECONDS:
        warnings.append({
            "code": "ESP_UPTIME_SHORT", "connector_id": connector.connector_id,
            "display_name": connector.display_name, "uptime_seconds": uptime,
            "threshold_seconds": MINIMUM_UPTIME_SECONDS,
            "message": (f"{connector.display_name}: the ESP has been up {uptime // 60} min "
                        f"(the warning threshold is {MINIMUM_UPTIME_SECONDS // 60} min)."),
        })
    return warnings


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

"""One connector's expiring ADD interruption; never an attendance/HIL verdict."""
from datetime import datetime, timedelta
import math
from pathlib import Path
import time
from uuid import UUID, uuid4

from sqlalchemy import select, update

from zk_add.hil_scope import HilTarget, target_matches
from zk_add.hil_runs import _release_identity
from zk_add.models import Connector, DeviceCommand, DeviceTelemetry, TemporaryAdminLease
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease
from zk_add.runtime_contract import RUNTIMES, journal_storage_status, worker_snapshot_fresh
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID

KEY = "add_interruption"
DURATION = timedelta(seconds=30)


def server_clock():
    """Linux workers share a monotonic clock and boot identity across restarts."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if str(UUID(boot)) != boot:
            return None
        return boot, time.monotonic_ns() // 1_000_000
    except (OSError, ValueError):
        return None


def _remaining(value, start, end, now):
    clock = server_clock()
    if clock is None or clock[0] != value["server_boot_id"] or clock[1] < value["started_monotonic_ms"]:
        return 0, "SERVER_CLOCK_CHANGED", clock
    if now < start:
        return 0, "WALL_CLOCK_REGRESSED", clock
    remaining = min((end - now).total_seconds(), (value["expires_monotonic_ms"] - clock[1]) / 1000)
    return max(0, remaining), "DEADLINE" if remaining <= 0 else "ACTIVE", clock


class TransportInterrupted(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__("HIL_ADD_INTERRUPT_30S")
        self.retry_after = retry_after


def _event(session, run, state, facts):
    session.add(FirmwareEvent(deployment_id=run.deployment_id, state=state,
        details={"run_id": run.run_id, "target": run.target,
                 "release_identity": run.release_identity, **facts}))


def _target_matches(run, connector):
    try:
        return isinstance(run.target, dict) and target_matches(HilTarget(**run.target), connector)
    except (TypeError, ValueError):
        return False


def _control(run):
    """Malformed state can never turn a test into an indefinite outage."""
    if (run.baseline or {}).get("profile") != "FULL_REMOTE_HIL_V1" or not isinstance(run.result, dict):
        return None
    value = run.result.get(KEY)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    try:
        start = datetime.fromisoformat(value["started_at"])
        end = datetime.fromisoformat(value["expires_at"])
        if (start.tzinfo is None or end.tzinfo is None or end - start != DURATION
                or value["kind"] != "ADD_INTERRUPT_30S"
                or value["run_id"] != run.run_id or not value["control_id"]
                or not ensure_utc(run.started_at) + timedelta(minutes=3) <= start
                < ensure_utc(run.started_at) + timedelta(minutes=4)
                or end > ensure_utc(run.ends_at)
                or not isinstance(value["server_boot_id"], str) or not value["server_boot_id"]
                or type(value["started_monotonic_ms"]) is not int
                or type(value["expires_monotonic_ms"]) is not int
                or not 0 <= value["started_monotonic_ms"] < value["expires_monotonic_ms"] <= 2**53 - 1
                or value["expires_monotonic_ms"] - value["started_monotonic_ms"] != 30000):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return value, start, end


def _healthy(session, connector, run, release, now, *, telemetry=None):
    if telemetry is None:
        telemetry = session.scalar(select(DeviceTelemetry)
            .where(DeviceTelemetry.connector_id == connector.id)
            .order_by(DeviceTelemetry.id.desc()).limit(1))
    if (telemetry is None or not connector.connected
            or telemetry.connector_id != connector.id
            or telemetry.boot_id != connector.boot_id or telemetry.boot_id != run.baseline["boot_id"]
            or not 0 <= (now - ensure_utc(telemetry.created_at)).total_seconds() <= 45):
        raise ValueError("Fresh unchanged-boot telemetry is required")
    payload = telemetry.payload or {}
    diagnostics, ota = payload.get("diagnostics") or {}, payload.get("ota") or {}
    try:
        sampled = datetime.fromisoformat(payload["_trusted_envelope_sent_at"].replace("Z", "+00:00"))
        fresh = sampled.tzinfo is not None and 0 <= (now - sampled).total_seconds() <= 45
    except (KeyError, TypeError, ValueError):
        fresh = False
    if (not fresh or diagnostics.get("schema_version") != 2
            or diagnostics.get("boot_id") != connector.boot_id
            or diagnostics.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or diagnostics.get("delivery_authority") != "ADD"
            or diagnostics.get("journal_format") != 1
            or ota.get("running_version") != release.version
            or ota.get("image_sha256") != run.release_identity["application_sha256"]
            or ota.get("running_partition") not in {"ota_0", "ota_1"}
            or ota.get("secure_boot") is not True or ota.get("rollback_enabled") is not True):
        raise ValueError("Current signed writer and sampling evidence are required")
    tick = diagnostics.get("sampled_uptime_ms")
    if (type(tick) is not int or type(telemetry.uptime_seconds) is not int
            or not -5000 <= telemetry.uptime_seconds * 1000 - tick <= 5000):
        raise ValueError("Diagnostic sampling uptime is not current")
    storage = diagnostics.get("storage") or {}
    if (storage.get("durability") != "HEALTHY" or storage.get("persistence_verified") is not True
            or storage.get("recovery_complete") is not True or storage.get("upgrade_ready") is not True
            or any(storage.get(key) for key in ("error_code", "upgrade_error", "persistence_probe_error",
                "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))
            or journal_storage_status(diagnostics, telemetry.uptime_seconds) != "HEALTHY"):
        raise ValueError("Verified local preservation is required before interruption")
    runtime = RUNTIMES["ZKT_JOURNAL_V1"]
    workers, queues = diagnostics.get("workers") or [], diagnostics.get("queues") or []
    names = {row.get("name") for row in workers}
    depths = {row.get("name"): row for row in queues}
    if (len(names) != len(workers) or not runtime.workers <= names <= runtime.workers | runtime.auxiliary_workers
            or any(row.get("state") not in {"RUNNING", "WAITING_NETWORK"}
                   or not worker_snapshot_fresh(row, telemetry.uptime_seconds, runtime) for row in workers)
            or len(depths) != len(queues) or not runtime.queues <= depths.keys()
            or any(depths[name].get("count_known") is not True
                   or type(depths[name].get("records")) is not int or depths[name]["records"] != 0
                   for name in runtime.queues & depths.keys())):
        raise ValueError("Healthy workers and settled known queues are required")
    terminal = payload.get("zkt") or {}
    if (terminal.get("online") is not True or terminal.get("serial") != run.target["terminal_serial"]
            or not connector.zkt_device.online
            or type(diagnostics.get("source_generation")) is not int
            or diagnostics.get("source_generation") != run.baseline["source_generation"]
            or type(diagnostics.get("committed_source_cursor")) is not int
            or diagnostics["committed_source_cursor"] < run.baseline["source_cursor"]
            or diagnostics["committed_source_cursor"] != terminal.get("attendance_count")):
        raise ValueError("Current terminal and source continuity are required")
    return telemetry


def start_interruption(session, run_id, *, actor, idempotency_key):
    from zk_add.service import ACTIVE_COMMAND_STATES

    if not actor or len(actor) > 120 or not 8 <= len(idempotency_key) <= 120:
        raise ValueError("A bounded actor and idempotency key are required")
    # Same lock order as ingestion: connector, then observation. Lookup itself
    # takes no lock and must be rechecked under both locks.
    connector_id = session.scalar(select(FirmwareHilRun.connector_id).where(FirmwareHilRun.run_id == run_id))
    if connector_id is None:
        raise ValueError("HIL observation was not found")
    connector = session.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id)
        .with_for_update().execution_options(populate_existing=True))
    existing = (run.result or {}).get(KEY)
    if existing is not None:
        if (not isinstance(existing, dict) or existing.get("actor") != actor
                or existing.get("idempotency_key") != idempotency_key):
            raise ValueError("This observation already has an interruption attempt")
        if _control(run) is None:
            raise ValueError("Stored interruption evidence is invalid")
        return existing, False  # Replays never extend the deadline or create a new action.
    now = utc_now()
    release = session.get(FirmwareRelease, run.release_id)
    deployment = session.get(FirmwareDeployment, run.deployment_id)
    campaign = session.get(FirmwareCampaign, deployment.campaign_id) if deployment else None
    target = BY_ID.get(connector.connector_id) if connector else None
    if (run.status != "OBSERVING" or run.baseline.get("profile") != "FULL_REMOTE_HIL_V1"
            or target is None or target.identity.model_dump() != run.target
            or not target_matches(target.identity, connector) or connector.firmware_family != "zkt"
            or connector.is_spare or not connector.zkt_custody_enabled
            or release is None or release.version != "2.7.0" or release.state != "HIL_ONLY"
            or release.revoked_at is not None or release.manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or _release_identity(release).model_dump(mode="json") != run.release_identity
            or deployment is None or deployment.status != "SUCCEEDED"
            or campaign is None or campaign.status not in {"ACTIVE", "COMPLETED"}):
        raise ValueError("Interruption requires the exact installed experimental writer observation")
    latest = session.scalar(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc()).limit(1))
    if latest != deployment.id:
        raise ValueError("A newer deployment supersedes this observation")
    if not ensure_utc(run.started_at) + timedelta(minutes=3) <= now < ensure_utc(run.started_at) + timedelta(minutes=4):
        raise ValueError("Interruption must start during observation minute four")
    if now + DURATION > ensure_utc(run.ends_at):
        raise ValueError("Interruption would exceed the observation deadline")
    if session.scalar(select(DeviceCommand.id).where(DeviceCommand.connector_id == connector.id,
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES)).limit(1)):
        raise ValueError("An active device command prevents interruption")
    if session.scalar(select(TemporaryAdminLease.id).where(
            TemporaryAdminLease.zkt_device_id == connector.zkt_device.id,
            TemporaryAdminLease.state.not_in(["REVOKED", "FAILED", "CANCELLED"])).limit(1)):
        raise ValueError("An unresolved administrator lease prevents interruption")
    telemetry = _healthy(session, connector, run, release, now)
    clock = server_clock()
    if clock is None:
        raise ValueError("A shared Linux boot and monotonic clock is required for a bounded interruption")
    value = {"schema_version": 1, "kind": "ADD_INTERRUPT_30S", "control_id": str(uuid4()),
        "run_id": run.run_id, "actor": actor, "idempotency_key": idempotency_key,
        "started_at": now.isoformat(), "expires_at": (now + DURATION).isoformat(),
        "boot_before": connector.boot_id, "baseline_telemetry_id": telemetry.id,
        "server_boot_id": clock[0], "started_monotonic_ms": clock[1], "expires_monotonic_ms": clock[1] + 30000,
        "rejected_at": None, "transport_restored_at": None, "outcome": "NOT_EVALUATED"}
    # PostgreSQL row locks serialize this with ingestion. The conditional write
    # also prevents duplicate starts on SQLite, whose FOR UPDATE is a no-op.
    changed = session.execute(update(FirmwareHilRun).where(FirmwareHilRun.id == run.id,
        FirmwareHilRun.status == "OBSERVING", FirmwareHilRun.result[KEY].as_string().is_(None))
        .values(result={**(run.result or {}), KEY: value}).execution_options(synchronize_session=False))
    session.expire(run, ["result", "status"])
    if changed.rowcount != 1:
        existing = (run.result or {}).get(KEY)
        if (_control(run) is None or existing.get("actor") != actor
                or existing.get("idempotency_key") != idempotency_key):
            raise ValueError("The observation changed before interruption could commit")
        return existing, False
    _event(session, run, "HIL_ADD_INTERRUPT_STARTED", value)
    session.flush()
    return value, True


def pending_stream_close(session, run_id, control_id):
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id,
                                                    FirmwareHilRun.status == "OBSERVING"))
    parsed = _control(run) if run else None
    if parsed is None or parsed[0]["control_id"] != control_id or parsed[0].get("transport_restored_at") is not None:
        return None
    now = utc_now()
    remaining, _, _ = _remaining(*parsed, now)
    if remaining <= 0:
        return None
    connector = session.get(Connector, run.connector_id)
    if connector is None or not _target_matches(run, connector):
        return None
    return connector.connector_id, now + timedelta(seconds=remaining)


def enforce_transport(session, connector, *, transport):
    """Called only after authentication and before receipt/command processing.

    Persisted wall/monotonic deadlines are checked on every request, including
    after server restart. A host reboot or missing clock restores admission
    immediately. No timer, worker, operator resume or device clock is necessary.
    """
    if transport not in {"HTTP", "WEBSOCKET"}:
        raise ValueError("Unknown HIL transport")
    if connector.firmware_family != "zkt":
        return
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.connector_id == connector.id,
        FirmwareHilRun.status == "OBSERVING").limit(1))
    parsed = _control(run) if run else None
    if not parsed or not _target_matches(run, connector):
        return
    value, _, _ = parsed
    # Restoration is permanent. A later wall-clock correction cannot reactivate
    # the same immutable test after either deadline or an early safety abort.
    if value.get("transport_restored_at") is not None:
        return
    # HTTP authentication has not locked the connector row. Take it first so
    # restoration cannot invert ingestion/start's connector -> run lock order.
    if session.scalar(select(Connector.id).where(Connector.id == connector.id).with_for_update()) is None:
        return
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.id == run.id)
        .with_for_update().execution_options(populate_existing=True))
    parsed = _control(run) if run and run.status == "OBSERVING" else None
    if not parsed or parsed[0].get("transport_restored_at") is not None:
        return
    value, start, end = parsed
    now = utc_now()
    remaining, expiry_reason, clock = _remaining(value, start, end, now)
    if remaining > 0:
        if value.get("rejected_at") is None:
            value = {**value, "rejected_at": now.isoformat(), "rejected_transport": transport,
                     "rejected_monotonic_ms": clock[1]}
            run.result = {**run.result, KEY: value}
            _event(session, run, "HIL_ADD_INTERRUPT_OBSERVED", value)
        # Persist only authentication and test evidence. The endpoint or
        # envelope handler has not yet accepted any attendance/queue item.
        session.commit()
        raise TransportInterrupted(max(1, min(30, math.ceil(remaining))))
    if value.get("transport_restored_at") is None:
        value = {**value, "transport_restored_at": now.isoformat(),
                 "restored_transport": transport, "boot_after_transport": connector.boot_id,
                 "restored_monotonic_ms": clock[1] if clock else None, "expiry_reason": expiry_reason}
        run.result = {**run.result, KEY: value}
        _event(session, run, "HIL_ADD_TRANSPORT_RESTORED", value)

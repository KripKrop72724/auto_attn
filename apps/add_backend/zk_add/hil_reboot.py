"""One expiring, exact-writer ESP reboot; never a release or HIL verdict.

The action expires after at most sixty seconds. Evidence collection continues
until observation minute eight. A generic command success, a changed boot ID,
or an intent written before a reset does not prove a safe reboot. Only the
firmware's post-idle-gate software-reset witness plus healthy new-boot telemetry
can complete this control.
"""
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from sqlalchemy import select, update

from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.hil_runs import _release_identity
from zk_add.hil_scope import HilTarget, target_matches
from zk_add.hil_transport import _healthy, server_clock
from zk_add.hil_validation import RecoveryTest
from zk_add.models import Connector, DeviceCommand, DeviceCommandEvent, DeviceTelemetry, TemporaryAdminLease
from zk_add.ota import (
    ACTIVE_DEPLOYMENT_STATES, FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease,
)
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt270_scope import BY_ID

KEY = "esp_reboot"
COMMAND = "ESP_REBOOT"
TTL_SECONDS = 60
SW_RESET_REASON = 3
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError):
        return False


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed


def _control(run, command=None):
    if not isinstance(run.result, dict) or not isinstance(run.baseline, dict) or not isinstance(run.target, dict):
        return None
    value = run.result.get(KEY)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    try:
        issued, expires, deadline = (_time(value[key]) for key in (
            "issued_at", "action_expires_at", "recovery_deadline"))
        if (run.baseline.get("profile") != "FULL_REMOTE_HIL_V1"
                or value["kind"] != COMMAND or value["run_id"] != run.run_id
                or not _uuid(value["run_id"]) or not _uuid(value["command_id"])
                or not 0 < (expires - issued).total_seconds() <= TTL_SECONDS
                or not ensure_utc(run.started_at) + timedelta(minutes=5) <= issued
                < ensure_utc(run.started_at) + timedelta(minutes=6)
                or deadline != ensure_utc(run.started_at) + timedelta(minutes=8)
                or not expires < deadline <= ensure_utc(run.ends_at)
                or type(value["expires_epoch"]) is not int or value["expires_epoch"] != int(expires.timestamp())
                or not isinstance(value["boot_before"], str) or not 1 <= len(value["boot_before"]) <= 47
                or not isinstance(value["terminal_serial"], str) or not 1 <= len(value["terminal_serial"]) <= 79
                or not DIGEST.fullmatch(value["application_sha256"])
                or value["application_sha256"] != run.release_identity["application_sha256"]
                or value["boot_before"] != run.baseline["boot_id"]
                or value["terminal_serial"] != run.target["terminal_serial"]
                or type(value["baseline_uptime_ms"]) is not int or not 0 <= value["baseline_uptime_ms"] <= 2**53-1
                or value.get("outcome") not in {"NOT_EVALUATED", "SUCCEEDED", "FAILED", "INCOMPLETE"}
                or not isinstance(value["server_boot_id"], str) or not value["server_boot_id"]
                or type(value["started_monotonic_ms"]) is not int
                or type(value["expires_monotonic_ms"]) is not int
                or type(value["recovery_monotonic_ms"]) is not int
                or value["expires_monotonic_ms"] - value["started_monotonic_ms"] != TTL_SECONDS * 1000
                or value["recovery_monotonic_ms"] - value["started_monotonic_ms"] != int((deadline-issued).total_seconds()*1000)
                or not 0 <= value["started_monotonic_ms"] < value["expires_monotonic_ms"] <= 2**53 - 1):
            return None
        if value.get("intent_reported_at") is not None and not issued <= _time(value["intent_reported_at"]) < expires:
            return None
        if value.get("recovered_at") is not None and not issued <= _time(value["recovered_at"]) <= deadline:
            return None
        if value.get("outcome") == "SUCCEEDED" and (not isinstance(value.get("witness"), dict)
                or not value.get("intent_reported_at") or not value.get("recovered_at")
                or type(value.get("recovery_telemetry_id")) is not int or value["recovery_telemetry_id"] <= 0):
            return None
        if command is not None and (command.command_id != value["command_id"]
                or command.command_type != COMMAND or command.connector_id != run.connector_id
                or command.expires_at is None or ensure_utc(command.expires_at) != expires
                or decrypt_json(command.payload_encrypted) != _payload(value)
                or decrypt_json(command.expected_state_encrypted) != {"serial": value["terminal_serial"]}):
            return None
    except (AttributeError, KeyError, TypeError, ValueError, InvalidToken, RuntimeError):
        return None
    return value


def _payload(value):
    return {"run_id": value["run_id"], "boot_id": value["boot_before"],
        "application_sha256": value["application_sha256"], "terminal_serial": value["terminal_serial"],
        "expires_at": value["expires_epoch"]}


def _scope(session, connector, run):
    release = session.get(FirmwareRelease, run.release_id)
    deployment = session.get(FirmwareDeployment, run.deployment_id)
    campaign = session.get(FirmwareCampaign, deployment.campaign_id) if deployment else None
    target = BY_ID.get(connector.connector_id)
    if (run.status != "OBSERVING" or (run.baseline or {}).get("profile") != "FULL_REMOTE_HIL_V1"
            or not _uuid(run.run_id) or target is None or target.identity.model_dump() != run.target
            or not target_matches(target.identity, connector) or connector.firmware_family != "zkt"
            or connector.is_spare or not connector.zkt_custody_enabled
            or release is None or release.state != "HIL_ONLY" or release.revoked_at is not None
            or release.version != "2.7.0" or release.manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or _release_identity(release).model_dump(mode="json") != run.release_identity
            or deployment is None or deployment.status != "SUCCEEDED"
            or campaign is None or campaign.status not in {"ACTIVE", "COMPLETED"}):
        raise ValueError("Reboot requires the exact installed experimental writer observation")
    latest = session.scalar(select(FirmwareDeployment.id).where(FirmwareDeployment.connector_id == connector.id)
        .order_by(FirmwareDeployment.id.desc()).limit(1))
    if latest != deployment.id or session.scalar(select(FirmwareDeployment.id).where(
            FirmwareDeployment.connector_id == connector.id,
            FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES)).limit(1)):
        raise ValueError("An active or newer firmware deployment prevents the test")
    return release


def _exclusive(session, connector, run, *, exclude_command=None):
    from zk_add.service import ACTIVE_COMMAND_STATES
    query = select(DeviceCommand.id).where(DeviceCommand.connector_id == connector.id,
                                          DeviceCommand.status.in_(ACTIVE_COMMAND_STATES))
    if exclude_command is not None:
        query = query.where(DeviceCommand.id != exclude_command)
    if session.scalar(query.limit(1)):
        raise ValueError("An active device command prevents reboot")
    if session.scalar(select(TemporaryAdminLease.id).where(
            TemporaryAdminLease.zkt_device_id == connector.zkt_device.id,
            TemporaryAdminLease.state.not_in(["REVOKED", "FAILED", "CANCELLED"])).limit(1)):
        raise ValueError("An unresolved administrator lease prevents reboot")
    interruption = (run.result or {}).get("add_interruption")
    if interruption is not None and (not isinstance(interruption, dict)
            or not interruption.get("transport_restored_at")):
        raise ValueError("ADD interruption has not demonstrably restored transport")


def _event(session, run, state, details):
    session.add(FirmwareEvent(deployment_id=run.deployment_id, state=state,
        details={"run_id": run.run_id, "target": run.target, "release_identity": run.release_identity, **details}))


def start_reboot(session, run_id, *, actor, idempotency_key):
    """Commit control and command together. No command is sent by this helper."""
    if not actor or len(actor) > 120 or not 8 <= len(idempotency_key) <= 120:
        raise ValueError("A bounded actor and idempotency key are required")
    connector_id = session.scalar(select(FirmwareHilRun.connector_id).where(FirmwareHilRun.run_id == run_id))
    if connector_id is None:
        raise ValueError("HIL observation was not found")
    connector = session.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id)
        .with_for_update().execution_options(populate_existing=True))
    existing = (run.result or {}).get(KEY)
    if existing is not None:
        command = session.scalar(select(DeviceCommand).where(DeviceCommand.command_id == existing.get("command_id"))) \
            if isinstance(existing, dict) else None
        if (_control(run, command) is None or command is None or existing.get("actor") != actor
                or existing.get("idempotency_key") != idempotency_key):
            raise ValueError("This observation already has a different or invalid reboot attempt")
        return existing, False
    now = utc_now()
    if not ensure_utc(run.started_at) + timedelta(minutes=5) <= now < ensure_utc(run.started_at) + timedelta(minutes=6):
        raise ValueError("ESP reboot must start during observation minute six")
    release = _scope(session, connector, run)
    _exclusive(session, connector, run)
    telemetry = _healthy(session, connector, run, release, now)
    diagnostics = (telemetry.payload or {}).get("diagnostics") or {}
    if (diagnostics.get("controlled_esp_reboot_v1") is not True
            or (diagnostics.get("journal_storage") or {}).get("hil_reboot_persistence_incident") is not False
            or not connector.boot_id
            or not 1 <= len(connector.boot_id) <= 47 or not 1 <= len(run.target["terminal_serial"]) <= 79):
        raise ValueError("The exact writer does not report controlled ESP reboot capability")
    clock = server_clock()
    if clock is None:
        raise ValueError("A shared server boot and monotonic clock is required")
    expires_epoch = int(now.timestamp()) + TTL_SECONDS
    value = {"schema_version": 1, "kind": COMMAND, "command_id": str(uuid4()), "run_id": run.run_id,
        "actor": actor, "idempotency_key": idempotency_key, "issued_at": now.isoformat(),
        "action_expires_at": datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat(),
        "expires_epoch": expires_epoch,
        "recovery_deadline": (ensure_utc(run.started_at) + timedelta(minutes=8)).isoformat(),
        "boot_before": connector.boot_id, "application_sha256": run.release_identity["application_sha256"],
        "terminal_serial": run.target["terminal_serial"], "baseline_telemetry_id": telemetry.id,
        "baseline_uptime_ms": diagnostics["sampled_uptime_ms"],
        "server_boot_id": clock[0], "started_monotonic_ms": clock[1],
        "expires_monotonic_ms": clock[1] + TTL_SECONDS * 1000,
        "recovery_monotonic_ms": clock[1] + int(((ensure_utc(run.started_at) + timedelta(minutes=8)) - now).total_seconds()*1000),
        "intent_reported_at": None, "witness": None, "recovered_at": None,
        "recovery_telemetry_id": None, "outcome": "NOT_EVALUATED"}
    changed = session.execute(update(FirmwareHilRun).where(FirmwareHilRun.id == run.id,
        FirmwareHilRun.status == "OBSERVING", FirmwareHilRun.result[KEY].as_string().is_(None))
        .values(result={**(run.result or {}), KEY: value}).execution_options(synchronize_session=False))
    session.expire(run, ["result"])
    if changed.rowcount != 1:
        raise ValueError("The observation changed before reboot could commit")
    command = DeviceCommand(command_id=value["command_id"], connector_id=connector.id,
        command_type=COMMAND, payload_encrypted=encrypt_json(_payload(value)),
        expected_state_encrypted=encrypt_json({"serial": value["terminal_serial"]}),
        desired_state_encrypted=encrypt_json({}), payload_summary={"hil_run_id": run.run_id},
        idempotency_key="hil-reboot:" + run.run_id, actor=actor, status="QUEUED",
        expires_at=_time(value["action_expires_at"]), created_at=now)
    session.add(command)
    session.flush()
    session.add(DeviceCommandEvent(command_id=command.id, status="QUEUED", details={"run_id": run.run_id}))
    _event(session, run, "HIL_ESP_REBOOT_REQUESTED", value)
    return value, True


def _run_for_command(session, command, *, lock=False):
    query = select(FirmwareHilRun).where(FirmwareHilRun.run_id == (command.payload_summary or {}).get("hil_run_id"))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def lock_reboot_command(session, command):
    """Serialize cancellation, result handling and dispatch bookkeeping."""
    session.scalar(select(Connector.id).where(Connector.id == command.connector_id).with_for_update())
    session.refresh(command)


def request_reboot_cancellation(session, command, *, actor):
    lock_reboot_command(session, command)
    run = _run_for_command(session, command, lock=True)
    value = _control(run, command) if run else None
    if value is None:
        raise ValueError("ESP_REBOOT_CONTROL_BINDING_INVALID")
    if command.status == "CANCEL_REQUESTED":
        return command
    if command.status in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
        raise ValueError("This command can no longer be cancelled.")
    now = utc_now()
    # An unmarked QUEUED command may already be on the socket. Always send a
    # durable cancellation; never infer that zero dispatch attempts is local.
    value = {**value, "cancel_requested_at": now.isoformat(), "cancel_requested_by": actor,
             "outcome": "INCOMPLETE", "reason": "ESP_REBOOT_CANCEL_REQUESTED"}
    run.result = {**run.result, KEY: value}
    command.status = "CANCEL_REQUESTED"
    command.result = {"kind": COMMAND, "run_id": run.run_id, "outcome": "INCOMPLETE",
                      "reason": "ESP_REBOOT_CANCEL_REQUESTED"}
    session.add(DeviceCommandEvent(command_id=command.id, status=command.status, details={"requested_by": actor}))
    _event(session, run, "HIL_ESP_REBOOT_CANCEL_REQUESTED", {"command_id": command.command_id})
    return command


def _action_window_open(value, now):
    clock = server_clock()
    return bool(clock is not None and clock[0] == value["server_boot_id"]
        and value["started_monotonic_ms"] <= clock[1] < value["expires_monotonic_ms"]
        and _time(value["issued_at"]) <= now < _time(value["action_expires_at"]))


def reboot_dispatch_allowed(session, command, *, now=None):
    if command.command_type != COMMAND:
        return True
    from zk_add.service import ACTIVE_COMMAND_STATES
    if command.status not in ACTIVE_COMMAND_STATES or command.status == "CANCEL_REQUESTED":
        return False
    now = now or utc_now()
    run = _run_for_command(session, command)
    value = _control(run, command) if run else None
    if value is None or value["outcome"] != "NOT_EVALUATED" or value.get("intent_reported_at"):
        return False
    if not _action_window_open(value, now):
        return False
    connector = session.get(Connector, command.connector_id)
    try:
        release = _scope(session, connector, run)
        _exclusive(session, connector, run, exclude_command=command.id)
        telemetry = _healthy(session, connector, run, release, now)
        diagnostics = telemetry.payload.get("diagnostics") or {}
        return (diagnostics.get("controlled_esp_reboot_v1") is True
            and (diagnostics.get("journal_storage") or {}).get("hil_reboot_persistence_incident") is False)
    except (KeyError, TypeError, ValueError):
        return False


def reboot_transport_allowed(session, command, *, now=None):
    """Cancellation is safe cleanup, not a fresh reboot authorization."""
    if command.command_type == COMMAND and command.status == "CANCEL_REQUESTED":
        run = _run_for_command(session, command)
        value = _control(run, command) if run else None
        return bool(value and _cancellation_window_open(value, now or utc_now()))
    return reboot_dispatch_allowed(session, command, now=now)


def _cancellation_window_open(value, now):
    clock = server_clock()
    return bool(clock is not None and clock[0] == value["server_boot_id"]
        and value["started_monotonic_ms"] <= clock[1] < value["recovery_monotonic_ms"]
        and _time(value["issued_at"]) <= now < _time(value["recovery_deadline"]))


def refresh_reboot_dispatch(command_id):
    """Recheck persisted deadline immediately before an asynchronous send."""
    from zk_add.db import session_scope
    from zk_add.service import serialize_command
    with session_scope() as session:
        command = session.scalar(select(DeviceCommand).where(DeviceCommand.command_id == command_id))
        return serialize_command(command) if command and reboot_transport_allowed(session, command) else None


def _finish(session, run, command, value, *, outcome, now, reason=None):
    value = {**value, "outcome": outcome}
    if reason:
        value["reason"] = reason
    run.result = {**run.result, KEY: value}
    command.status = "SUCCEEDED" if outcome == "SUCCEEDED" else "FAILED" if outcome == "FAILED" else "EXPIRED"
    command.completed_at = now
    command.result = {"kind": COMMAND, "run_id": run.run_id, "outcome": outcome,
        "recovered_at": value.get("recovered_at"), "recovery_telemetry_id": value.get("recovery_telemetry_id")}
    command.error_code = reason
    command.error_message = "Controlled ESP reboot did not produce verified recovery." if reason else None
    session.add(DeviceCommandEvent(command_id=command.id, status=command.status, details=command.result))
    _event(session, run, "HIL_ESP_REBOOT_" + ("RECOVERED" if outcome == "SUCCEEDED" else "INCOMPLETE"), value)


def _witness_valid(value, result, *, envelope_boot_id, sent_at, received_at, within_observation=True):
    try:
        expected = {"schema_version": 1, "kind": COMMAND, "command_id": value["command_id"],
            "run_id": value["run_id"], "boot_before": value["boot_before"],
            "application_sha256": value["application_sha256"], "terminal_serial": value["terminal_serial"],
            "expires_at": value["expires_epoch"], "attempted": True, "intent_persisted": True}
        if any(type(result.get(key)) is not type(item) or result.get(key) != item for key, item in expected.items()):
            return False
        if sent_at is None or sent_at.tzinfo is None or not timedelta(0) <= received_at - sent_at <= timedelta(seconds=45):
            return False
        if (not _time(value["issued_at"]) <= sent_at <= received_at
                or (within_observation and received_at > _time(value["recovery_deadline"]))):
            return False
        if result.get("recovered") is False and result.get("safe_checkpoint") is False:
            if result.get("outcome") == "NOT_OBSERVED":
                return (isinstance(result.get("boot_after"), str)
                    and 1 <= len(result["boot_after"]) <= 47 and result["boot_after"] == envelope_boot_id)
            return envelope_boot_id == value["boot_before"] and received_at < _time(value["action_expires_at"])
        checkpoint = result.get("safe_checkpoint_epoch")
        tick = result.get("safe_checkpoint_uptime_ms")
        return (result.get("recovered") is True and result.get("safe_checkpoint") is True
            and type(result.get("reset_reason")) is int and result["reset_reason"] == SW_RESET_REASON
            and isinstance(result.get("boot_after"), str) and 1 <= len(result["boot_after"]) <= 47
            and result["boot_after"] == envelope_boot_id and envelope_boot_id != value["boot_before"]
            and type(checkpoint) is int and int(_time(value["issued_at"]).timestamp()) <= checkpoint < value["expires_epoch"]
            and datetime.fromtimestamp(checkpoint, timezone.utc) <= sent_at
            and type(tick) is int and value["baseline_uptime_ms"] <= tick
            <= value["baseline_uptime_ms"] + (TTL_SECONDS + 45) * 1000)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _record_witness(value, result, *, now, envelope_sent_at, envelope_boot_id):
    if not value.get("intent_reported_at"):
        raise ValueError("ESP_REBOOT_PREBOOT_INTENT_NOT_OBSERVED")
    witness = {key: result[key] for key in (
        "schema_version", "kind", "command_id", "run_id", "boot_before", "boot_after",
        "application_sha256", "terminal_serial", "expires_at", "attempted", "intent_persisted",
        "safe_checkpoint", "recovered", "reset_reason", "safe_checkpoint_epoch", "safe_checkpoint_uptime_ms")}
    existing = value.get("witness")
    if existing:
        if any(existing.get(key) != item for key, item in witness.items()):
            raise ValueError("ESP_REBOOT_WITNESS_CHANGED")
        return value
    witness.update(received_at=now.isoformat(), envelope_sent_at=envelope_sent_at.isoformat(),
                   envelope_boot_id=envelope_boot_id)
    return {**value, "witness": witness}


def apply_reboot_update(session, *, connector, command, status, result, error_code,
                        envelope_boot_id=None, envelope_sent_at=None):
    """Only an authenticated boot envelope can acknowledge a reset witness."""
    lock_reboot_command(session, command)
    run = _run_for_command(session, command, lock=True)
    value = _control(run, command) if run else None
    if value is None:
        raise ValueError("ESP_REBOOT_CONTROL_BINDING_INVALID")
    if status == "SUCCEEDED":
        raise ValueError("ESP_REBOOT_GENERIC_SUCCESS_IS_NOT_PROOF")
    if not envelope_boot_id or envelope_sent_at is None:
        raise ValueError("ESP_REBOOT_AUTHENTICATED_BOOT_ENVELOPE_REQUIRED")
    now = utc_now()
    canceled = command.status in {"CANCEL_REQUESTED", "CANCELLED"} or bool(value.get("cancel_requested_at"))
    closed = value["outcome"] != "NOT_EVALUATED" or command.status in {"SUCCEEDED", "FAILED", "EXPIRED"}
    preboot_intent = (status == "RUNNING" and result.get("recovered") is False
        and result.get("safe_checkpoint") is False and result.get("outcome") != "NOT_OBSERVED")
    if (status == "ACKNOWLEDGED" or preboot_intent) and (
            canceled or closed or not _action_window_open(value, now)):
        # Firmware waits for this authenticated receipt before proceeding to
        # its restart gate. A successful HTTP/WS receipt here would authorize
        # an action even if we silently retained the canceled database state.
        raise ValueError("ESP_REBOOT_ACTION_AUTHORIZATION_CLOSED")
    if canceled or closed:
        if status == "RUNNING" and result.get("recovered") is True:
            if not _witness_valid(value, result, envelope_boot_id=envelope_boot_id,
                                  sent_at=envelope_sent_at, received_at=now, within_observation=False):
                raise ValueError("ESP_REBOOT_WITNESS_INVALID")
            updated = _record_witness(value, result, now=now, envelope_sent_at=envelope_sent_at,
                                      envelope_boot_id=envelope_boot_id)
            if updated is not value:
                value = updated
                run.result = {**run.result, KEY: value}
                _event(session, run, "HIL_ESP_REBOOT_CLOSED_WITNESS", {"command_id": command.command_id,
                    "witness": value.get("witness"), "outcome": value["outcome"]})
        elif status == "RUNNING":
            negative_cleanup = (result.get("outcome") == "NOT_OBSERVED"
                and _witness_valid(value, result, envelope_boot_id=envelope_boot_id,
                    sent_at=envelope_sent_at, received_at=now, within_observation=False))
            cancellation_busy = canceled and not result and error_code == "COMMAND_ALREADY_RUNNING"
            if not negative_cleanup and not cancellation_busy:
                raise ValueError("ESP_REBOOT_ACTION_AUTHORIZATION_CLOSED")
        if canceled and status == "CANCELLED":
            if command.status != "CANCELLED" or not value.get("cancel_acknowledged_at"):
                value = {**value, "outcome": "INCOMPLETE", "cancel_acknowledged_at": now.isoformat()}
                run.result = {**run.result, KEY: value}
                command.status, command.completed_at = "CANCELLED", now
                session.add(DeviceCommandEvent(command_id=command.id, status="CANCELLED", details={"run_id": run.run_id}))
        return command
    _scope(session, connector, run)
    if status in {"FAILED", "EXPIRED", "CANCELLED"} and not value.get("intent_reported_at"):
        if envelope_boot_id != value["boot_before"]:
            raise ValueError("ESP_REBOOT_FAILURE_BOOT_MISMATCH")
        value = {**value, "device_error_code": error_code if isinstance(error_code, str)
                 and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error_code) else "UNCLASSIFIED"}
        _finish(session, run, command, value, outcome="FAILED", now=now, reason="ESP_REBOOT_DEVICE_REFUSED")
        return command
    if status == "ACKNOWLEDGED" and not result:
        if envelope_boot_id != value["boot_before"] or now >= _time(value["action_expires_at"]):
            raise ValueError("ESP_REBOOT_ACK_SCOPE_OR_DEADLINE")
        command.status, command.acknowledged_at = "ACKNOWLEDGED", command.acknowledged_at or now
        return command
    if status != "RUNNING" or not _witness_valid(value, result, envelope_boot_id=envelope_boot_id,
                                                sent_at=envelope_sent_at, received_at=now):
        raise ValueError("ESP_REBOOT_WITNESS_INVALID")
    if result.get("outcome") == "NOT_OBSERVED" and result.get("recovered") is False:
        value = {**value, "negative_report_at": now.isoformat(), "negative_report_boot_id": envelope_boot_id}
        _finish(session, run, command, value, outcome="INCOMPLETE", now=now, reason="ESP_REBOOT_WITNESS_NOT_OBSERVED")
        return command
    if result.get("recovered") is True:
        updated = _record_witness(value, result, now=now, envelope_sent_at=envelope_sent_at,
                                  envelope_boot_id=envelope_boot_id)
        if updated is value:
            return command
        value = updated
    else:
        if value.get("intent_reported_at"):
            return command
        value = {**value, "intent_reported_at": now.isoformat()}
    run.result = {**run.result, KEY: value}
    command.status, command.started_at = "RUNNING", command.started_at or now
    session.add(DeviceCommandEvent(command_id=command.id, status="RUNNING",
        details={"run_id": run.run_id, "intent_reported_at": value["intent_reported_at"],
                 "witness": value.get("witness")}))
    _event(session, run, "HIL_ESP_REBOOT_WITNESS" if result.get("recovered") else "HIL_ESP_REBOOT_INTENT",
           {"command_id": command.command_id, "witness": value.get("witness")})
    return command


def _healthy_recovery(session, connector, run, release, value, *, now, telemetry=None):
    witness = value.get("witness")
    try:
        valid = isinstance(witness, dict) and _witness_valid(value, witness,
            envelope_boot_id=witness.get("envelope_boot_id"), sent_at=_time(witness["envelope_sent_at"]),
            received_at=_time(witness["received_at"]))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        return None
    proxy = SimpleNamespace(baseline={**run.baseline, "boot_id": witness["boot_after"]},
                            target=run.target, release_identity=run.release_identity)
    try:
        row = _healthy(session, connector, proxy, release, now, telemetry=telemetry)
        diagnostics = row.payload.get("diagnostics") or {}
        recovered = max(ensure_utc(row.created_at), _time(witness["received_at"]))
        if (diagnostics.get("controlled_esp_reboot_v1") is not True
                or (diagnostics.get("journal_storage") or {}).get("hil_reboot_persistence_incident") is not False
                or not _time(value["issued_at"]) <= ensure_utc(row.created_at) <= _time(value["recovery_deadline"])
                or recovered > _time(value["recovery_deadline"])):
            return None
        return row, recovered
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def advance_reboot_command(session, command, *, now=None, telemetry=None):
    """Do not expire recovery evidence at the shorter action deadline."""
    now = now or utc_now()
    connector = session.get(Connector, command.connector_id)
    lock_reboot_command(session, command)
    run = _run_for_command(session, command, lock=True)
    value = _control(run, command) if run else None
    if value is None:
        return
    if command.status in {"CANCEL_REQUESTED", "CANCELLED"} or value.get("cancel_requested_at"):
        if command.status == "CANCEL_REQUESTED" and not _cancellation_window_open(value, now):
            command.status, command.completed_at = "CANCELLED", now
            session.add(DeviceCommandEvent(command_id=command.id, status="CANCELLED",
                details={"run_id": run.run_id, "reason": "ESP_REBOOT_CANCEL_OBSERVATION_EXPIRED"}))
        return
    if value.get("outcome") != "NOT_EVALUATED":
        return
    clock = server_clock()
    if (clock is None or clock[0] != value["server_boot_id"] or clock[1] < value["started_monotonic_ms"]
            or now < _time(value["issued_at"])):
        _finish(session, run, command, value, outcome="INCOMPLETE", now=now, reason="ESP_REBOOT_SERVER_CLOCK_CHANGED")
        return
    try:
        release = _scope(session, connector, run)
    except ValueError:
        _finish(session, run, command, value, outcome="INCOMPLETE", now=now, reason="ESP_REBOOT_SCOPE_CHANGED")
        return
    proof = _healthy_recovery(session, connector, run, release, value, now=now, telemetry=telemetry)
    if proof:
        row, recovered = proof
        if row.id is None:
            session.flush()
        value = {**value, "recovered_at": recovered.isoformat(), "recovery_telemetry_id": row.id}
        _finish(session, run, command, value, outcome="SUCCEEDED", now=now)
    elif now >= _time(value["recovery_deadline"]) or clock[1] >= value["recovery_monotonic_ms"]:
        _finish(session, run, command, value, outcome="INCOMPLETE", now=now, reason="ESP_REBOOT_RECOVERY_NOT_OBSERVED")


def observe_reboot_heartbeat(session, connector, telemetry, *, now):
    command = session.scalar(select(DeviceCommand).where(DeviceCommand.connector_id == connector.id,
        DeviceCommand.command_type == COMMAND, DeviceCommand.status == "RUNNING").order_by(DeviceCommand.id.desc()).limit(1))
    if command is not None:
        advance_reboot_command(session, command, now=now, telemetry=telemetry)


def collect_reboot_evidence(session, run, now):
    """Read a completed authoritative control; never create a success result."""
    value = _control(run)
    if value is None or value.get("outcome") != "SUCCEEDED":
        return {"test": None, "reasons": ["ESP_REBOOT_RECOVERY_EVIDENCE_INCOMPLETE"]}
    command = session.scalar(select(DeviceCommand).where(DeviceCommand.command_id == value["command_id"]))
    connector = session.get(Connector, run.connector_id)
    release = session.get(FirmwareRelease, run.release_id)
    telemetry = session.get(DeviceTelemetry, value.get("recovery_telemetry_id"))
    try:
        scope_valid = (connector is not None and connector.firmware_family == "zkt"
            and target_matches(HilTarget.model_validate(run.target), connector)
            and release is not None and release.version == "2.7.0"
            and _release_identity(release).model_dump(mode="json") == run.release_identity)
    except (AttributeError, KeyError, TypeError, ValueError):
        scope_valid = False
    if (command is None or _control(run, command) is None or command.status != "SUCCEEDED"
            or not scope_valid or telemetry is None or telemetry.connector_id != connector.id
            or not isinstance(command.result, dict) or command.result.get("run_id") != run.run_id
            or command.result.get("outcome") != "SUCCEEDED"
            or command.result.get("recovery_telemetry_id") != value.get("recovery_telemetry_id")
            or command.result.get("recovered_at") != value.get("recovered_at")
            or not value.get("intent_reported_at") or not value.get("recovered_at")):
        return {"test": None, "reasons": ["ESP_REBOOT_RECOVERY_BINDING_CHANGED"]}
    proof = _healthy_recovery(session, connector, run, release, value,
                              now=ensure_utc(telemetry.created_at), telemetry=telemetry)
    if proof is None or proof[1] != _time(value["recovered_at"]) or proof[1] > now:
        return {"test": None, "reasons": ["ESP_REBOOT_HEALTHY_RECOVERY_UNVERIFIED"]}
    witness = value["witness"]
    return {"test": RecoveryTest(command_id=command.command_id, run_id=run.run_id, kind=COMMAND,
        target=HilTarget.model_validate(run.target), issued_at=_time(value["issued_at"]),
        started_at=max(_time(value["issued_at"]), datetime.fromtimestamp(witness["safe_checkpoint_epoch"], timezone.utc)),
        expires_at=_time(value["action_expires_at"]), recovered_at=_time(value["recovered_at"]),
        outcome="SUCCEEDED", durable_dedup_verified=True, safe_checkpoint_verified=True,
        boot_before=value["boot_before"], boot_after=witness["boot_after"]), "reasons": []}

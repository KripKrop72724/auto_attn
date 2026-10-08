"""Isolated exact-device reboot controls; no network or real device commands."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from threading import Event
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_zkt_source_load import store  # noqa: F401
from zk_add import hil_reboot, service, web, worker
from zk_add.crypto import encrypt_json
from zk_add.hil_runs import _release_identity
from zk_add.hil_reboot import (
    KEY, advance_reboot_command, collect_reboot_evidence, reboot_dispatch_allowed, start_reboot,
)
from zk_add.models import Connector, DeviceCommand, DeviceCommandEvent, DeviceTelemetry, TemporaryAdminLease
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease, OTA_LAYOUT
from zk_add.schemas import Envelope, FirmwareDiagnostics, HeartbeatPayload
from zk_add.service import apply_command_update, serialize_command
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt270_scope import TARGETS


@pytest.fixture
def observed(store, monkeypatch):  # noqa: F811
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    target = TARGETS[0].identity
    now = utc_now().replace(microsecond=0)
    clock = {"now": now, "boot": "synthetic-server-boot"}
    monkeypatch.setattr(hil_reboot, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(hil_reboot, "server_clock", lambda: (
        clock["boot"], 60000 + int((clock["now"] - now).total_seconds() * 1000)))
    monkeypatch.setattr("zk_add.db.SessionLocal", store)
    with store() as db:
        connector = db.scalar(select(Connector))
        connector.connector_id, connector.hardware_id = target.connector_id, target.mac
        connector.zkt_custody_enabled = connector.connected = True
        connector.boot_id = "synthetic-boot-before"
        connector.firmware_family = "zkt"
        terminal = connector.zkt_device
        terminal.online, terminal.connection_state = True, "ONLINE"
        terminal.serial = terminal.expected_serial = terminal.confirmed_serial = target.terminal_serial
        terminal.terminal_binding_state = "CONFIRMED"
        release = FirmwareRelease(release_id="synthetic-writer", version="2.7.0", git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1024, signing_key_id="synthetic", partition_layout=OTA_LAYOUT,
            minimum_bootstrap_version="2.6.19", storage_name="synthetic.bin", manifest_signature="synthetic",
            state="HIL_ONLY", manifest={"application_sha256": "c" * 64, "runtime_profile": "ZKT_JOURNAL_V1"})
        db.add(release)
        db.flush()
        campaign = FirmwareCampaign(campaign_id="synthetic", release_id=release.id, zone_id=connector.zone_id,
            actor="test", idempotency_key="synthetic", reason="Synthetic control test", typed_confirmation="2.7.0")
        db.add(campaign)
        db.flush()
        deployment = FirmwareDeployment(deployment_id="synthetic", campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, target_version="2.7.0", status="SUCCEEDED")
        db.add(deployment)
        db.flush()
        diagnostics = dict(schema_version=2, runtime_profile="ZKT_JOURNAL_V1", journal_format=1,
            delivery_authority="ADD", boot_id=connector.boot_id, sampled_uptime_ms=100000,
            controlled_esp_reboot_v1=True, source_generation=1, committed_source_cursor=100,
            storage=dict(durability="HEALTHY", persistence_verified=True, recovery_complete=True, upgrade_ready=True),
            workers=[dict(name=name, state="RUNNING", last_activity_uptime_ms=100000)
                for name in ("capture", "storage_owner", "add_delivery")],
            queues=[dict(name=name, count_known=True, records=0) for name in ("journal", "legacy_migration")],
            journal_storage=dict(observed=True, fresh=True, ready=True, durability="HEALTHY",
                hil_reboot_persistence_incident=False,
                checkpoint_recovery_pending=False, sampled_uptime_ms=100000,
                mailbox_capacity=12, mailbox_high_watermark=0, pending_appends=0))
        telemetry = DeviceTelemetry(connector_id=connector.id, boot_id=connector.boot_id,
            sequence=10, uptime_seconds=100, created_at=now, payload={
                "_trusted_envelope_sent_at": now.isoformat(), "diagnostics": diagnostics,
                "ota": {"running_version": "2.7.0", "image_sha256": "c" * 64,
                    "running_partition": "ota_1", "secure_boot": True, "rollback_enabled": True},
                "zkt": {"serial": target.terminal_serial, "online": True, "attendance_count": 100}})
        db.add(telemetry)
        db.flush()
        run = FirmwareHilRun(run_id=str(uuid4()), deployment_id=deployment.id, connector_id=connector.id,
            release_id=release.id, actor="test", idempotency_key="observation", target=target.model_dump(),
            release_identity=_release_identity(release).model_dump(mode="json"),
            started_at=now-timedelta(minutes=5), ends_at=now+timedelta(minutes=10), result={},
            baseline={"profile": "FULL_REMOTE_HIL_V1", "boot_id": connector.boot_id,
                      "source_generation": 1, "source_cursor": 100, "telemetry_id": telemetry.id})
        db.add(run)
        db.commit()
        identity = (run.run_id, connector.id)
    return store, clock, identity


def begin(db, observed, **kwargs):
    return start_reboot(db, observed[2][0], **{"actor": "admin", "idempotency_key": "reboot-unique", **kwargs})


def command_and_run(db, observed):
    command = db.scalar(select(DeviceCommand).where(DeviceCommand.command_type == "ESP_REBOOT"))
    run = db.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == observed[2][0]))
    return command, run


def core(value):
    return dict(schema_version=1, kind="ESP_REBOOT", command_id=value["command_id"], run_id=value["run_id"],
        boot_before=value["boot_before"], application_sha256=value["application_sha256"],
        terminal_serial=value["terminal_serial"], expires_at=value["expires_epoch"], attempted=True,
        intent_persisted=True, safe_checkpoint=False, recovered=False)


def report(db, observed, value, *, result=None, status="RUNNING", boot=None, **kwargs):
    return apply_command_update(db, connector=db.get(Connector, observed[2][1]), command_id=value["command_id"],
        status=status, result=core(value) if result is None else result, error_code=None, error_message=None,
        envelope_boot_id=boot or value["boot_before"], envelope_sent_at=observed[1]["now"], **kwargs)


def witness(value):
    return {**core(value), "boot_after": "synthetic-boot-after", "safe_checkpoint": True, "recovered": True,
        "reset_reason": 3, "safe_checkpoint_epoch": value["expires_epoch"] - 50,
        "safe_checkpoint_uptime_ms": value["baseline_uptime_ms"] + 10000}


def new_boot(db, observed, value, *, healthy=True):
    connector = db.get(Connector, observed[2][1])
    connector.boot_id = "synthetic-boot-after"
    before = db.get(DeviceTelemetry, value["baseline_telemetry_id"])
    payload = deepcopy(before.payload)
    payload["_trusted_envelope_sent_at"] = observed[1]["now"].isoformat()
    diag = payload["diagnostics"]
    diag["boot_id"], diag["sampled_uptime_ms"] = connector.boot_id, 10000
    diag["journal_storage"]["sampled_uptime_ms"] = 10000
    for item in diag["workers"]:
        item["last_activity_uptime_ms"] = 10000
    diag["storage"]["persistence_verified"] = healthy
    row = DeviceTelemetry(connector_id=connector.id, boot_id=connector.boot_id, sequence=1,
        uptime_seconds=10, created_at=observed[1]["now"], payload=payload)
    db.add(row)
    db.commit()
    return row


def test_control_and_command_are_durable_idempotent_and_replays_never_extend_deadline(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, created = begin(db, observed)
        assert created
        command, run = command_and_run(db, observed)
        envelope = serialize_command(command)
        assert envelope["expires_epoch"] == value["expires_epoch"] == envelope["payload"]["expires_at"]
        assert envelope["expected_state"] == {"serial": value["terminal_serial"]}
        assert reboot_dispatch_allowed(db, command)
        db.commit()
    clock["now"] += timedelta(minutes=20)
    with factory() as db:
        replay, created = begin(db, observed)
        assert not created and replay == value
        assert len(db.scalars(select(DeviceCommand)).all()) == 1
        assert command_and_run(db, observed)[1].status == "OBSERVING"
        with pytest.raises(ValueError, match="different"):
            begin(db, observed, idempotency_key="different-key")


@pytest.mark.parametrize("fault", ["capability", "stale", "image", "boot", "queue", "storage", "lease", "command",
    "persistence-incident", "persistence-unknown", "persistence-nonboolean",
    "interruption", "superseded", "revoked", "minute-before", "minute-after", "server-clock"])
def test_start_rejects_unsafe_or_wrong_scope_without_creating_command(observed, fault, monkeypatch):
    factory, clock, identity = observed
    with factory() as db:
        connector = db.get(Connector, identity[1])
        run = command_and_run(db, observed)[1]
        telemetry = db.scalar(select(DeviceTelemetry))
        payload = deepcopy(telemetry.payload)
        if fault == "capability":
            payload["diagnostics"].pop("controlled_esp_reboot_v1")
        elif fault == "stale":
            telemetry.created_at -= timedelta(seconds=46)
        elif fault == "image":
            payload["ota"]["image_sha256"] = "d" * 64
        elif fault == "boot":
            connector.boot_id = "different"
        elif fault == "queue":
            payload["diagnostics"]["queues"][0]["count_known"] = False
        elif fault == "storage":
            payload["diagnostics"]["storage"]["persistence_verified"] = False
        elif fault.startswith("persistence-"):
            journal = payload["diagnostics"]["journal_storage"]
            if fault == "persistence-unknown":
                journal.pop("hil_reboot_persistence_incident")
            else:
                journal["hil_reboot_persistence_incident"] = True if fault == "persistence-incident" else 0
        elif fault == "lease":
            # No user mutation: a synthetically unresolved lease is sufficient.
            from zk_add.models import DeviceUser
            user = DeviceUser(zkt_device_id=connector.zkt_device.id, uid="1", user_id="1", display_name="synthetic")
            db.add(user)
            db.flush()
            db.add(TemporaryAdminLease(lease_id=str(uuid4()), zkt_device_id=connector.zkt_device.id,
                device_user_id=user.id, state="ACTIVE", original_privilege=0,
                expires_at=clock["now"]+timedelta(minutes=1)))
        elif fault == "command":
            db.add(DeviceCommand(command_id=str(uuid4()), connector_id=connector.id, command_type="REFRESH_USERS",
                payload_encrypted=encrypt_json({}), expected_state_encrypted=encrypt_json({}),
                desired_state_encrypted=encrypt_json({}), idempotency_key="other", actor="test"))
        elif fault == "interruption":
            run.result = {"add_interruption": {"transport_restored_at": None}}
        elif fault == "superseded":
            old = db.get(FirmwareDeployment, run.deployment_id)
            db.add(FirmwareDeployment(deployment_id="newer", campaign_id=old.campaign_id, release_id=run.release_id,
                connector_id=connector.id, target_version="2.7.0", status="OFFERED"))
        elif fault == "revoked":
            db.get(FirmwareRelease, run.release_id).revoked_at = clock["now"]
        elif fault.startswith("minute-"):
            clock["now"] += timedelta(seconds=-1 if fault == "minute-before" else 60)
        else:
            monkeypatch.setattr(hil_reboot, "server_clock", lambda: None)
        telemetry.payload = payload
        db.commit()
        with pytest.raises(ValueError):
            begin(db, observed)
        db.rollback()
        assert not db.scalar(select(DeviceCommand.id).where(DeviceCommand.command_type == "ESP_REBOOT"))
        assert KEY not in command_and_run(db, observed)[1].result


def test_action_deadline_stops_every_dispatch_path_but_preserves_recovery_evidence(observed, monkeypatch):
    factory, clock, identity = observed
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
        command, run = command_and_run(db, observed)
        report(db, observed, value)
        db.commit()
        assert not reboot_dispatch_allowed(db, command)  # Committed intent consumes dispatch.
        clock["now"] += timedelta(seconds=61)
        advance_reboot_command(db, command, now=clock["now"])
        db.commit()
        assert command.status == "RUNNING" and run.result[KEY]["outcome"] == "NOT_EVALUATED"
        assert not reboot_dispatch_allowed(db, command)
        assert hil_reboot.refresh_reboot_dispatch(command.command_id) is None
        assert web.poll_commands((db, db.get(Connector, identity[1]))) == {"commands": []}
        assert web.pending_command_payloads(db.get(Connector, identity[1]).connector_id) == []
        clock["now"] += timedelta(seconds=120)
        advance_reboot_command(db, command, now=clock["now"])
        db.commit()
        assert command.status == "EXPIRED" and run.result[KEY]["outcome"] == "INCOMPLETE"
        assert run.result[KEY]["intent_reported_at"]


def test_server_restart_or_wall_clock_regression_cannot_extend_action(observed):
    factory, clock, _ = observed
    with factory() as db:
        begin(db, observed)
        db.commit()
        command, _ = command_and_run(db, observed)
        clock["boot"] = "changed-server-boot"
        assert not reboot_dispatch_allowed(db, command)
        clock["boot"] = "synthetic-server-boot"
        clock["now"] -= timedelta(seconds=1)
        assert not reboot_dispatch_allowed(db, command)


def test_worker_rechecks_a_queued_send_after_action_deadline(observed, monkeypatch):
    factory, clock, identity = observed
    with factory() as db:
        begin(db, observed)
        db.commit()
        command, _ = command_and_run(db, observed)
        stale = serialize_command(command)
        connector_id = db.get(Connector, identity[1]).connector_id
    clock["now"] += timedelta(seconds=61)
    monkeypatch.setattr(worker, "prepare_maintenance_tick", lambda now: (
        [(connector_id, stale)], [], [], [], []))
    sent = []
    async def capture(*args):
        sent.append(args)
        return True
    monkeypatch.setattr(worker.connector_hub, "send", capture)
    asyncio.run(worker.maintenance_tick())
    assert sent == []
    worker.mark_command_dispatched(stale["command_id"])
    with factory() as db:
        command, _ = command_and_run(db, observed)
        assert command.attempt_count == 0 and command.status == "QUEUED"


def test_intent_and_boot_change_without_witness_cannot_pass(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        new_boot(db, observed, value)
        command, run = command_and_run(db, observed)
        advance_reboot_command(db, command, now=clock["now"])
        db.commit()
        assert command.status == "RUNNING"
        assert collect_reboot_evidence(db, run, clock["now"])["test"] is None
        with pytest.raises(ValueError, match="GENERIC_SUCCESS"):
            report(db, observed, value, status="SUCCEEDED", result={}, boot="synthetic-boot-after")


def test_processed_replay_without_witness_is_acknowledged_as_incomplete(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=61)
        new_boot(db, observed, value)
        negative = {**core(value), "boot_after": "synthetic-boot-after", "outcome": "NOT_OBSERVED",
                    "reset_reason": 1, "safe_checkpoint_epoch": 0, "safe_checkpoint_uptime_ms": 0}
        report(db, observed, value, result=negative, boot="synthetic-boot-after")
        db.commit()
        command, run = command_and_run(db, observed)
        assert command.status == "EXPIRED" and run.result[KEY]["outcome"] == "INCOMPLETE"
        assert collect_reboot_evidence(db, run, clock["now"])["test"] is None
        assert not reboot_dispatch_allowed(db, command)


def test_matched_idle_gate_witness_and_new_boot_health_produce_control_evidence_only(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        row = new_boot(db, observed, value)
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        command, run = command_and_run(db, observed)
        assert command.status == "RUNNING"
        advance_reboot_command(db, command, now=clock["now"], telemetry=row)
        db.commit()
        assert command.status == "SUCCEEDED" and run.status == "OBSERVING"
        assert run.result[KEY]["recovery_telemetry_id"] == row.id
        evidence = collect_reboot_evidence(db, run, clock["now"])
        assert evidence["reasons"] == []
        test = evidence["test"]
        assert test.safe_checkpoint_verified and test.durable_dedup_verified
        assert test.boot_before != test.boot_after and test.outcome == "SUCCEEDED"
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state == "HIL_ACCEPTED"))
        count = len(db.scalars(select(DeviceCommandEvent)).all())
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        assert len(db.scalars(select(DeviceCommandEvent)).all()) == count


def test_actual_envelope_and_heartbeat_paths_retain_capability_and_complete_bound_recovery(observed, monkeypatch):
    factory, clock, identity = observed
    monkeypatch.setattr(service, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(web, "utc_now", lambda: clock["now"])
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
        connector_id = db.get(Connector, identity[1]).connector_id
    def envelope(result, boot, sequence):
        return Envelope(connector_id=connector_id, message_id=str(uuid4()), boot_id=boot,
            seq=sequence, sent_at=clock["now"], type="command_update",
            payload={"command_id": value["command_id"], "status": "RUNNING", "result": result})
    assert web.persist_envelope(identity[1], envelope(core(value), value["boot_before"], 20)).ack["type"] == "ack"
    clock["now"] += timedelta(seconds=20)
    assert web.persist_envelope(identity[1], envelope(witness(value), "synthetic-boot-after", 1)).ack["type"] == "ack"
    with factory() as db:
        connector = db.get(Connector, identity[1])
        payload = deepcopy(db.get(DeviceTelemetry, value["baseline_telemetry_id"]).payload)
        payload.pop("_trusted_envelope_sent_at")
        payload["diagnostics"]["boot_id"] = "synthetic-boot-after"
        # Using a new-boot uptime distinct from the retained old-boot witness.
        payload["diagnostics"]["sampled_uptime_ms"] = 10000
        payload["diagnostics"]["journal_storage"]["sampled_uptime_ms"] = 10000
        for item in payload["diagnostics"]["workers"]:
            item["last_activity_uptime_ms"] = 10000
        service.update_heartbeat(db, connector=connector, boot_id="synthetic-boot-after", sequence=2,
            payload=HeartbeatPayload(**payload, uptime_seconds=10), device_sent_at=clock["now"])
        db.commit()
        command, run = command_and_run(db, observed)
        assert command.status == "SUCCEEDED" and run.result[KEY]["outcome"] == "SUCCEEDED"
        latest = db.get(DeviceTelemetry, run.result[KEY]["recovery_telemetry_id"])
        assert latest.payload["diagnostics"]["controlled_esp_reboot_v1"] is True


@pytest.mark.parametrize("fault", ["run", "command", "digest", "terminal", "reset", "same-boot", "checkpoint",
    "uptime", "not-persisted", "pre-gate", "missing-intent", "unsigned-body", "late"])
def test_witness_is_bound_to_the_command_actual_envelope_and_idle_gate(observed, fault):
    factory, clock, identity = observed
    with factory() as db:
        value, _ = begin(db, observed)
        if fault != "missing-intent":
            report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        new_boot(db, observed, value)
        result = witness(value)
        field_values = {"run": ("run_id", str(uuid4())), "command": ("command_id", str(uuid4())),
            "digest": ("application_sha256", "d"*64), "terminal": ("terminal_serial", "OTHER"),
            "reset": ("reset_reason", 1), "same-boot": ("boot_after", value["boot_before"]),
            "checkpoint": ("safe_checkpoint_epoch", value["expires_epoch"]),
            "uptime": ("safe_checkpoint_uptime_ms", 1), "not-persisted": ("intent_persisted", False),
            "pre-gate": ("safe_checkpoint", False)}
        if fault in field_values:
            field, item = field_values[fault]
            result[field] = item
        if fault == "late":
            clock["now"] += timedelta(minutes=3)
        with pytest.raises(ValueError):
            if fault == "unsigned-body":
                apply_command_update(db, connector=db.get(Connector, identity[1]), command_id=value["command_id"],
                    status="RUNNING", result=result, error_code=None, error_message=None)
            else:
                report(db, observed, value, result=result, boot="synthetic-boot-after")
        db.rollback()
        assert not command_and_run(db, observed)[1].result[KEY]["witness"]


def test_unhealthy_recovery_keeps_witness_and_retries_health_without_rebooting_again(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        bad = new_boot(db, observed, value, healthy=False)
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        command, run = command_and_run(db, observed)
        advance_reboot_command(db, command, now=clock["now"], telemetry=bad)
        db.commit()
        assert command.status == "RUNNING" and run.result[KEY]["witness"]
        assert not reboot_dispatch_allowed(db, command)
        clock["now"] += timedelta(seconds=10)
        healthy = new_boot(db, observed, value)
        hil_reboot.observe_reboot_heartbeat(db, db.get(Connector, command.connector_id), healthy, now=clock["now"])
        db.commit()
        assert command.status == "SUCCEEDED"


@pytest.mark.parametrize("fault", ["command-json", "witness", "timestamp", "telemetry", "command-result"])
def test_malformed_stored_recovery_evidence_is_explicitly_unverified(observed, fault):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        telemetry = new_boot(db, observed, value)
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        command, run = command_and_run(db, observed)
        advance_reboot_command(db, command, now=clock["now"], telemetry=telemetry)
        db.commit()
        assert command.status == "SUCCEEDED"
        if fault == "command-json":
            command.payload_encrypted = "corrupt-ciphertext"
        elif fault == "command-result":
            command.result = {**command.result, "run_id": "different"}
        elif fault == "telemetry":
            payload = deepcopy(telemetry.payload)
            payload["diagnostics"]["workers"] = ["invalid"]
            telemetry.payload = payload
        else:
            changed = deepcopy(run.result)
            if fault == "witness":
                changed[KEY]["witness"] = "invalid"
            else:
                changed[KEY]["recovered_at"] = "invalid-date"
            run.result = changed
        db.commit()
        result = collect_reboot_evidence(db, run, clock["now"])
        assert result["test"] is None and result["reasons"]


def test_clock_regression_expires_control_without_losing_intent_evidence(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        clock["now"] -= timedelta(seconds=1)
        command, run = command_and_run(db, observed)
        advance_reboot_command(db, command, now=clock["now"])
        db.commit()
        assert command.status == "EXPIRED"
        assert run.result[KEY]["intent_reported_at"]
        assert run.result[KEY]["reason"] == "ESP_REBOOT_SERVER_CLOCK_CHANGED"


def test_latched_persistence_incident_blocks_dispatch_and_recovery(observed):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        command, _ = command_and_run(db, observed)
        telemetry = db.get(DeviceTelemetry, value["baseline_telemetry_id"])
        payload = deepcopy(telemetry.payload)
        payload["diagnostics"]["journal_storage"]["hil_reboot_persistence_incident"] = True
        telemetry.payload = payload
        db.commit()
        assert not reboot_dispatch_allowed(db, command)
        # An already dispatched attempt can report its witness; a subsequent
        # failure in the new boot must still prevent recovered status.
        report(db, observed, value)
        db.commit()
        clock["now"] += timedelta(seconds=20)
        row = new_boot(db, observed, value)
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        advance_reboot_command(db, command, now=clock["now"], telemetry=row)
        db.commit()
        assert command.status == "RUNNING"
        assert collect_reboot_evidence(db, command_and_run(db, observed)[1], clock["now"])["test"] is None


def test_concurrent_start_commits_one_control_and_one_command(observed):
    factory, _, _ = observed
    def start():
        with factory() as db:
            try:
                result = begin(db, observed)
                db.commit()
                return result[1]
            except ValueError:
                db.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: start(), range(2))) == 1
    with factory() as db:
        assert len(db.scalars(select(DeviceCommand)).all()) == 1


def test_capability_is_a_strict_boolean_and_legacy_diagnostics_stay_compatible():
    assert FirmwareDiagnostics(schema_version=2).controlled_esp_reboot_v1 is None
    assert FirmwareDiagnostics(schema_version=2, controlled_esp_reboot_v1=True).controlled_esp_reboot_v1
    for value in ("true", 1):
        with pytest.raises(ValueError):
            FirmwareDiagnostics(schema_version=2, controlled_esp_reboot_v1=value)


def test_persistence_incident_is_strict_boolean_but_legacy_schema_remains_compatible():
    from zk_add.schemas import JournalStorageDiagnostics
    fields = dict(observed=True, fresh=True, ready=True, durability="HEALTHY", checkpoint_recovery_pending=False,
        mailbox_capacity=12, mailbox_high_watermark=0, pending_appends=0)
    assert JournalStorageDiagnostics(**fields).hil_reboot_persistence_incident is None
    assert JournalStorageDiagnostics(**fields, hil_reboot_persistence_incident=False).hil_reboot_persistence_incident is False
    for value in ("false", 0, "true", 1):
        with pytest.raises(ValueError):
            JournalStorageDiagnostics(**fields, hil_reboot_persistence_incident=value)


def test_cancel_between_socket_offer_and_dispatch_bookkeeping_cannot_authorize_intent(observed, monkeypatch):
    factory, clock, identity = observed
    monkeypatch.setattr(web.connector_hub, "send", AsyncMock(return_value=True))
    monkeypatch.setattr(web.browser_events, "publish", AsyncMock())
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
        command, _ = command_and_run(db, observed)
        offered = serialize_command(command)
        # The socket may have offered this while the DB still says QUEUED.
        assert command.attempt_count == 0 and offered["type"] == "command"
        response = asyncio.run(web.cancel_command(command.command_id, (db, SimpleNamespace(username="admin"))))
        assert response["status"] == "CANCEL_REQUESTED"
    cancellation = {"schema_version": "2", "type": "command_cancel", "command_id": value["command_id"]}
    assert web.connector_hub.send.call_args.args[1] == cancellation
    web.mark_command_dispatched(value["command_id"])
    worker.mark_command_dispatched(value["command_id"])
    with factory() as db:
        command, run = command_and_run(db, observed)
        assert command.status == "CANCEL_REQUESTED" and run.result[KEY]["outcome"] == "INCOMPLETE"
        for status, result in (("ACKNOWLEDGED", {}), ("RUNNING", core(value)),
                               ("RUNNING", {}), ("RUNNING", {"kind": "ESP_REBOOT"})):
            with pytest.raises(ValueError, match="AUTHORIZATION_CLOSED"):
                report(db, observed, value, status=status, result=result)
        assert command.status == "CANCEL_REQUESTED" and not run.result[KEY]["intent_reported_at"]
        apply_command_update(db, connector=db.get(Connector, identity[1]), command_id=command.command_id,
            status="RUNNING", result={}, error_code="COMMAND_ALREADY_RUNNING", error_message=None,
            envelope_boot_id=value["boot_before"], envelope_sent_at=clock["now"])
        assert command.status == "CANCEL_REQUESTED"
        clock["now"] += timedelta(seconds=61)
        assert not reboot_dispatch_allowed(db, command)
        assert hil_reboot.refresh_reboot_dispatch(command.command_id) == cancellation
        connector = db.get(Connector, identity[1])
        assert web.poll_commands((db, connector)) == {"commands": [cancellation]}
        assert web.pending_command_payloads(connector.connector_id) == [cancellation]
        report(db, observed, value, status="CANCELLED", result={})
        db.commit()
        assert command.status == "CANCELLED"
        count = len(list(db.scalars(select(DeviceCommandEvent))))
        report(db, observed, value, status="CANCELLED", result={})
        db.commit()
        assert len(list(db.scalars(select(DeviceCommandEvent)))) == count
        with pytest.raises(ValueError, match="AUTHORIZATION_CLOSED"):
            report(db, observed, value)


def test_cancel_retry_outlives_action_expiry_without_extending_observation(observed, monkeypatch):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
        command, _ = command_and_run(db, observed)
        hil_reboot.request_reboot_cancellation(db, command, actor="admin")
        db.commit()
    clock["now"] += timedelta(seconds=61)
    dispatch = worker.prepare_maintenance_tick(clock["now"])[0]
    assert len(dispatch) == 1 and dispatch[0][1]["type"] == "command_cancel"
    assert dispatch[0][1]["command_id"] == value["command_id"]
    clock["now"] += timedelta(seconds=120)
    assert worker.prepare_maintenance_tick(clock["now"])[0] == []
    with factory() as db:
        command, run = command_and_run(db, observed)
        assert command.status == "CANCELLED" and run.result[KEY]["outcome"] == "INCOMPLETE"


@pytest.mark.parametrize("delay", [20, 240])
def test_canceled_test_retains_already_happened_witness_but_never_new_success(observed, delay):
    factory, clock, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        report(db, observed, value)
        db.commit()
        command, run = command_and_run(db, observed)
        hil_reboot.request_reboot_cancellation(db, command, actor="admin")
        db.commit()
        clock["now"] += timedelta(seconds=delay)
        if delay > 180:
            advance_reboot_command(db, command, now=clock["now"])
            db.commit()
        row = new_boot(db, observed, value)
        report(db, observed, value, result=witness(value), boot="synthetic-boot-after")
        db.commit()
        advance_reboot_command(db, command, now=clock["now"], telemetry=row)
        db.commit()
        assert command.status == ("CANCELLED" if delay > 180 else "CANCEL_REQUESTED")
        assert run.result[KEY]["witness"]["recovered"]
        assert run.result[KEY]["outcome"] == "INCOMPLETE"
        assert collect_reboot_evidence(db, run, clock["now"])["test"] is None


@pytest.mark.parametrize("closure", ["cancel", "clock-change", "monotonic-expired"])
def test_replayed_or_lower_sequence_intent_cannot_bypass_closed_authorization(observed, monkeypatch, closure):
    factory, clock, identity = observed
    monkeypatch.setattr(web, "utc_now", lambda: clock["now"])
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
        connector = db.get(Connector, identity[1])
        connector.last_sequence = 25
        connector_id = connector.connector_id
        db.commit()
    intent = Envelope(connector_id=connector_id, message_id=str(uuid4()), boot_id=value["boot_before"],
        seq=20, sent_at=clock["now"], type="command_update",
        payload={"command_id": value["command_id"], "status": "RUNNING", "result": core(value)})
    assert web.persist_envelope(identity[1], intent).ack["type"] == "ack"
    assert web.persist_envelope(identity[1], intent).ack["type"] == "ack"
    with factory() as db:
        command, run = command_and_run(db, observed)
        assert run.result[KEY]["intent_reported_at"]  # Older sequence still had to commit intent.
        if closure == "cancel":
            hil_reboot.request_reboot_cancellation(db, command, actor="admin")
        elif closure == "clock-change":
            clock["boot"] = "different-server-boot"
            advance_reboot_command(db, command, now=clock["now"])
        else:
            monkeypatch.setattr(hil_reboot, "server_clock", lambda: ("synthetic-server-boot", 120000))
        db.commit()
    with pytest.raises(ValueError, match="AUTHORIZATION_CLOSED"):
        web.persist_envelope(identity[1], intent)


def test_command_state_loaded_before_cancel_is_refreshed_under_lock(observed):
    factory, _, _ = observed
    with factory() as db:
        value, _ = begin(db, observed)
        db.commit()
    with factory() as stale:
        command, _ = command_and_run(stale, observed)
        assert command.status == "QUEUED"
        stale.commit()
        with factory() as cancel:
            hil_reboot.request_reboot_cancellation(cancel, command_and_run(cancel, observed)[0], actor="admin")
            cancel.commit()
        with pytest.raises(ValueError, match="AUTHORIZATION_CLOSED"):
            report(stale, observed, value)
        assert command.status == "CANCEL_REQUESTED"


def test_postgres_cancel_commit_wins_against_waiting_intent_transaction(observed):
    factory, _, _ = observed
    with factory() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL row-lock race; SQLite stale-state path is tested separately")
        value, _ = begin(db, observed)
        db.commit()
    started = Event()
    def receive_intent():
        with factory() as incoming:
            command_and_run(incoming, observed)
            started.set()
            with pytest.raises(ValueError, match="AUTHORIZATION_CLOSED"):
                report(incoming, observed, value)
            incoming.rollback()
        return "rejected"
    with ThreadPoolExecutor(max_workers=1) as pool, factory() as cancel:
        hil_reboot.request_reboot_cancellation(cancel, command_and_run(cancel, observed)[0], actor="admin")
        cancel.flush()
        receiver = pool.submit(receive_intent)
        assert started.wait(timeout=5)
        cancel.commit()
        assert receiver.result(timeout=10) == "rejected"
    with factory() as db:
        command, run = command_and_run(db, observed)
        assert command.status == "CANCEL_REQUESTED" and not run.result[KEY]["intent_reported_at"]

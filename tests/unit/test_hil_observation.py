"""Committed synthetic ADD rows; no physical or production qualification claim."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sa_event, select

from reader_matrix_fixtures import admit, pinned, proof as reader_proof  # noqa: F401
from test_hil_writer_evidence import observed, prepared, seal_oracle, source_store, store  # noqa: F401
from zk_add import hil_observation as observation
from zk_add.attendance_repair import _protected_digest
from zk_add.hil_validation import ReleaseIdentity
from zk_add.hil_scope import HilTarget
from zk_add.models import (
    AttendanceEvent, Connector, DeviceAlert, DeviceCommand, DeviceLog, DeviceTelemetry, ReconciliationCoverage,
    ReconciliationJob, SourceTailChunk, TerminalRecordManifest, ZktOracleIntent,
    ZktOracleMembershipReceipt, ZktSourceAttendance,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareHilRun, FirmwareRelease
from zk_add.reconciliation import reconciliation_chain_digest
from zk_add.schemas import FirmwareDiagnostics
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_source_attendance import _facts_material


def telemetry_payload(run, *, boot, tick, cursor, stamp):
    value = {"_trusted_envelope_sent_at": stamp.isoformat(),
        "ota": {"running_version": "2.7.0", "image_sha256": run.release_identity["application_sha256"],
            "secure_boot": True, "rollback_enabled": True, "running_partition": "ota_1"},
        "zkt": {"serial": run.target["terminal_serial"], "online": True, "attendance_count": cursor,
            "connection_state": "ONLINE", "consecutive_failures": 0, "flap_count_15m": 0},
        "diagnostics": {"schema_version": 2, "journal_format": 1, "runtime_profile": "ZKT_JOURNAL_V1",
            "controlled_esp_reboot_v1": True,
            "delivery_authority": "ADD", "boot_id": boot, "sampled_uptime_ms": tick,
            "source_generation": 1, "committed_source_cursor": cursor,
            "storage": {"durability": "HEALTHY", "persistence_verified": True,
                "recovery_complete": True, "upgrade_ready": True, "write_failures": 0, "read_failures": 0},
            "journal_storage": {"observed": True, "fresh": True, "ready": True,
                "hil_reboot_persistence_incident": False,
                "mailbox_capacity": 8, "mailbox_high_watermark": 0, "pending_appends": 0,
                "durability": "HEALTHY", "checkpoint_recovery_pending": False, "sampled_uptime_ms": tick},
            "journal_runtime": {"observed": True, "phase": "READY", "reader_ready": True,
                "writer_ready": True, "delivery_authority": "ADD", "start_attempts": 3,
                "storage_starts": 1, "delivery_starts": 1, "capture_starts": 1,
                "proof_attempts": 1, "failures": 0, "compatibility": "", "sampled_uptime_ms": tick},
            "workers": [{"name": name, "state": "RUNNING", "last_activity_uptime_ms": tick,
                "restart_count": 0, "failures": 0, "timeouts": 0, "refusals": 0,
                "consecutive_failures": 0} for name in ("capture", "storage_owner", "add_delivery",
                    "legacy_add_delivery", "legacy_ords_delivery")],
            "queues": [{"name": name, "count_known": True, "records": 0}
                for name in ("journal", "legacy_migration")]}}
    if "reader_admission" in run.baseline:
        value["diagnostics"]["qualified_reader"] = reader_proof(run.baseline["reader_admission"], tick)
    value["diagnostics"]["workers"][0].update(execution_model="ON_DEMAND",
        sampled_uptime_ms=tick, pending_requests=0)
    return value


def startup_payload(payload, *, owner_observed):
    """The firmware emits this unfinished snapshot before probe/session ready.

    journal_diagnostics.c deliberately downgrades storage to DEGRADED while
    the fresh owner is recovering, without an I/O failure. UNKNOWN precedes
    the owner's first snapshot. The placeholder terminal count is still 0.
    """
    payload = deepcopy(payload)
    diag = payload["diagnostics"]
    if "qualified_reader" in diag:
        diag["qualified_reader"] = {key: diag["qualified_reader"][key] for key in ("schema_version", "matrix_sha256")}
        diag["qualified_reader"]["verified"] = False
    diag.pop("source_generation")
    diag.pop("committed_source_cursor")
    authority = "ADD" if owner_observed else "UNKNOWN"
    diag["delivery_authority"] = authority
    payload["zkt"].update(serial=payload["zkt"]["serial"] if owner_observed else "", online=False,
        attendance_count=0, connection_state="CONNECTING" if owner_observed else "BOOTING")
    diag["storage"].update(durability="DEGRADED" if owner_observed else "UNKNOWN",
        persistence_verified=False, recovery_complete=False)
    if not owner_observed:
        diag["storage"].pop("read_failures")
        diag["storage"].pop("write_failures")
    diag["journal_storage"].update(observed=owner_observed, fresh=owner_observed, ready=False,
        durability="DEGRADED" if owner_observed else "UNKNOWN")
    diag["journal_runtime"].update(observed=owner_observed, phase="RECOVERING" if owner_observed else "NOT_STARTED",
        reader_ready=False, writer_ready=False, delivery_authority=authority,
        start_attempts=2 if owner_observed else 0, storage_starts=int(owner_observed),
        delivery_starts=int(owner_observed), capture_starts=0, proof_attempts=0)
    if not owner_observed:
        diag["journal_storage"].pop("sampled_uptime_ms")
        diag["journal_runtime"].pop("sampled_uptime_ms")
    for worker in diag["workers"]:
        worker["state"] = ({"storage_owner": "WAITING_RESOURCE", "capture": "STOPPED",
            "add_delivery": "RUNNING"}.get(worker["name"], "STOPPED") if owner_observed else
            "STOPPED" if worker["name"].startswith("legacy_") else "UNKNOWN")
        if worker["state"] != "RUNNING":
            worker.pop("last_activity_uptime_ms")
    for queue in diag["queues"]:
        queue.pop("records")
        queue.update(count_known=False, count_reason=("STALE_OWNER" if not owner_observed else
            "NONEMPTY_OR_UNVERIFIED" if queue["name"] == "journal" else "UNVERIFIED_MIGRATION"))
    return payload


def interruption_control(run, start, telemetry_id):
    return {"schema_version": 1, "kind": "ADD_INTERRUPT_30S",
        "control_id": "synthetic-transport-control", "run_id": run.run_id,
        "started_at": (start + timedelta(seconds=180)).isoformat(),
        "expires_at": (start + timedelta(seconds=210)).isoformat(),
        "rejected_at": (start + timedelta(seconds=185)).isoformat(),
        "transport_restored_at": (start + timedelta(seconds=210)).isoformat(),
        "server_boot_id": "synthetic-server-boot", "started_monotonic_ms": 60000,
        "expires_monotonic_ms": 90000, "rejected_monotonic_ms": 65000,
        "restored_monotonic_ms": 90000, "expiry_reason": "DEADLINE",
        "boot_before": "boot-before", "boot_after_transport": "boot-before",
        "baseline_telemetry_id": telemetry_id}


@pytest.fixture
def full_rows(observed, monkeypatch, request, pinned):  # noqa: F811
    now = utc_now().replace(microsecond=0)
    start = now - timedelta(minutes=15)
    with observed() as db:
        seal_oracle(db)
        run = db.scalar(select(FirmwareHilRun))
        release = db.get(FirmwareRelease, run.release_id)
        deployment = db.get(FirmwareDeployment, run.deployment_id)
        selection = admit(db, release, deployment, pinned)
        baseline_proof = reader_proof(selection)
        baseline_proof.pop("sampled_uptime_ms")
        run.baseline = {**run.baseline, "reader_admission": selection, "qualified_reader": baseline_proof}
        run.run_id = str(uuid4())
        run.started_at, run.ends_at = start, now
        connector = db.scalar(select(Connector))
        connector.boot_id, connector.connected, connector.zkt_device.online = "boot-after", True, True
        job, coverage = db.scalar(select(ReconciliationJob)), db.scalar(select(ReconciliationCoverage))
        job.status, job.capture_certificate = "COMPLETED", {"synthetic": "committed baseline certificate"}
        baseline = {**run.baseline, "boot_id": "boot-before", "capture_certificate_sha256":
                    observation.evidence_digest(job.capture_certificate)}
        run.baseline = baseline
        # Split actual source-ingress records into two committed tail ranges at
        # synthetic receipt times. The protected source/derivation remains real.
        for chunk in db.scalars(select(SourceTailChunk)):
            db.delete(chunk)
        db.flush()
        chain = baseline["source_chain"]
        for index, row in enumerate(db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.ordinal))):
            stamp = start + timedelta(seconds=(220, 490)[index])
            row.created_at = stamp
            material = [{"disposition": row.declared_disposition, "event_uid": None,
                "occurrence_index": row.occurrence_index, "ordinal": row.ordinal,
                "raw_record_digest": row.raw_record_digest, "terminal_record_key": row.terminal_record_key}]
            digest = observation.evidence_digest(material)
            resulting = reconciliation_chain_digest(chain, start_ordinal=index, end_ordinal=index + 1, chunk_digest=digest)
            db.add(SourceTailChunk(coverage_id=coverage.id, connector_id=connector.id,
                zkt_device_id=connector.zkt_device.id, generation=1, start_ordinal=index, end_ordinal=index + 1,
                latest_terminal_count=index + 1, record_count=1, chunk_digest=digest,
                previous_chain_digest=chain, resulting_chain_digest=resulting, committed_at=stamp))
            chain = resulting
        coverage.source_committed_chain_digest = chain
        for index, event in enumerate(db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.id))):
            stamp = start + timedelta(seconds=(220, 490)[index])
            event.captured_at = stamp
            link = db.scalar(select(ZktSourceAttendance).where(ZktSourceAttendance.attendance_event_id == event.id))
            link.facts_digest = _protected_digest(_facts_material(event))
            intent = db.scalar(select(ZktOracleIntent).where(ZktOracleIntent.attendance_event_id == event.id))
            intent.created_at, intent.prepared_at = stamp, stamp + timedelta(seconds=1)
            receipt = db.scalar(select(ZktOracleMembershipReceipt).where(ZktOracleMembershipReceipt.intent_id == intent.id))
            receipt.verified_at = stamp + timedelta(seconds=2)
        samples = {}
        reboot_value = None
        if getattr(request, "param", False):
            from zk_add import hil_reboot, hil_transport
            from zk_add.service import apply_command_update
            from test_hil_reboot import core, witness
            # Isolated allowlist configuration, not an evidence/health stub.
            monkeypatch.setattr(hil_reboot, "BY_ID", {connector.connector_id:
                SimpleNamespace(identity=HilTarget.model_validate(run.target))})
            monkeypatch.setattr(hil_transport, "BY_ID", hil_reboot.BY_ID)
            clock = {"now": start}
            monkeypatch.setattr(hil_reboot, "utc_now", lambda: clock["now"])
            monkeypatch.setattr(hil_reboot, "server_clock", lambda: (
                "synthetic-server-boot", 60000 + int((clock["now"] - start).total_seconds() * 1000)))
            monkeypatch.setattr(hil_transport, "utc_now", lambda: clock["now"])
            monkeypatch.setattr(hil_transport, "server_clock", hil_reboot.server_clock)
        for seconds in range(0, 901, 30):
            if seconds == 180:  # The real enforced 30-second transport window.
                if getattr(request, "param", False):
                    clock["now"] = start + timedelta(seconds=180)
                    db.commit()
                    _, created = hil_transport.start_interruption(db, run.run_id,
                        actor="test", idempotency_key="synthetic-interruption")
                    assert created
                    db.commit()
                    clock["now"] += timedelta(seconds=5)
                    with pytest.raises(hil_transport.TransportInterrupted):
                        hil_transport.enforce_transport(db, connector, transport="HTTP")
                    clock["now"] = start + timedelta(seconds=210)
                    hil_transport.enforce_transport(db, connector, transport="HTTP")
                    db.commit()
                continue
            stamp = start + timedelta(seconds=seconds)
            boot = "boot-before" if seconds <= 330 else "boot-after"
            uptime = seconds + 100 if seconds <= 330 else seconds - 340
            cursor = 0 if seconds < 220 else 1 if seconds < 490 else 2
            row = DeviceTelemetry(connector_id=connector.id, boot_id=boot, sequence=uptime,
                uptime_seconds=uptime, created_at=stamp,
                payload=telemetry_payload(run, boot=boot, tick=uptime * 1000, cursor=cursor, stamp=stamp))
            if getattr(request, "param", False) == "startup" and seconds in (360, 390):
                row.payload = startup_payload(row.payload, owner_observed=seconds == 390)
                row.payload["diagnostics"] = FirmwareDiagnostics.model_validate(
                    row.payload["diagnostics"]).model_dump(mode="json")
            db.add(row)
            db.flush()
            samples[seconds] = row
            connector.boot_id = boot
            if seconds == 0:
                baseline.update(telemetry_id=row.id, queue_inventory={"schema_version": 1,
                    "basis": "VERIFIED_EMPTY_REQUIRED_QUEUES", "telemetry_id": row.id,
                    "queues": {"journal": 0, "legacy_migration": 0}})
                run.baseline = dict(baseline)
            if seconds == 210 and not getattr(request, "param", False):
                run.result = {"add_interruption": interruption_control(run, start, samples[150].id)}
            if getattr(request, "param", False):
                clock["now"] = stamp
                if seconds == 330:
                    db.commit()
                    reboot_value, created = hil_reboot.start_reboot(db, run.run_id,
                        actor="test", idempotency_key="synthetic-reboot")
                    assert created
                    apply_command_update(db, connector=connector, command_id=reboot_value["command_id"],
                        status="RUNNING", result=core(reboot_value), error_code=None, error_message=None,
                        envelope_boot_id=boot, envelope_sent_at=stamp)
                    db.commit()
                if seconds == 360:
                    db.commit()
                    proof = {**witness(reboot_value), "boot_after": boot}
                    apply_command_update(db, connector=connector, command_id=reboot_value["command_id"],
                        status="RUNNING", result=proof, error_code=None, error_message=None,
                        envelope_boot_id=boot, envelope_sent_at=stamp)
                    db.commit()
                if seconds == 450:
                    db.commit()
                    command = db.scalar(select(DeviceCommand))
                    hil_reboot.advance_reboot_command(db, command, now=stamp, telemetry=row)
                    db.commit()
                    assert command.status == "SUCCEEDED"
        baseline.update(telemetry_id=samples[0].id, queue_inventory={"schema_version": 1,
            "basis": "VERIFIED_EMPTY_REQUIRED_QUEUES", "telemetry_id": samples[0].id,
            "queues": {"journal": 0, "legacy_migration": 0}})
        run.baseline = baseline
        if not getattr(request, "param", False):
            run.result = {**run.result, "add_interruption": interruption_control(run, start, samples[150].id)}
        db.commit()
    monkeypatch.setattr(observation, "utc_now", lambda: now)
    return observed, now


def finalize(full_rows):
    sessions, _ = full_rows
    with sessions() as db:
        run = observation.complete_full_run(db, db.scalar(select(FirmwareHilRun.run_id)), actor="test")
        db.commit()
        return run


def test_source_chain_recomputed_from_actual_raw_ingress(full_rows):
    sessions, _ = full_rows
    with sessions() as db:
        run, connector = db.scalar(select(FirmwareHilRun)), db.scalar(select(Connector))
        value, errors, tail, certificates = observation._source(db, run, connector, 2)
        assert errors == []
        assert len(value["chunks"]) == len(value["manifest_ids"]) == 2
        assert tail == ensure_utc(run.started_at) + timedelta(seconds=490)
        assert certificates == (run.baseline["job_id"],)


@pytest.mark.parametrize("fault,reason", [
    ("gap", "SOURCE_ORDINAL_INVENTORY_MISMATCH"), ("chain", "SOURCE_DIGEST_CHAIN_CHANGED"),
    ("raw", "SOURCE_RAW_CUSTODY_UNVERIFIED"), ("disposition", "SOURCE_RAW_CUSTODY_UNVERIFIED"),
    ("certificate", "SOURCE_BASELINE_OR_CERTIFICATE_CHANGED"),
    ("epoch", "SOURCE_BASELINE_OR_CERTIFICATE_CHANGED"),
])
def test_source_inventory_and_raw_dispositions_cannot_be_assumed(full_rows, fault, reason):
    sessions, _ = full_rows
    with sessions() as db:
        run, connector = db.scalar(select(FirmwareHilRun)), db.scalar(select(Connector))
        row = db.scalar(select(TerminalRecordManifest).order_by(TerminalRecordManifest.id))
        if fault == "gap":
            row.canonical_source = False
        elif fault == "chain":
            db.scalar(select(SourceTailChunk).order_by(SourceTailChunk.id)).resulting_chain_digest = "f" * 64
        elif fault == "raw":
            row.protected_raw_record = "corrupt"
        elif fault == "disposition":
            row.disposition = "EVENT"
        elif fault == "certificate":
            db.scalar(select(ReconciliationJob)).capture_certificate = {"changed": True}
        else:
            run.baseline = {**run.baseline, "source_epoch": "other"}
        db.commit()
        assert reason in observation._source(db, run, connector, 2)[1]


def test_safe_interruption_is_assembled_from_committed_control_and_telemetry(full_rows):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        samples = [observation._sample(row, run, HilTarget.model_validate(run.target),
            ReleaseIdentity.model_validate(run.release_identity), [])[0]
            for row in db.scalars(select(DeviceTelemetry).order_by(DeviceTelemetry.id))]
        test, errors = observation._interruption(db, run, samples)
        assert errors == [] and test.kind == "ADD_INTERRUPT_30S" and test.outcome == "SUCCEEDED"
        value = deepcopy(run.result)
        value["add_interruption"]["restored_monotonic_ms"] -= 1
        run.result = value
        db.commit()
        assert observation._interruption(db, run, samples)[1] == ["ADD_INTERRUPTION_EXPIRY_OR_RECOVERY_UNVERIFIED"]


def test_full_collector_never_infers_reboot_from_new_boot_or_operator_success(full_rows):
    run = finalize(full_rows)
    assert run.status != "HIL_ACCEPTED"
    assert "RECOVERY_TESTS_MISSING_OR_REPEATED" in run.result["reasons"]
    assert "UNPLANNED_RESET" in run.result["reasons"]
    assert run.result["evidence"]["attendance"]["reasons"] == []
    assert all(receipt["oracle_raw_content_and_day_times"] == "NOT_ASSERTED"
        for item in run.result["evidence"]["attendance"]["occurrences"] for receipt in item["oracle_receipts"])


@pytest.mark.parametrize("full_rows", [True], indirect=True)
def test_all_server_owned_inputs_can_accept_experimental_hil_without_claiming_content_or_hardware(full_rows):
    sessions, _ = full_rows
    run = finalize(full_rows)
    assert run.result["reasons"] == []
    assert run.status == "HIL_ACCEPTED" and run.result["outcome"] == "PASS"
    evidence = run.result["evidence"]
    assert evidence["physical_power_cut"] == evidence["hardware_endurance"] == "NOT_PERFORMED"
    assert evidence["production_qualification"] == "NOT_ASSERTED"
    assert evidence["acceptance_scope"] == "EXPERIMENTAL_REMOTE_CONTROL_AND_SOURCE_CUSTODY"
    assert evidence["local_journal_preservation"] == evidence["seven_day_capacity"] == "NOT_ASSERTED"
    with sessions() as db:
        event = db.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "HIL_ACCEPTED"))
        assert observation.accepted_full_evidence_valid(run, event.details)
        altered = deepcopy(event.details)
        altered["evidence_sha256"] = "a" * 64
        assert not observation.accepted_full_evidence_valid(run, altered)
        run.result = {**run.result, "evidence": {**evidence, "production_qualification": "PASS"}}
        assert not observation.accepted_full_evidence_valid(run, event.details)


@pytest.mark.parametrize("full_rows", [True], indirect=True)
def test_failed_startup_sample_is_preserved_even_inside_controlled_reboot(full_rows):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        row = db.scalar(select(DeviceTelemetry).where(
            DeviceTelemetry.created_at == ensure_utc(run.started_at) + timedelta(seconds=360)))
        payload = deepcopy(row.payload)
        payload["diagnostics"]["storage"]["durability"] = "DEGRADED"
        row.payload = payload
        db.commit()
    run = finalize(full_rows)
    assert run.status == "HIL_FAILED" and "STORAGE_HEALTHY" in run.result["reasons"]
    assert any(item["storage_healthy"] is False for item in run.result["evidence"]["samples"])


@pytest.mark.parametrize("full_rows", ["startup"], indirect=True)
def test_real_boot_prefix_retains_unknown_and_recovering_snapshots(full_rows):
    run = finalize(full_rows)
    assert run.status == "HIL_ACCEPTED", run.result["reasons"]
    samples = run.result["evidence"]["samples"]
    assert len(samples) == 30  # The 30-second ADD interruption is the only gap.
    transitions = [sample for sample in samples if sample.get("reboot_startup")]
    assert [row["reboot_startup"]["phase"] for row in transitions] == ["NOT_STARTED", "RECOVERING"]
    assert transitions[0]["storage_healthy"] is None
    assert transitions[0]["write_failures"] is transitions[0]["read_failures"] is None
    assert transitions[0]["source_count"] == transitions[1]["source_count"] == 0
    assert transitions[1]["storage_healthy"] is False
    assert all(row["persistence_verified"] is False and row["recovery_complete"] is False for row in transitions)
    assert transitions[0]["startup_diagnostics"]["delivery_authority"] == "UNKNOWN"
    assert transitions[0]["errors"] == ["DELIVERY_AUTHORITY_UNVERIFIED", "QUALIFIED_READER_PROOF_PENDING"]
    assert transitions[1]["startup_diagnostics"]["journal_storage"]["durability"] == "DEGRADED"
    assert all(row["reboot_startup"]["recovery_telemetry_id"] == run.result["esp_reboot"]["recovery_telemetry_id"]
               for row in transitions)


@pytest.mark.parametrize("full_rows", ["startup"], indirect=True)
@pytest.mark.parametrize("case,reason", [
    ("second-startup", "STORAGE_HEALTHY"),
    ("source-generation", "SOURCE_GENERATION_CHANGED"),
    ("source-cursor", "SOURCE_CURSOR_REGRESSED"),
    ("source-count", "SOURCE_COUNT_REGRESSED"),
    ("write-failure", "NEW_WRITE_FAILURES"),
    ("read-failure", "NEW_READ_FAILURES"),
    ("restart", "NEW_WORKER_RESTARTS"),
    ("io-failure", "STORAGE_HEALTHY"),
    ("owner-failure", "STORAGE_HEALTHY"),
    ("sticky", "STORAGE_HEALTHY"),
    ("checkpoint-damage", "STORAGE_HEALTHY"),
    ("stall", "STORAGE_HEALTHY"),
    ("missing-owner-failures", "STORAGE_HEALTHY"),
    ("malformed-phase", "STORAGE_HEALTHY"),
    ("malformed-worker-state", "STORAGE_HEALTHY"),
    ("malformed-journal-durability", "JOURNAL_STORAGE_SHAPE_INVALID"),
    ("wrong-image", "SIGNED_WRITER_APPLICATION_MISMATCH"),
    ("unrelated-boot", "UNPLANNED_RESET"),
])
def test_startup_classification_never_masks_fault_or_regression(full_rows, case, reason):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        seconds = 480 if case == "second-startup" else 420 if case.startswith("source-") else 390
        row = db.scalar(select(DeviceTelemetry).where(DeviceTelemetry.created_at ==
            ensure_utc(run.started_at) + timedelta(seconds=seconds)))
        payload = deepcopy(row.payload)
        diag = payload["diagnostics"]
        if case == "second-startup":
            payload = startup_payload(payload, owner_observed=True)
        elif case == "source-generation":
            diag["source_generation"] = 2
        elif case == "source-cursor":
            diag["committed_source_cursor"] = 0
        elif case == "source-count":
            payload["zkt"]["attendance_count"] = 0
            diag["committed_source_cursor"] = 0
        elif case in {"write-failure", "read-failure"}:
            diag["storage"][case.split("-")[0] + "_failures"] = 1
        elif case in {"owner-failure", "missing-owner-failures", "restart"}:
            worker = next(worker for worker in diag["workers"] if worker["name"] == "storage_owner")
            if case == "missing-owner-failures":
                worker.pop("failures")
            else:
                worker["restart_count" if case == "restart" else "failures"] = 1
        elif case == "io-failure":
            diag["storage"]["error_code"] = 5
        elif case == "sticky":
            diag["journal_storage"]["hil_reboot_persistence_incident"] = True
        elif case == "checkpoint-damage":
            diag["journal_storage"]["checkpoint_recovery_pending"] = True
        elif case == "stall":
            diag["journal_runtime"]["phase"] = "STALLED"
        elif case == "malformed-phase":
            diag["journal_runtime"]["phase"] = []
        elif case == "malformed-worker-state":
            diag["workers"][0]["state"] = []
        elif case == "malformed-journal-durability":
            diag["journal_storage"]["durability"] = []
        elif case == "wrong-image":
            payload["ota"]["image_sha256"] = "0" * 64
        else:
            row.boot_id = diag["boot_id"] = "unrelated-boot"
        row.payload = payload
        db.commit()
    run = finalize(full_rows)
    assert run.status == "HIL_FAILED", run.result["reasons"]
    assert reason in run.result["reasons"]


@pytest.mark.parametrize("full_rows", [True], indirect=True)
@pytest.mark.parametrize("case,accepted", [("during", True), ("after-expiry", False),
    ("persistent", False), ("wrong-boot", False), ("catalog", True), ("storage", False)])
def test_expected_warning_requires_exact_code_boot_interval_and_preservation(full_rows, case, accepted):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        seconds = {"after-expiry": 220, "persistent": 280}.get(case, 190)
        stamp = ensure_utc(run.started_at) + timedelta(seconds=seconds)
        code = "IDENTITY_CATALOG_MEMORY_FALLBACK" if case == "catalog" else (
            "AUTHENTICATED_IP_NOT_PERSISTED" if case == "storage" else "CURRENT_TAIL_CHECKPOINT_RETRY")
        db.add(DeviceLog(connector_id=run.connector_id,
            boot_id="wrong-boot" if case == "wrong-boot" else "boot-before", sequence=10001,
            level="WARN", subsystem="identity" if case == "catalog" else "reconcile", code=code,
            message="synthetic diagnostic", received_at=stamp + timedelta(seconds=25), device_time=stamp))
        db.commit()
    run = finalize(full_rows)
    assert (run.status == "HIL_ACCEPTED") is accepted
    review = run.result["evidence"]["alert_review"]
    if case == "during":
        assert review["expected_interruption_log_ids"] and not review["warning_log_ids"]
    elif case == "catalog":
        assert review["optional_catalog_warning_ids"]
    else:
        assert "WARNING_LOG_REVIEW_REQUIRED" in run.result["reasons"]


@pytest.mark.parametrize("full_rows", [True], indirect=True)
def test_concurrent_completion_commits_one_verdict_and_never_replaces_evidence(full_rows):
    sessions, _ = full_rows
    def finish():
        with sessions() as db:
            run = observation.complete_full_run(db, db.scalar(select(FirmwareHilRun.run_id)), actor="test")
            db.commit()
            return run.result["evidence_sha256"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        digests = list(pool.map(lambda _: finish(), range(2)))
    assert digests[0] == digests[1]
    with sessions() as db:
        assert len(list(db.scalars(select(FirmwareEvent).where(FirmwareEvent.state == "HIL_ACCEPTED")))) == 1


@pytest.mark.parametrize("full_rows", [True], indirect=True)
@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_actual_control_route_between_collection_and_commit_cannot_advance_scope(full_rows, monkeypatch, action):
    from zk_add import web
    sessions, _ = full_rows
    monkeypatch.setattr(web, "require_step_up", lambda *_: None)
    monkeypatch.setattr(web.browser_events, "publish", AsyncMock())
    with sessions() as db:
        engine = db.get_bind()
    invoked = []
    def change_scope(_connection, _cursor, statement, _parameters, _context, _many):
        if invoked or not statement.upper().startswith("UPDATE ADD_FIRMWARE_HIL_RUNS"):
            return
        invoked.append(action)
        with sessions() as other:
            body = SimpleNamespace(password="synthetic-test", reason="Synthetic race test")
            auth = (other, SimpleNamespace(username="test"))
            if action == "cancel":
                campaign = other.scalar(select(FirmwareCampaign))
                asyncio.run(web.control_firmware_campaign(campaign.campaign_id, "cancel", body, auth))
            else:
                release = other.scalar(select(FirmwareRelease))
                asyncio.run(web.revoke_firmware_release(release.release_id, body, auth))
    sa_event.listen(engine, "before_cursor_execute", change_scope)
    try:
        run = finalize(full_rows)
    finally:
        sa_event.remove(engine, "before_cursor_execute", change_scope)
    assert invoked == [action]
    assert run.status == "HIL_INCOMPLETE"
    assert "SCOPE_CHANGED_BEFORE_FINALIZATION" in run.result["reasons"]
    with sessions() as db:
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state == "HIL_ACCEPTED"))


@pytest.mark.parametrize("full_rows", [True], indirect=True)
@pytest.mark.parametrize("fault,reason", [("baseline-sequence", "BASELINE_BOOT_PROGRESS_REGRESSED"),
    ("baseline-uptime", "BASELINE_BOOT_PROGRESS_REGRESSED"), ("baseline-source", "BASELINE_SOURCE_MISMATCH"),
    ("different-boot", "UNPLANNED_RESET"), ("tail-too-early", "FINAL_TAIL_VERIFICATION_MISSING")])
def test_first_sample_and_late_tail_cannot_hide_a_regression(full_rows, fault, reason):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        initial = db.get(DeviceTelemetry, run.baseline["telemetry_id"])
        if fault.startswith("baseline-"):
            # The pinned baseline is just before the window; first sample must
            # advance it. Move only the receipt timestamp, retaining its bytes.
            initial.created_at = ensure_utc(run.started_at) - timedelta(seconds=1)
            payload = deepcopy(initial.payload)
            payload["_trusted_envelope_sent_at"] = initial.created_at.isoformat()
            if fault == "baseline-sequence":
                initial.sequence += 1000
            elif fault == "baseline-uptime":
                initial.uptime_seconds += 1000
            else:
                payload["diagnostics"]["committed_source_cursor"] = 1
            initial.payload = payload
        elif fault == "different-boot":
            row = db.scalar(select(DeviceTelemetry).where(
                DeviceTelemetry.created_at == ensure_utc(run.started_at) + timedelta(seconds=690)))
            row.boot_id = "unrelated-reset"
            row.payload = {**row.payload, "diagnostics": {**row.payload["diagnostics"], "boot_id": row.boot_id}}
        else:
            chunk = db.scalar(select(SourceTailChunk).order_by(SourceTailChunk.id.desc()))
            chunk.committed_at = ensure_utc(run.started_at) + timedelta(seconds=470)
        db.commit()
    run = finalize(full_rows)
    assert reason in run.result["reasons"] and run.status != "HIL_ACCEPTED"


@pytest.mark.parametrize("full_rows", [True], indirect=True)
@pytest.mark.parametrize("fault,reason", [("counter-offset", "WORKER_RESTART_COUNTER_CHANGED"),
                                        ("source-count", "SOURCE_COUNT_REGRESSED")])
def test_aggregate_counters_cannot_mask_individual_regression(full_rows, fault, reason):
    sessions, _ = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        for row in db.scalars(select(DeviceTelemetry).where(DeviceTelemetry.boot_id == "boot-before")):
            payload = deepcopy(row.payload)
            special = ensure_utc(row.created_at) == ensure_utc(run.started_at) + timedelta(seconds=120)
            if fault == "counter-offset":
                payload["diagnostics"]["workers"][0]["restart_count"] = 2 if special else 3
                payload["diagnostics"]["workers"][1]["restart_count"] = 3 if special else 2
            elif special:
                payload["zkt"]["attendance_count"] = 2
            row.payload = payload
        db.commit()
    run = finalize(full_rows)
    assert reason in run.result["reasons"] and run.status != "HIL_ACCEPTED"


def test_completion_is_immutable_and_has_one_hash_bound_event(full_rows):
    sessions, _ = full_rows
    first = finalize(full_rows)
    again = finalize(full_rows)
    assert first.result == again.result and first.completed_at == again.completed_at
    assert first.result["evidence_sha256"] == observation.evidence_digest(first.result["evidence"])
    with sessions() as db:
        events = list(db.scalars(select(FirmwareEvent).where(FirmwareEvent.state != "OFFERED")))
        assert len(events) == 1
        assert events[0].details["evidence_sha256"] == first.result["evidence_sha256"]
        assert not observation.accepted_full_evidence_valid(first, events[0].details)


@pytest.mark.parametrize("fault,reason", [
    ("unknown_queue", "QUEUE_BASELINE_OR_FINAL_INVENTORY_UNVERIFIED"),
    ("old_sample", "DIAGNOSTICS_FRESHNESS_UNPROVEN"),
    ("replay", "BOOT_PROGRESS_OR_SEQUENCE_REGRESSED"),
    ("wrong_image", "SIGNED_WRITER_APPLICATION_MISMATCH"),
    ("reboot_incident", "STORAGE_HEALTHY"),
    ("missing_reboot_incident", "REBOOT_PERSISTENCE_INCIDENT_STATE_UNKNOWN"),
    ("superseded", "DEPLOYMENT_OR_CAMPAIGN_CHANGED"),
    ("paused", "DEPLOYMENT_OR_CAMPAIGN_CHANGED"),
    ("no_attendance", "NO_QUALIFYING_ATTENDANCE"),
    ("rejection", "UNRESOLVED_REGRESSION"),
    ("alert", "UNRESOLVED_REGRESSION"),
])
def test_bad_or_missing_committed_evidence_never_becomes_success(full_rows, fault, reason):
    sessions, now = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        row = db.scalar(select(DeviceTelemetry).order_by(DeviceTelemetry.id.desc()))
        payload = deepcopy(row.payload)
        if fault == "unknown_queue":
            payload["diagnostics"]["queues"][0]["count_known"] = False
        elif fault == "old_sample":
            payload["_trusted_envelope_sent_at"] = (now - timedelta(minutes=2)).isoformat()
        elif fault == "replay":
            row.sequence = 0
        elif fault == "wrong_image":
            payload["ota"]["image_sha256"] = "f" * 64
        elif fault == "reboot_incident":
            payload["diagnostics"]["journal_storage"]["hil_reboot_persistence_incident"] = True
        elif fault == "missing_reboot_incident":
            payload["diagnostics"]["journal_storage"].pop("hil_reboot_persistence_incident")
        elif fault == "superseded":
            old = db.scalar(select(FirmwareDeployment))
            db.add(FirmwareDeployment(deployment_id="newer", connector_id=old.connector_id,
                release_id=old.release_id, campaign_id=old.campaign_id, target_version="2.7.0", status="SUCCEEDED"))
        elif fault == "paused":
            db.scalar(select(FirmwareCampaign)).status = "PAUSED"
        elif fault == "no_attendance":
            for item in db.scalars(select(ZktOracleMembershipReceipt)):
                db.delete(item)
        elif fault == "rejection":
            db.add(DeviceLog(connector_id=run.connector_id, boot_id=row.boot_id, sequence=999,
                level="ERROR", subsystem="add_backend", code="DEVICE_MESSAGE_REJECTED",
                message="private original error must not leak", received_at=now))
        else:
            db.add(DeviceAlert(connector_id=run.connector_id, code="STORAGE_FAILURE", severity="HIGH",
                state="RESOLVED", message="private original alert must not leak", first_seen_at=now,
                last_seen_at=now))
        row.payload = payload
        db.commit()
    run = finalize(full_rows)
    assert reason in run.result["reasons"] and run.status != "HIL_ACCEPTED"
    assert "private original" not in str(run.result)


def test_collection_limits_are_explicit_not_silent_empty_success(full_rows, monkeypatch):
    monkeypatch.setattr(observation, "SAMPLE_LIMIT", 2)
    run = finalize(full_rows)
    assert "OBSERVATION_SAMPLE_LIMIT" in run.result["reasons"]
    assert len(run.result["evidence"]["samples"]) == 2 and run.status != "HIL_ACCEPTED"


def test_source_limit_is_an_explicit_incomplete_inventory(full_rows, monkeypatch):
    monkeypatch.setattr(observation, "SOURCE_LIMIT", 1)
    run = finalize(full_rows)
    assert "SOURCE_RANGE_UNVERIFIED_OR_LIMIT" in run.result["reasons"]
    assert run.result["evidence"]["attendance"]["truncated"]
    assert run.status != "HIL_ACCEPTED"


@pytest.mark.parametrize("shape", ["diagnostics", "workers", "queues", "journal_storage"])
def test_malformed_nested_telemetry_is_missing_proof_not_a_collector_crash(full_rows, shape):
    sessions, _ = full_rows
    with sessions() as db:
        run, row = db.scalar(select(FirmwareHilRun)), db.scalar(select(DeviceTelemetry))
        payload = deepcopy(row.payload)
        if shape == "diagnostics":
            payload[shape] = "invalid"
        else:
            payload["diagnostics"][shape] = "invalid"
        row.payload = payload
        db.commit()
        sample, detail = observation._sample(row, run, HilTarget.model_validate(run.target),
            ReleaseIdentity.model_validate(run.release_identity), [])
        assert detail["errors"] or sample.counts_known is None
        assert any(getattr(sample, field) is not True for field in (
            "storage_healthy", "workers_healthy", "counts_known", "terminal_certified"))


def test_incomplete_window_and_dirty_state_cannot_finalize(full_rows, monkeypatch):
    sessions, now = full_rows
    with sessions() as db:
        run = db.scalar(select(FirmwareHilRun))
        monkeypatch.setattr(observation, "utc_now", lambda: now - timedelta(seconds=1))
        with pytest.raises(ValueError, match="complete 15-minute"):
            observation.complete_full_run(db, run.run_id, actor="test")
        run.result = {"operator": "PASS"}
        with pytest.raises(ValueError, match="clean committed"):
            observation.complete_full_run(db, run.run_id, actor="test")
        db.rollback()

"""Actual custody/heartbeat evidence is not an operator-created cutover permit."""

import base64
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

import test_zkt_raw_source_custody as source
from test_zkt_source_load import store as store
from zk_add.crypto import encrypt_text
from zk_add.models import (
    AttendanceEvent,
    ReconciliationCoverage,
    TerminalRecordManifest,
    TerminalSourceEpoch,
    ZktSourceCutover,
)
from zk_add.schemas import HeartbeatPayload, SourceBoundaryDiagnostics
from zk_add.service import apply_firmware_diagnostics
from zk_add.time_utils import utc_now
from zk_add.zkt_handoff import boundary_status, terminal_digest

source_store = source.source_store


def claim():
    return dict(
        schema_version=1,
        observed=True,
        verified=True,
        migration_certified=False,
        result="OK",
        next_ordinal=3,
        record_size=40,
        capture_epoch="a" * 32,
        writer_digest="b" * 64,
        terminal_digest=terminal_digest("TEST-LOAD"),
        anchor_digest=hashlib.sha256(b"\xff" * 40).hexdigest(),
    )


def prepared(db):
    connector, request = source.prepare(db, "tail", size=40, length=3)
    source.apply(db, connector, "tail", request)
    now = utc_now()
    connector.connected = connector.ota_secure_boot = connector.ota_rollback_enabled = True
    connector.ota_running_partition = "ota_1"
    connector.ota_image_sha256 = "b" * 64
    connector.boot_id = "test-writer-boot"
    connector.last_sequence = 9
    payload = HeartbeatPayload(
        diagnostics={
            "schema_version": 2,
            "runtime_profile": "ZKT_JOURNAL_V1",
            "delivery_authority": "ADD",
            "journal_format": 1,
            "source_boundary": claim(),
            "boot_id": "untrusted-nested-claim",
            "sample_sequence": 999,
        }
    )
    apply_firmware_diagnostics(db, connector, payload, sampled_at=now)
    db.commit()
    return connector, now


def test_actual_ingress_preserves_boundary_and_uses_authenticated_envelope_identity(source_store):
    with source_store() as db:
        connector, now = prepared(db)
        assert connector.firmware_diagnostics["source_boundary"] == claim()
        assert connector.firmware_diagnostics["boot_id"] == "test-writer-boot"
        assert connector.firmware_diagnostics["sample_sequence"] == 9
        result = boundary_status(db, connector, now=now + timedelta(seconds=1))
        assert result["state"] == "VERIFIED_SOURCE_ANCHOR"
        assert result["sampled_at"] == now.isoformat()
        assert result["next_ordinal"] == 3 and result["record_size"] == 40
        assert (
            result["migration_certified"] is False
            and result["delivery_permission"] == "NOT_EVALUATED"
        )
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 0
        assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == 0


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("offline", "STALE_TELEMETRY"),
        ("old-sample", "STALE_TELEMETRY"),
        ("old-receipt", "STALE_TELEMETRY"),
        ("future", "STALE_TELEMETRY"),
        ("boot", "STALE_TELEMETRY"),
        ("sequence", "STALE_TELEMETRY"),
        ("naive", "STALE_TELEMETRY"),
        ("image", "WRITER_BINDING"),
        ("factory", "WRITER_BINDING"),
        ("unsigned", "WRITER_BINDING"),
        ("authority", "WRITER_BINDING"),
        ("terminal", "TERMINAL_BINDING"),
        ("binding", "TERMINAL_BINDING"),
        ("coverage", "WAIT_SOURCE_COVERAGE"),
        ("epoch", "WAIT_SOURCE_COVERAGE"),
        ("cursor", "WAIT_SOURCE_COVERAGE"),
        ("size", "ANCHOR_MISMATCH"),
        ("anchor", "ANCHOR_MISMATCH"),
        ("raw", "RAW_EVIDENCE_CHANGED"),
        ("encrypted", "RAW_EVIDENCE_CHANGED"),
        ("unverified", "UNCERTAIN"),
        ("forged-certificate", "INVALID"),
    ],
)
def test_changed_or_stale_evidence_holds_without_permit(source_store, fault, reason):
    with source_store() as db:
        connector, now = prepared(db)
        diagnostic = deepcopy(connector.firmware_diagnostics)
        coverage = db.scalar(
            select(ReconciliationCoverage).where(ReconciliationCoverage.active.is_(True))
        )
        anchor = db.scalar(
            select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 2)
        )
        if fault == "offline":
            connector.connected = False
        elif fault == "old-sample":
            diagnostic["sampled_at"] = (now - timedelta(seconds=46)).isoformat()
        elif fault == "old-receipt":
            connector.firmware_diagnostics_at = now - timedelta(seconds=46)
        elif fault == "future":
            diagnostic["sampled_at"] = (now + timedelta(minutes=1)).isoformat()
        elif fault == "naive":
            diagnostic["sampled_at"] = now.replace(tzinfo=None).isoformat()
        elif fault == "boot":
            connector.boot_id = "different-boot"
        elif fault == "sequence":
            diagnostic["sample_sequence"] = 10
        elif fault == "image":
            connector.ota_image_sha256 = "c" * 64
        elif fault == "factory":
            connector.ota_running_partition = "factory"
        elif fault == "unsigned":
            connector.ota_secure_boot = False
        elif fault == "authority":
            diagnostic["delivery_authority"] = "UNKNOWN"
        elif fault == "terminal":
            diagnostic["source_boundary"]["terminal_digest"] = "d" * 64
        elif fault == "binding":
            connector.zkt_device.confirmed_serial = "OTHER"
        elif fault == "coverage":
            coverage.active = False
        elif fault == "epoch":
            db.get(TerminalSourceEpoch, coverage.source_epoch_id).state = "SUPERSEDED"
        elif fault == "cursor":
            coverage.source_committed_cursor = 2
        elif fault == "size":
            anchor.record_size = 16
        elif fault == "anchor":
            anchor.raw_record_digest = "e" * 64
        elif fault == "raw":
            anchor.protected_raw_record = encrypt_text(base64.b64encode(b"\x01" * 40).decode())
        elif fault == "encrypted":
            anchor.protected_raw_record = "damaged-ciphertext"
        elif fault == "unverified":
            diagnostic["source_boundary"] = dict(
                schema_version=1,
                observed=True,
                verified=False,
                migration_certified=False,
                result="UNCERTAIN",
            )
        elif fault == "forged-certificate":
            diagnostic["source_boundary"]["migration_certified"] = True
        connector.firmware_diagnostics = diagnostic
        db.flush()
        result = boundary_status(db, connector, now=now + timedelta(seconds=1))
        assert result["state"] == "HELD" and result["reason"] == "SOURCE_BOUNDARY_" + reason
        assert not result["migration_certified"]
        assert not db.new and not db.dirty


@pytest.mark.parametrize(
    "change",
    [
        {"next_ordinal": True},
        {"next_ordinal": -1},
        {"next_ordinal": 2**31},
        {"record_size": False},
        {"record_size": 28},
        {"schema_version": True},
        {"migration_certified": 0},
        {"migration_certified": True},
        {"verified": 1},
        {"observed": False},
        {"result": "CORRUPT"},
        {"writer_digest": "0" * 64},
        {"capture_epoch": "0" * 32},
        {"anchor_digest": "0" * 64},
        {"next_ordinal": 0},
        {"verified": False},
        {"extra": True},
    ],
)
def test_boundary_schema_does_not_coerce_or_infer_verified_evidence(change):
    with pytest.raises(ValidationError):
        SourceBoundaryDiagnostics.model_validate({**claim(), **change})


def test_empty_source_boundary_and_legacy_heartbeat(source_store):
    assert HeartbeatPayload.model_validate({}).diagnostics is None
    empty = {**claim(), "next_ordinal": 0, "record_size": 0, "anchor_digest": "0" * 64}
    assert SourceBoundaryDiagnostics.model_validate(empty).next_ordinal == 0
    with source_store() as db:
        connector, now = prepared(db)
        connector.firmware_diagnostics = {
            **connector.firmware_diagnostics,
            "source_boundary": empty,
        }
        result = boundary_status(db, connector, now=now + timedelta(seconds=1))
        assert result["state"] == "VERIFIED_SOURCE_ANCHOR" and result["record_size"] == 0
        connector.firmware_version = "2.6.16"
        assert (
            boundary_status(db, connector, now=now)["reason"] == "SOURCE_BOUNDARY_WRITER_REQUIRED"
        )

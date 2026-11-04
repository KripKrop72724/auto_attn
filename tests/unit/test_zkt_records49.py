"""49-byte ZKT attendance records: the 40-byte record plus a 9-byte tail.

LHRCENTRAL's MB40-VL/ID terminals (platform ZAM180_TFT) return these. The
records here are synthetic: user ID "1234", a sequential record UID and the
observed tail bytes (0xff or 0x00, ASCII "255", zeros).
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace

from cryptography.fernet import Fernet
from pydantic import ValidationError
import pytest

from zk_add.attendance_identity_evidence import manifest_identity_matches_event
from zk_add.attendance_recovery import RecoveryError, _decode_zkt_record
from zk_add.crypto import encrypt_text
from zk_add.models import TerminalRecordManifest
from zk_add.reconciliation import _ledger_row_valid, _source_identity_missing
from zk_add.schemas import (
    ReconciliationAnchorRequest, SourceBoundaryDiagnostics, SourceProbeResultRequest,
    SourceTailChunkRequest,
)
from zk_add.settings import settings
from zk_add.zkt_decode import SOURCE_SIZES, TEXT_SOURCE_SIZES

ENCODED = 859972462  # 2026-10-03 09:14:22 PKT
TAIL = bytes.fromhex("ff3235350000000000")
DIGEST = "a" * 64


def record49(*, record_uid=1322, user_id=b"1234", status=15, lead=0xFF) -> bytes:
    raw = bytearray(40)
    raw[0:2] = record_uid.to_bytes(2, "little")
    raw[2:2 + len(user_id)] = user_id
    raw[26] = status
    raw[27:31] = ENCODED.to_bytes(4, "little")
    return bytes(raw) + bytes([lead]) + TAIL[1:]


def source_row(raw: bytes, ordinal: int = 0) -> dict:
    return dict(ordinal=ordinal, raw_record_digest=hashlib.sha256(raw).hexdigest(),
                terminal_record_key=DIGEST, occurrence_index=ordinal + 1, disposition="MALFORMED",
                error_code="UNPARSEABLE_TERMINAL_RECORD", raw_record_b64=base64.b64encode(raw).decode())


def test_size_sets_name_49_beside_the_existing_layouts():
    assert SOURCE_SIZES == {8, 16, 40, 49}
    assert TEXT_SOURCE_SIZES == {40, 49}


@pytest.mark.parametrize("size", [8, 16, 40, 49, 48, 50, 41])
def test_wire_schemas_accept_49_and_still_reject_other_widths(size):
    accepted = size in {8, 16, 40, 49}
    epoch = "12345678-1234-4234-8234-123456789012"
    anchor = dict(source_epoch=epoch, job_id="1" * 36, generation=1, terminal_serial="COVP-TEST",
                  terminal_generation=1, cutoff_count=13293, latest_terminal_count=13293,
                  record_size=size, source_total_bytes=4 + 13293 * size,
                  first_anchor_digest=DIGEST)
    raw = record49()[:size] if size <= 49 else record49() + bytes(size - 49)
    probe = dict(source_epoch=epoch, job_id="1" * 36, generation=1, terminal_serial="COVP-TEST",
                 latest_terminal_count=13293, record_size=size, ordinal=7, record=source_row(raw, 7))
    tail = dict(source_epoch=epoch, terminal_serial="COVP-TEST", terminal_generation=1,
                record_size=size, start_ordinal=0, end_ordinal=1, latest_terminal_count=1,
                chunk_digest=DIGEST, previous_chain_digest=DIGEST, resulting_chain_digest=DIGEST,
                records=[source_row(raw)])
    boundary = dict(schema_version=1, observed=True, verified=True, migration_certified=False,
                    result="OK", next_ordinal=13293, record_size=size, capture_epoch="b" * 32,
                    writer_digest=DIGEST, terminal_digest=DIGEST, anchor_digest=DIGEST)
    for model, value in ((ReconciliationAnchorRequest, anchor), (SourceProbeResultRequest, probe),
                         (SourceTailChunkRequest, tail), (SourceBoundaryDiagnostics, boundary)):
        if accepted:
            assert model.model_validate(value).record_size == size
        else:
            with pytest.raises(ValidationError):
                model.model_validate(value)


def test_missing_text_identity_is_read_from_the_49_byte_prefix():
    event = object()
    for raw, missing in ((record49(), False), (record49(user_id=b" " * 24), True),
                         (record49(user_id=b""), True), (record49(lead=0), False),
                         (record49(user_id=b"")[:40], True), (record49()[:48], False)):
        row = SimpleNamespace(event=event, raw_record_b64=base64.b64encode(raw).decode())
        assert _source_identity_missing(row) is missing
    # The tail never supplies identity, even when the prefix field is empty.
    assert _source_identity_missing(SimpleNamespace(
        event=event, raw_record_b64=base64.b64encode(bytes(40) + b"1234\0\0\0\0\0").decode()))


@pytest.mark.parametrize("size", [8, 16, 40, 49, 41, 48, 50, None])
def test_canonical_ledger_rows_admit_49_like_40(size):
    common = dict(serial="COVP-TEST", connector_id=1, raw=DIGEST, key=DIGEST, occurrence=1,
                  present=True, expected_serial="COVP-TEST", expected_connector=1)
    assert _ledger_row_valid(size=size, **common) is (size in {8, 16, 40, 49})
    assert _ledger_row_valid(size=size, expected_size=49, **common) is (size == 49)


@pytest.fixture()
def pii_keys(monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")


def saved(raw: bytes, *, record_size: int = 49, **fields) -> TerminalRecordManifest:
    return TerminalRecordManifest(
        record_size=record_size, protected_raw_record=encrypt_text(base64.b64encode(raw).decode()),
        raw_record_digest=hashlib.sha256(raw).hexdigest(), **fields)


@pytest.mark.parametrize("lead", [0xFF, 0x00])
def test_recovery_decodes_the_prefix_and_checks_all_49_bytes(pii_keys, lead):
    raw = record49(lead=lead)
    decoded = _decode_zkt_record(saved(raw, observed_uid="1322", observed_user_id="1234"))
    assert decoded == {"uid": "1322", "user_id": "1234", "raw_timestamp": ENCODED,
                       "status": "15", "punch": "0", "raw_punch": False}
    assert decoded == _decode_zkt_record(saved(raw[:40], record_size=40))
    # Length and digest cover the tail: a 40-byte copy is not the 49-byte record.
    with pytest.raises(RecoveryError, match="size does not match"):
        _decode_zkt_record(saved(raw[:40]))
    tampered = saved(raw)
    tampered.raw_record_digest = hashlib.sha256(raw[:40]).hexdigest()
    with pytest.raises(RecoveryError, match="digest does not match"):
        _decode_zkt_record(tampered)
    with pytest.raises(RecoveryError, match="UID does not match"):
        _decode_zkt_record(saved(raw, observed_uid="1323"))
    with pytest.raises(RecoveryError, match="unparseable"):
        _decode_zkt_record(saved(record49(user_id=b" " * 24)))
    with pytest.raises(RecoveryError, match="cannot be decoded"):
        _decode_zkt_record(saved(raw + b"\0", record_size=50))


@pytest.mark.parametrize("record_size,admitted", [(40, True), (49, True), (16, False), (8, False)])
def test_record_uid_namespace_is_admitted_for_49_byte_records_like_40(record_size, admitted):
    now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    event = SimpleNamespace(
        user_id="1234", uid="7", raw_event={"attendance_record_uid": "1322"},
        identity_terminal_fingerprint="f" * 64, source="FULL_HISTORY", clock_quality="OK",
        device_event_time=now - timedelta(days=30), captured_at=now,
        received_at=now + timedelta(seconds=1))
    manifest = SimpleNamespace(record_size=record_size, observed_uid="1322", observed_user_id="1234")
    assert manifest_identity_matches_event(manifest, event) is admitted
    manifest.observed_uid = "1323"
    assert manifest_identity_matches_event(manifest, event) is False

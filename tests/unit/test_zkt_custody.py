import base64
from contextlib import contextmanager
import hashlib

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_json
from zk_add.db import Base
from zk_add.models import (Connector, ZKTDevice, TerminalSourceEpoch, TerminalRecordManifest,
                           ZktObservationReceipt, ZktOccurrenceAlias, ZktObservationLink, AttendanceEvent)
from zk_add.settings import settings
from zk_add.zkt_custody import observation_id, settle_observations
from zk_add import web
from zk_add.schemas import Envelope
from zk_add.time_utils import utc_now


@pytest.fixture
def custody(monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        connector = Connector(connector_id="custody-test", hardware_id="aa:bb:cc:dd:ee:01",
                              zone_id="TEST", zone_name="TEST", device_id="TEST", display_name="TEST",
                              zkt_custody_enabled=True)
        db.add(connector)
        db.flush()
        connector.zkt_device = ZKTDevice(connector_id=connector.id, serial="TEST01", confirmed_serial="TEST01")
        db.commit()
        yield db, connector
    engine.dispose()


def observation(sequence=1, **updates):
    raw = b"sanitized raw bytes"
    result = dict(observation_id=observation_id("TEST01", "a" * 32, sequence),
                  terminal_serial="TEST01", capture_epoch="a" * 32, capture_sequence=sequence,
                  captured_at="2026-10-03T10:00:00Z", raw_b64=base64.b64encode(raw).decode(),
                  raw_digest=hashlib.sha256(raw).hexdigest(), raw_format="UNKNOWN",
                  decoder_profile="unqualified", decoder_version="1", time_quality="UNKNOWN")
    return {**result, **updates}


def batch(*observations):
    return {"schema_version": 1, "observations": list(observations)}


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_raw_preservation_replay_conflict_and_same_second_are_distinct(custody):
    db, connector = custody
    first = settle_observations(db, connector, batch(observation(), observation(2)))
    db.commit()
    replay = settle_observations(db, connector, batch(observation()))
    assert first["items"][0]["receipt_id"] == replay["items"][0]["receipt_id"]
    assert replay["items"][0]["replay"]
    assert first["items"][0]["receipt_id"] != first["items"][1]["receipt_id"]
    assert first["oracle_completion"] == "NOT_ASSERTED"
    conflict = settle_observations(db, connector, batch(observation(encoded_time=42)))
    assert conflict["items"][0]["error_code"] == "CAPTURE_IDENTITY_REUSED"
    assert count(db, ZktObservationReceipt) == 3
    assert count(db, AttendanceEvent) == 0  # No inferred employee or Oracle result.
    row = db.scalar(select(ZktObservationReceipt).order_by(ZktObservationReceipt.id))
    assert "sanitized" not in row.protected_observation
    assert decrypt_json(row.protected_observation)["observation"] == observation()


def test_bad_rows_do_not_block_later_observations(custody):
    db, connector = custody
    result = settle_observations(db, connector, batch(None, observation(raw_digest="0" * 64), observation(2)))
    assert [row["custody"] for row in result["items"]] == [
        "PRESERVED_EXCEPTION", "PRESERVED_EXCEPTION", "PRESERVED_UNRESOLVED"]
    assert count(db, ZktObservationReceipt) == 3


def test_disabled_scope_and_failed_encryption_cannot_return_receipts(custody, monkeypatch):
    db, connector = custody
    connector.zkt_custody_enabled = False
    with pytest.raises(ValueError, match="NOT_ENABLED"):
        settle_observations(db, connector, batch(observation()))
    connector.zkt_custody_enabled = True
    monkeypatch.setattr(settings, "pii_fernet_key", "")
    with pytest.raises(RuntimeError, match="FERNET"):
        settle_observations(db, connector, batch(observation()))
    db.rollback()
    assert count(db, ZktObservationReceipt) == 0


def test_source_aliases_require_exact_verified_coordinates(custody):
    db, connector = custody
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    raw_digest = observation()["raw_digest"]
    for ordinal in (0, 1):
        db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
                                     terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id,
                                     ordinal=ordinal, canonical_source=True, raw_record_digest=raw_digest,
                                     terminal_record_key=raw_digest, disposition="MALFORMED"))
    db.commit()
    def source(seq, ordinal, **kwargs):
        return observation(seq, raw_format="SOURCE_RECORD",
                           occurrence={"source_epoch": epoch.epoch_id, "ordinal": ordinal}, **kwargs)
    result = settle_observations(db, connector, batch(source(1, 0), source(2, 1), source(3, 2)))
    assert result["items"][0]["occurrence_id"] != result["items"][1]["occurrence_id"]
    assert result["items"][2]["occurrence_id"] is None
    assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 2
    replay = settle_observations(db, connector, batch(source(1, 0)))
    assert replay["items"][0]["occurrence_id"] == result["items"][0]["occurrence_id"]
    assert count(db, ZktObservationLink) == 2


def test_lost_ack_replays_receipt_even_after_later_sequence(custody, monkeypatch):
    db, connector = custody
    @contextmanager
    def scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    monkeypatch.setattr(web, "session_scope", scope)
    envelope = Envelope(connector_id=connector.connector_id, message_id="test-1", boot_id="test",
                        seq=1, sent_at=utc_now(), type="zkt_observation_batch", payload=batch(observation()))
    first = web.persist_envelope(connector.id, envelope).ack
    connector.last_sequence = 5
    db.commit()
    replay = web.persist_envelope(connector.id, envelope).ack
    assert first["items"][0]["receipt_id"] == replay["items"][0]["receipt_id"]
    assert replay["items"][0]["replay"]
    assert connector.last_sequence == 5


def test_commit_failure_emits_no_custody_ack(custody, monkeypatch):
    db, connector = custody
    @contextmanager
    def failing_scope():
        try:
            yield db
            raise RuntimeError("commit failed")
        finally:
            db.rollback()
    monkeypatch.setattr(web, "session_scope", failing_scope)
    # Exercise the commit boundary directly: handle_envelope sends only after
    # this function returns; no result may escape the failed transaction.
    envelope = Envelope(connector_id=connector.connector_id, message_id="test-1", boot_id="test",
                        seq=1, sent_at=utc_now(), type="zkt_observation_batch", payload=batch(observation()))
    with pytest.raises(RuntimeError, match="commit failed"):
        web.persist_envelope(connector.id, envelope)
    assert count(db, ZktObservationReceipt) == 0


def test_old_boot_receipt_replay_cannot_replace_current_boot(custody, monkeypatch):
    db, connector = custody
    connector.boot_id, connector.last_sequence = "current-boot", 100
    db.commit()
    @contextmanager
    def scope():
        yield db
        db.commit()
    monkeypatch.setattr(web, "session_scope", scope)
    envelope = Envelope(connector_id=connector.connector_id, message_id="old-1", boot_id="old-boot",
                        seq=999, sent_at=utc_now(), type="zkt_observation_batch", payload=batch(observation()))
    assert web.persist_envelope(connector.id, envelope).ack["committed"]
    assert connector.boot_id == "current-boot" and connector.last_sequence == 100

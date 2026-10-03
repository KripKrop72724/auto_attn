import base64
import hashlib
import struct

from sqlalchemy import delete, select

from test_zkt_custody import custody as custody, observation, batch, count
from zk_add.crypto import decrypt_json
from zk_add.models import (ZktCustodyWork, ZktCustodyWorkReceipt, ZktObservationReceipt,
                           AttendanceEvent, OrdsOutbox, Connector, ZKTDevice)
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import settle_observations, observation_id
from zk_add.zkt_custody_work import advance_work, backfill_work, materialize_packet, work_status
from zk_add.zkt_packet import FRAGMENT_DATA


def packet_observation(sequence, raw=b"synthetic packet bytes", **changes):
    return observation(sequence, raw_format="LIVE_PACKET", raw_b64=base64.b64encode(raw).decode(),
                       raw_digest=hashlib.sha256(raw).hexdigest(), **changes)


def packet_fragments(packet=b"x" * 600, *, group=1, first_sequence=1):
    values = []
    for index, offset in enumerate(range(0, len(packet), FRAGMENT_DATA)):
        raw = (b"ZJF1" + bytes([group]) * 16 + struct.pack("<II", len(packet), offset)
               + hashlib.sha256(packet).digest() + packet[offset:offset + FRAGMENT_DATA])
        values.append(observation(first_sequence + index, raw_format="PACKET_FRAGMENT",
                                  raw_b64=base64.b64encode(raw).decode(),
                                  raw_digest=hashlib.sha256(raw).hexdigest()))
    return values


def test_every_receipt_commits_a_work_obligation_and_replay_does_not_reset_it(custody):
    db, connector = custody
    settle_observations(db, connector, batch(None, observation(), packet_observation(2)))
    db.commit()
    assert count(db, ZktCustodyWork) == count(db, ZktCustodyWorkReceipt) == count(db, ZktObservationReceipt) == 3
    assert advance_work(db) == 2
    rows = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert [row.state for row in rows] == ["HELD_EXCEPTION", "WAIT_PROFILE", "WAIT_PROFILE"]
    assert all(row.reason_code and row.owner for row in rows)
    attempts = [row.attempt_count for row in rows]
    settle_observations(db, connector, batch(None, observation(), packet_observation(2)))
    assert advance_work(db) == 0
    assert [row.attempt_count for row in rows] == attempts
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_packet_waits_for_all_fragments_and_new_extent_wakes_it_once(custody):
    db, connector = custody
    first, second = packet_fragments()
    settle_observations(db, connector, batch(first))
    assert advance_work(db) == 1
    work = db.scalar(select(ZktCustodyWork))
    assert work.state == "WAIT_FRAGMENTS" and work.next_attempt_at is None
    assert materialize_packet(db, work) is None
    settle_observations(db, connector, batch(first))
    assert advance_work(db) == 0 and work.evidence_revision == 1
    settle_observations(db, connector, batch(second))
    assert work.evidence_revision == 2 and work.state == "PENDING"
    assert advance_work(db) == 1
    assert work.state == "WAIT_PROFILE" and materialize_packet(db, work) == b"x" * 600
    assert work.assembled_digest == hashlib.sha256(b"x" * 600).hexdigest()
    assert count(db, ZktCustodyWork) == 1 and count(db, ZktCustodyWorkReceipt) == 2


def test_conflicting_raw_extent_cannot_replace_original_or_block_other_packet(custody):
    db, connector = custody
    first, second = packet_fragments()
    raw = bytearray(base64.b64decode(first["raw_b64"]))
    raw[-1] ^= 1
    conflict = observation(3, raw_format="PACKET_FRAGMENT", raw_b64=base64.b64encode(raw).decode(),
                           raw_digest=hashlib.sha256(raw).hexdigest())
    settle_observations(db, connector, batch(first, conflict, second, packet_observation(4)))
    assert advance_work(db) == 2
    work = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert work[0].state == "HELD_EXCEPTION" and work[1].state == "WAIT_PROFILE"
    assert count(db, ZktObservationReceipt) == count(db, ZktCustodyWorkReceipt) == 4
    receipt = db.scalar(select(ZktObservationReceipt).order_by(ZktObservationReceipt.id))
    assert decrypt_json(receipt.protected_observation)["observation"] == first
    settle_observations(db, connector, batch(first))
    assert work[0].state == "HELD_EXCEPTION" and advance_work(db) == 0


def test_conflicting_profile_metadata_holds_group_even_after_more_good_fragments(custody):
    db, connector = custody
    first, second = packet_fragments()
    second["decoder_profile"] = "different-profile"
    settle_observations(db, connector, batch(first, second))
    work = db.scalar(select(ZktCustodyWork))
    assert work.state == "HELD_EXCEPTION" and work.reason_code == "CONFLICTING_PACKET_METADATA"
    assert advance_work(db) == 0


def test_unreadable_ciphertext_remains_retryable_and_isolated(custody):
    db, connector = custody
    settle_observations(db, connector, batch(packet_observation(1), packet_observation(2)))
    receipt = db.scalar(select(ZktObservationReceipt).order_by(ZktObservationReceipt.id))
    original = receipt.protected_observation
    receipt.protected_observation = "corrupt encrypted bytes"
    db.flush()
    assert advance_work(db) == 2
    first, second = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert first.state == "RETRY_SYSTEM" and first.next_attempt_at is not None
    assert first.reason_code == "CUSTODY_DECRYPT_UNAVAILABLE"
    assert second.state == "WAIT_PROFILE"
    receipt.protected_observation = original
    first.next_attempt_at = utc_now()
    db.flush()
    assert advance_work(db) == 1
    assert first.state == "WAIT_PROFILE" and first.attempt_count == 2


def test_precontract_receipts_are_repaired_in_bounded_idempotent_pages(custody):
    db, connector = custody
    settle_observations(db, connector, batch(observation(1), observation(2), packet_observation(3)))
    db.execute(delete(ZktCustodyWorkReceipt))
    db.execute(delete(ZktCustodyWork))
    db.commit()
    assert backfill_work(db, limit=1) == 1
    assert backfill_work(db, limit=1) == 1
    assert backfill_work(db, limit=100) == 1
    assert backfill_work(db, limit=100) == 0
    assert count(db, ZktObservationReceipt) == count(db, ZktCustodyWorkReceipt) == 3


def test_receiver_cannot_ack_without_committed_processing_obligation(custody, monkeypatch):
    from contextlib import contextmanager
    import pytest
    from zk_add import web, zkt_custody_work
    from zk_add.schemas import Envelope

    db, connector = custody
    @contextmanager
    def scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    def unavailable(*args):
        raise RuntimeError("work persistence unavailable")
    monkeypatch.setattr(web, "session_scope", scope)
    monkeypatch.setattr(zkt_custody_work, "attach_work", unavailable)
    envelope = Envelope(connector_id=connector.connector_id, message_id="work-failure", boot_id="test",
                        seq=1, sent_at=utc_now(), type="zkt_observation_batch", payload=batch(observation()))
    with pytest.raises(RuntimeError, match="work persistence unavailable"):
        web.persist_envelope(connector.id, envelope)
    assert count(db, ZktObservationReceipt) == count(db, ZktCustodyWork) == count(db, ZktCustodyWorkReceipt) == 0


def test_connector_with_large_backlog_cannot_starve_another_connector(custody):
    db, connector = custody
    settle_observations(db, connector, batch(*(packet_observation(i) for i in range(1, 21))))
    second = Connector(connector_id="second", hardware_id="aa:bb:cc:dd:ee:02", zone_id="TEST2",
                       zone_name="TEST2", device_id="TEST2", display_name="TEST2", zkt_custody_enabled=True)
    db.add(second)
    db.flush()
    second.zkt_device = ZKTDevice(connector_id=second.id, serial="TEST02", confirmed_serial="TEST02")
    db.flush()
    value = packet_observation(1, terminal_serial="TEST02")
    value["observation_id"] = observation_id("TEST02", value["capture_epoch"], 1)
    settle_observations(db, second, batch(value))
    assert advance_work(db, limit=2) == 2
    other = db.scalar(select(ZktCustodyWork).where(ZktCustodyWork.connector_id == second.id))
    assert other.state == "WAIT_PROFILE"
    remaining = db.scalars(select(ZktCustodyWork).where(ZktCustodyWork.state == "PENDING")).all()
    assert len(remaining) == 19


def test_work_status_is_scoped_paginated_and_contains_no_protected_bytes(custody):
    import json
    db, connector = custody
    settle_observations(db, connector, batch(packet_observation(1), packet_observation(2)))
    status = work_status(db, connector, limit=1)
    assert status["connector_id"] == connector.connector_id
    assert status["enabled"] and not status["missing_processing_obligation"]
    assert len(status["rows"]) == 1 and status["next_cursor"] == status["rows"][0]["id"]
    next_page = work_status(db, connector, limit=1, before=status["next_cursor"])
    assert len(next_page["rows"]) == 1 and next_page["next_cursor"] is None
    rendered = json.dumps(status, default=str)
    assert "raw_b64" not in rendered and "protected_" not in rendered and "synthetic packet" not in rendered
    db.execute(delete(ZktCustodyWorkReceipt).where(ZktCustodyWorkReceipt.id == 1))
    assert work_status(db, connector)["missing_processing_obligation"]


def test_source_commit_after_custody_is_associated_without_replaying_esp(custody):
    from zk_add.models import TerminalSourceEpoch, TerminalRecordManifest, ZktOccurrenceAlias, ZktObservationLink
    db, connector = custody
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    def source(sequence, ordinal):
        return observation(sequence, raw_format="SOURCE_RECORD",
                           occurrence={"source_epoch": epoch.epoch_id, "ordinal": ordinal})
    result = settle_observations(db, connector, batch(source(1, 0), source(2, 1)))
    db.commit()
    assert advance_work(db) == 2
    work = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert all(row.state == "WAIT_SOURCE" and row.reason_code == "CANONICAL_SOURCE_PENDING"
               and row.next_attempt_at is not None for row in work)
    assert advance_work(db) == 0
    for ordinal in (0, 1):
        db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=ordinal,
            canonical_source=True, raw_record_digest=observation()["raw_digest"],
            terminal_record_key=observation()["raw_digest"], disposition="MALFORMED"))
    for row in work:
        row.next_attempt_at = utc_now()
    db.commit()
    assert advance_work(db) == 2
    assert all(row.state == "SOURCE_ASSOCIATED" and row.next_attempt_at is None for row in work)
    assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 2
    # Identical bytes at distinct ordinals remain distinct occurrences.
    assert len(set(db.scalars(select(ZktOccurrenceAlias.occurrence_id)))) == 2
    assert advance_work(db) == 0
    replay = settle_observations(db, connector, batch(source(1, 0)))
    assert replay["items"][0]["receipt_id"] == result["items"][0]["receipt_id"]
    assert count(db, ZktObservationReceipt) == 2
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_unreferenced_source_and_changed_binding_are_explicit_holds(custody):
    db, connector = custody
    settle_observations(db, connector, batch(observation(1, raw_format="SOURCE_RECORD")))
    assert advance_work(db) == 1
    row = db.scalar(select(ZktCustodyWork))
    assert row.state == "WAIT_SOURCE" and row.reason_code == "SOURCE_REFERENCE_REQUIRED"
    assert row.next_attempt_at is None
    connector.zkt_device.confirmed_serial = None
    row.next_attempt_at = utc_now()
    db.flush()
    assert advance_work(db) == 1
    assert row.reason_code == "TERMINAL_BINDING_CHANGED" and row.next_attempt_at is None


def test_late_association_failure_isolated_by_savepoint(custody, monkeypatch):
    from zk_add import zkt_custody
    from zk_add.models import TerminalSourceEpoch, TerminalRecordManifest, ZktOccurrenceAlias, ZktObservationLink
    db, connector = custody
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    values = [observation(index + 1, raw_format="SOURCE_RECORD",
                          occurrence={"source_epoch": epoch.epoch_id, "ordinal": index}) for index in (0, 1)]
    settle_observations(db, connector, batch(*values))
    for ordinal in (0, 1):
        db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=ordinal,
            canonical_source=True, raw_record_digest=observation()["raw_digest"],
            terminal_record_key=observation()["raw_digest"], disposition="MALFORMED"))
    db.commit()
    original = zkt_custody.bind_source_occurrence
    def interrupted(session, connector, receipt, value):
        result = original(session, connector, receipt, value)
        if value.capture_sequence == 1:
            raise zkt_custody.SourceAssociationError("SOURCE_ASSOCIATION_CONFLICT")
        return result
    monkeypatch.setattr(zkt_custody, "bind_source_occurrence", interrupted)
    assert advance_work(db) == 2
    first, second = db.scalars(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).all()
    assert first.state == "HELD_EXCEPTION" and first.reason_code == "SOURCE_ASSOCIATION_CONFLICT"
    assert second.state == "SOURCE_ASSOCIATED"
    assert count(db, ZktObservationReceipt) == 2
    assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 1


def test_a_replayed_receipt_cannot_bind_after_terminal_confirmation_changes(custody):
    from zk_add.models import TerminalSourceEpoch, TerminalRecordManifest, ZktObservationLink
    db, connector = custody
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    value = observation(raw_format="SOURCE_RECORD", occurrence={"source_epoch": epoch.epoch_id, "ordinal": 0})
    original = settle_observations(db, connector, batch(value))
    db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
        terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=0,
        canonical_source=True, raw_record_digest=value["raw_digest"], terminal_record_key=value["raw_digest"],
        disposition="MALFORMED"))
    connector.zkt_device.confirmed_serial = "ANOTHER-TERMINAL"
    db.commit()
    replay = settle_observations(db, connector, batch(value))
    assert replay["items"][0]["receipt_id"] == original["items"][0]["receipt_id"]
    assert replay["items"][0]["occurrence_id"] is None
    assert count(db, ZktObservationLink) == 0


def test_source_conflict_preserves_custody_and_does_not_stall_next_item(custody):
    from zk_add.models import TerminalSourceEpoch, TerminalRecordManifest, ZktObservationLink
    db, connector = custody
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    for ordinal in (0, 1):
        db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=ordinal,
            canonical_source=True, raw_record_digest="0" * 64 if ordinal == 0 else observation()["raw_digest"],
            terminal_record_key=observation()["raw_digest"], disposition="MALFORMED"))
    db.commit()
    values = [observation(index + 1, raw_format="SOURCE_RECORD",
                          occurrence={"source_epoch": epoch.epoch_id, "ordinal": index}) for index in (0, 1)]
    receipt = settle_observations(db, connector, batch(*values))
    db.commit()
    assert receipt["committed"] and len(receipt["items"]) == 2
    assert receipt["items"][0]["occurrence_id"] is None and receipt["items"][1]["occurrence_id"]
    assert count(db, ZktObservationReceipt) == 2 and count(db, ZktObservationLink) == 1
    first = db.scalar(select(ZktCustodyWork).order_by(ZktCustodyWork.id))
    assert first.state == "HELD_EXCEPTION" and first.reason_code == "SOURCE_RAW_DIGEST_MISMATCH"
    assert first.next_attempt_at is None
    replay = settle_observations(db, connector, batch(values[0]))
    assert replay["items"][0]["receipt_id"] == receipt["items"][0]["receipt_id"]
    assert count(db, ZktObservationReceipt) == 2

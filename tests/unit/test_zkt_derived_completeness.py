"""A valid encrypted prefix is not a complete interpretation certificate."""
from copy import deepcopy
import hashlib

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from test_zkt_custody import custody as custody, batch, count
from test_zkt_custody_work import packet_fragments, packet_observation
from test_zkt_decode import live, packet
from test_zkt_derived_evidence import source_observation
from test_zkt_source_load import store as store
from zk_add import zkt_custody_work as work, zkt_derived_evidence as derived
from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.models import AttendanceEvent, Connector, OrdsOutbox, ZktCustodyWork, ZktDerivedEvidence, ZktObservationReceipt
from zk_add.settings import settings
from zk_add.zkt_custody import observation_id, settle_observations


def prepare(custody, *, complete=True):
    db, connector = custody
    raw = packet(live(12) * 129)
    settle_observations(db, connector, batch(*packet_fragments(raw)))
    db.commit()
    if complete:
        while work.advance_work(db):
            db.commit()
    else:
        assert work.advance_work(db) == 1
        db.commit()
    return raw, db.scalar(select(ZktCustodyWork)), list(db.scalars(select(ZktDerivedEvidence)
        .order_by(ZktDerivedEvidence.step_index)))


@pytest.mark.parametrize("missing", ["none", "tail", "in_progress"])
def test_absent_incomplete_and_truncated_chains_cannot_complete(custody, missing):
    db, _ = custody
    raw, obligation, rows = prepare(custody, complete=missing != "in_progress")
    if missing == "none":
        for row in rows:
            db.delete(row)
    elif missing == "tail":
        db.delete(rows[-1])
    db.commit()
    receipts = list(db.scalars(select(ZktObservationReceipt.protected_observation)))
    with pytest.raises(derived.DerivedEvidenceInvalid, match="DERIVED_INCOMPLETE"):
        list(derived.verified_steps(db, obligation, raw))
    assert list(db.scalars(select(ZktObservationReceipt.protected_observation))) == receipts
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def rewrite_chain(db, rows, mutation):
    """Model an internally produced, correctly encrypted but inconsistent step.

    Key access is synthetic. This verifies application invariants in addition
    to authenticated storage, not a claim about an attacker with the key.
    """
    previous = None
    for index, row in enumerate(rows):
        value = deepcopy(decrypt_json(row.protected_evidence))
        if index == 0:
            mutation(value)
        row.previous_digest = value["previous_digest"] = previous
        row.record_count, row.error_count, row.result = value["record_count"], value["error_count"], value["result"]
        row.evidence_digest = derived._digest(value)
        row.protected_evidence = encrypt_json(value)
        previous = row.evidence_digest
    db.commit()


@pytest.mark.parametrize("fault", ["drop_record", "duplicate_offset", "raw_digest", "record_length",
    "fact_offset", "fact_length", "error_count", "progress_skip", "progress_error", "wrong_plausible",
    "input_digest", "input_length", "receipt_revision", "decoder_version", "reported_profile"])
def test_authenticated_internal_inconsistency_cannot_prove_record_coverage(custody, fault):
    db, _ = custody
    raw, obligation, rows = prepare(custody)
    def mutate(value):
        record = value["records"][0]
        if fault == "drop_record":
            value["records"].pop()
        elif fault == "duplicate_offset":
            value["records"][1] = deepcopy(record)
        elif fault == "raw_digest":
            record["raw_digest"] = "e" * 64
        elif fault == "record_length":
            record["length"] = 32
        elif fault == "fact_offset":
            record["facts"]["offset"] += 12
        elif fault == "fact_length":
            record["facts"]["length"] = 32
        elif fault == "error_count":
            value["error_count"] += 1
        elif fault == "progress_skip":
            value["plan"]["layouts"][0]["processed"] += 1
        elif fault == "progress_error":
            value["plan"]["layouts"][0]["errors"] += 1
        elif fault == "wrong_plausible":
            value["plausible_layouts"] = [12]
        elif fault == "input_digest":
            value["input_digest"] = "e" * 64
        elif fault == "input_length":
            value["input_bytes"] -= 1
        elif fault == "receipt_revision":
            value["evidence_revision"] += 1
        elif fault == "decoder_version":
            value["decoder_version"] = "wrong-version"
        elif fault == "reported_profile":
            value["reported_profile"] = "wrong-profile"
    rewrite_chain(db, rows, mutate)
    with pytest.raises(derived.DerivedEvidenceInvalid):
        list(derived.verified_steps(db, obligation, raw))
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


@pytest.mark.parametrize("size", [8, 16, 40])
def test_complete_source_chain_retains_original_bytes_and_historical_uid(custody, size):
    import base64
    db, connector = custody
    value = source_observation(size)
    raw = base64.b64decode(value["raw_b64"])
    settle_observations(db, connector, batch(value))
    while work.advance_work(db):
        db.commit()
    obligation = db.scalar(select(ZktCustodyWork))
    evidence = list(derived.verified_steps(db, obligation, raw))
    assert len(evidence) == 1 and evidence[0]["input_digest"] == hashlib.sha256(raw).hexdigest()
    facts = evidence[0]["records"][0]["facts"]
    assert facts["attendance_uid"] == (None if size == 16 else 456)
    assert facts["user_id"] == (None if size == 8 else "123")
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_historical_decoder_chain_requires_explicit_version_without_reinterpretation(custody, monkeypatch):
    db, _ = custody
    raw, obligation, rows = prepare(custody)
    old_version, old_decoder = derived.INTERPRETATION_VERSION, derived.DECODER_VERSION
    original = [row.protected_evidence for row in rows]
    monkeypatch.setattr(derived, "INTERPRETATION_VERSION", "synthetic-decoder-2/derived-1")
    monkeypatch.setattr(derived, "DECODER_VERSION", "synthetic-decoder-2")
    while work.advance_work(db):
        db.commit()
    assert [row.protected_evidence for row in rows] == original
    assert list(derived.verified_steps(db, obligation, raw))[-1]["decoder_version"] == "synthetic-decoder-2"
    assert list(derived.verified_steps(db, obligation, raw, version=old_version,
        decoder_version=old_decoder))[-1]["decoder_version"] == old_decoder
    with pytest.raises(derived.DerivedEvidenceInvalid, match="PROVENANCE"):
        list(derived.verified_steps(db, obligation, raw, version=old_version))


def test_chain_completion_after_committed_resume_and_deleted_tail(store, monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    with store() as db:
        connector = db.scalar(select(Connector))
        connector.zkt_custody_enabled = True
        raw = packet(live(12) * 129)
        values = packet_fragments(raw)
        for value in values:
            value["terminal_serial"] = "TEST-LOAD"
            value["observation_id"] = observation_id("TEST-LOAD", value["capture_epoch"], value["capture_sequence"])
        settle_observations(db, connector, batch(*values))
        db.commit()
        assert work.advance_work(db, limit=1) == 1
        db.commit()
        db.expire_all()
        obligation = db.scalar(select(ZktCustodyWork))
        with pytest.raises(derived.DerivedEvidenceInvalid, match="INCOMPLETE"):
            list(derived.verified_steps(db, obligation, raw))
        while work.advance_work(db, limit=1):
            db.commit()
        evidence = list(derived.verified_steps(db, obligation, raw))
        assert sum(row["record_count"] for row in evidence) >= 129
        tail = db.scalar(select(ZktDerivedEvidence).order_by(ZktDerivedEvidence.step_index.desc()))
        db.delete(tail)
        db.commit()
        db.expire_all()
        with pytest.raises(derived.DerivedEvidenceInvalid, match="INCOMPLETE"):
            list(derived.verified_steps(db, obligation, raw))
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


@pytest.mark.parametrize("raw", [packet(live(32)), packet(live(12) * 8), packet(live(32), command=1)])
def test_complete_unique_ambiguous_and_rejected_chains_remain_unqualified(custody, raw):
    db, connector = custody
    settle_observations(db, connector, batch(packet_observation(1, raw)))
    while work.advance_work(db):
        db.commit()
    obligation = db.scalar(select(ZktCustodyWork))
    evidence = list(derived.verified_steps(db, obligation, raw))
    assert evidence and evidence[-1]["result"] != "PENDING"
    assert all(item["authority"] == "UNQUALIFIED" for item in evidence)
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0

"""A tail ADD cannot apply because the ESP's certificate is stale is answered, not looped.

Production shape: ZONE-PESHAWAR-06 (zone-lite-2.7.5, connector generation 5,
40-byte records), 21 Oct 2026, UTC:

* 12:48:49 the ESP reconnects after the ADD deploy. ADD's connect bootstrap
  pushes the identity catalog, then coverage committed to 94777.
* 12:51:36 the ESP's tail 94777..94780 commits (94779 is INVALID_TIME), and
  the ESP advances to 94780.
* 12:52:03 the ESP applies the bootstrap push it had queued: "Local tail
  cursor was ahead of ADD" (ADD_SOURCE_CHECKPOINT_REPLAY), back to 94777.
* 13:07:09 its next tail starts at 94777 again. No punch arrived in between,
  so it is the same range, but 94779 was the terminal's newest slot and read
  back differently. ADD finds the committed chunk at 94777 with another digest
  and invalidates coverage as SOURCE_TAIL_REPLAY_DIVERGED, without telling
  the ESP.
* 13:22:15 to 16:08:23 every tail is rejected as EVIDENCE_INVALID
  (UNCLASSIFIED_VALUE_ERROR at apply_source_tail_chunk:1678, "no active
  certified source coverage"), the same range every interval, until the
  socket reconnects at 16:18.

ZONE-SLICTOWER-3FL (2.7.2) repeated it on 22 Oct (07:06 reconnect, 07:21
invalidation, 07:36 rejection), and ZONE-LAHORE-01 (2.6.15) on 12 Oct after a
lost tail ACK. A ZKT reader changes its cursor, chain, epoch and generation
only on a source_coverage push, so a generic MESSAGE_REJECTED leaves it
extending the same certificate for as long as its socket stays up.

Judging the rewound replay itself (benign when only unsettled newest-slot
rows differ) belongs to the newest-slot work (SOURCE_TAIL_REPLAY_SUPERSEDED).
These tests use replays that differ elsewhere, which stay divergences there.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from test_reconciliation import (  # noqa: F401
    SERIAL,
    _certify_one_record_baseline,
    _tail_source,
    reconciliation_db,
)
from zk_add import web
from zk_add.models import (
    AttendanceEvent,
    Base,
    Connector,
    DeviceAlert,
    DeviceLog,
    ReconciliationCoverage,
    SourceTailChunk,
    TerminalRecordManifest,
)
from zk_add.reconciliation import (
    apply_source_tail_chunk,
    invalidate_coverage_for_terminal_change,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
    source_epoch_uuid,
)
from zk_add.rejection import rejection_category, rejection_reason, rejection_site
from zk_add.schemas import Envelope, SourceTailChunkRequest
from zk_add.source_tail_authority import (
    SOURCE_TAIL_COVERAGE_INACTIVE,
    SOURCE_TAIL_EPOCH_STALE,
    refuse_tail_without_authority,
)
from zk_add.time_utils import utc_now

P06_GENERATION = 5


def _peshawar_06(session, connector):
    """One certified row, then the shape P06 runs: 2.7.5, generation 5, 40-byte rows."""
    connector.onboarding_generation = P06_GENERATION
    coverage = _certify_one_record_baseline(session, connector, bind_epoch=True)
    connector.firmware_version = "zone-lite-2.7.5"
    connector.zkt_device.attendance_count = 1
    connector.boot_id, connector.last_sequence = "p06-boot", 0
    assert coverage.terminal_generation == P06_GENERATION
    session.commit()
    return coverage


def _tail(db, coverage, *, start=None, end=None, epoch="current", previous=None, latest=None, records=None):
    start = coverage.source_committed_cursor if start is None else start
    end = start + 3 if end is None else end
    draft = SourceTailChunkRequest(
        source_epoch=source_epoch_uuid(db, coverage) if epoch == "current" else epoch,
        terminal_serial=SERIAL,
        terminal_generation=coverage.terminal_generation,
        record_size=40,
        start_ordinal=start,
        end_ordinal=end,
        latest_terminal_count=end if latest is None else latest,
        chunk_digest="0" * 64,
        previous_chain_digest=previous or coverage.source_committed_chain_digest,
        resulting_chain_digest="0" * 64,
        records=records or [_tail_source(ordinal, "EVENT" if ordinal == start else "INVALID_TIME",
                                         event=ordinal == start) for ordinal in range(start, end)],
    )
    return _sealed(draft)


def _sealed(draft):
    """The chunk and chain digests the firmware computes for these records."""
    digest = reconciliation_chunk_digest(draft)
    return draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": reconciliation_chain_digest(
        draft.previous_chain_digest, start_ordinal=draft.start_ordinal, end_ordinal=draft.end_ordinal,
        chunk_digest=digest)})


@contextmanager
def _postgres_copy(session):
    """The SQLite fixture's rows in a fresh PostgreSQL schema, as the CI database runs them."""
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "source_tail_authority_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            snapshots = []
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in session.execute(select(table)).mappings()]
                if not rows:
                    continue
                if table.name == "add_zkt_devices":
                    for row in rows:
                        snapshots.append((row["id"], row["identity_snapshot_id"]))
                        row["identity_snapshot_id"] = None
                db.execute(table.insert(), rows)
                if "id" in table.c:
                    db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :maximum)"),
                        {"table": table.name, "maximum": max(row["id"] for row in rows)})
            devices = Base.metadata.tables["add_zkt_devices"]
            for device_id, snapshot_id in snapshots:
                db.execute(devices.update().where(devices.c.id == device_id).values(
                    identity_snapshot_id=snapshot_id))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@dataclass
class Wire:
    factory: sessionmaker
    connector_id: int
    public_id: str
    coverage_id: int
    sequence: int = 0

    def coverage(self, db):
        return db.get(ReconciliationCoverage, self.coverage_id)

    def connector(self, db):
        return db.get(Connector, self.connector_id)

    def envelope(self, request, *, seq=None):
        if seq is None:
            self.sequence += 1
            seq = self.sequence
        return Envelope(message_id=str(uuid4()), connector_id=self.public_id, boot_id="p06-boot",
            seq=seq, sent_at=utc_now(), type="source_tail_chunk", payload=request.model_dump(mode="json"))

    def send(self, request):
        """The production WebSocket path: persist_envelope in its own committed transaction."""
        return web.persist_envelope(self.connector_id, self.envelope(request))

    def count(self, model):
        with self.factory() as db:
            return db.scalar(select(func.count(model.id)))


@pytest.fixture(params=["sqlite", "postgres"])
def wire(request, reconciliation_db, monkeypatch):  # noqa: F811
    session, connector = reconciliation_db
    coverage = _peshawar_06(session, connector)
    ids = dict(connector_id=connector.id, public_id=connector.connector_id, coverage_id=coverage.id)
    with (_postgres_copy(session) if request.param == "postgres" else _sqlite(session)) as engine:
        factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)

        @contextmanager
        def scope():
            with factory() as db:
                try:
                    yield db
                    db.commit()
                except Exception:
                    db.rollback()
                    raise

        monkeypatch.setattr(web, "session_scope", scope)
        yield Wire(factory=factory, **ids)


@contextmanager
def _sqlite(session):
    yield session.get_bind()


def _assert_firmware_parses(push, *, active):
    """The fields add_source_parse_coverage requires of every source_coverage push."""
    assert push["type"] == "source_coverage" and push["active"] is active
    assert isinstance(push["terminal_serial"], str) and push["terminal_serial"] == SERIAL
    assert isinstance(push["terminal_generation"], int) and push["terminal_generation"] >= 1
    assert isinstance(push["source_committed_cursor"], int) and push["source_committed_cursor"] >= 0
    assert re.fullmatch(r"[0-9a-f]{64}", push["source_committed_chain_digest"])
    if active:
        assert push["source_epoch"]


def test_p06_tail_after_coverage_invalidation_stops_the_esp_instead_of_looping(wire):
    with wire.factory() as db:
        accepted = wire.send(_tail(db, wire.coverage(db)))
    assert accepted.ack["type"] == "source_tail_ack" and accepted.ack["committed_next_ordinal"] == 4
    # Coverage is invalidated fail-closed outside the tail reply (here by a
    # heartbeat count check). Nothing tells the ESP, which keeps extending its
    # certificate from its saved cursor.
    with wire.factory() as db:
        zkt = wire.connector(db).zkt_device
        zkt.attendance_count = 0
        assert invalidate_coverage_for_terminal_change(
            db, zkt=zkt, previous_serial=SERIAL, previous_attendance_count=4)
        db.commit()
        stale = _tail(db, wire.coverage(db), start=4, end=7)
    # This is the EVIDENCE_INVALID production saw from 13:22: an unclassified
    # ValueError at the coverage lookup, the same for every retry of the range.
    with wire.factory() as db, pytest.raises(ValueError) as refused:
        apply_source_tail_chunk(db, connector=wire.connector(db), payload=stale)
    assert rejection_category(refused.value) == "EVIDENCE_INVALID"
    assert rejection_reason(refused.value) == "UNCLASSIFIED_VALUE_ERROR"
    assert rejection_site(refused.value).startswith("zk_add.reconciliation:apply_source_tail_chunk:")

    before = tuple(wire.count(model) for model in (SourceTailChunk, TerminalRecordManifest, AttendanceEvent))
    for _ in range(2):
        outcome = wire.send(stale)
        assert outcome.ack == {"type": "error", "message_id": outcome.ack["message_id"],
            "message_type": "source_tail_chunk", "code": SOURCE_TAIL_COVERAGE_INACTIVE, "duplicate": False}
        _assert_firmware_parses(outcome.coverage, active=False)
        assert outcome.coverage["terminal_generation"] == P06_GENERATION
        assert outcome.coverage["source_committed_cursor"] == 0
    # Nothing was accounted, certified or delivered from the refused range.
    assert tuple(wire.count(model) for model in (SourceTailChunk, TerminalRecordManifest, AttendanceEvent)) == before
    with wire.factory() as db:
        current = wire.coverage(db)
        assert not current.active and current.source_committed_cursor == 4
        # A structured answer is not a rejected device message.
        assert db.scalar(select(DeviceAlert.id).where(
            DeviceAlert.code == "DEVICE_MESSAGE_REJECTED", DeviceAlert.state == "OPEN")) is None
        stored = wire.connector(db)
        assert stored.last_sequence == 3 and stored.last_error_code is None
        logs = db.scalars(select(DeviceLog).where(
            DeviceLog.code == SOURCE_TAIL_COVERAGE_INACTIVE).order_by(DeviceLog.id)).all()
        assert len(logs) == 2 and all(row.level == "WARN" for row in logs)
        assert logs[0].context["start_ordinal"] == 4 and logs[0].context["coverage_id"] is None


def test_tail_that_invalidates_coverage_also_withdraws_the_esp_certificate(wire):
    with wire.factory() as db:
        diverged = _tail(db, wire.coverage(db), previous="f" * 64)
    outcome = wire.send(diverged)
    assert outcome.ack["type"] == "error" and outcome.ack["code"] == "SOURCE_TAIL_CHAIN_DIVERGED"
    _assert_firmware_parses(outcome.coverage, active=False)
    with wire.factory() as db:
        assert not wire.coverage(db).active
    # A copy still in flight, or sent before the push was applied, is answered
    # the same way instead of being rejected every interval.
    retry = wire.send(diverged)
    assert retry.ack["code"] == SOURCE_TAIL_COVERAGE_INACTIVE
    _assert_firmware_parses(retry.coverage, active=False)
    assert wire.count(SourceTailChunk) == 0


def test_stale_epoch_in_the_same_generation_resyncs_to_add_coverage(wire):
    with wire.factory() as db:
        wire.send(_tail(db, wire.coverage(db)))
    with wire.factory() as db:
        coverage = wire.coverage(db)
        before = (coverage.active, coverage.source_committed_cursor, coverage.source_committed_chain_digest)
        epoch = source_epoch_uuid(db, coverage)
        outcome = wire.send(_tail(db, coverage, epoch=str(uuid4())))
    assert outcome.ack["code"] == SOURCE_TAIL_EPOCH_STALE and outcome.ack["type"] == "error"
    _assert_firmware_parses(outcome.coverage, active=True)
    assert outcome.coverage["source_epoch"] == epoch
    assert outcome.coverage["source_committed_cursor"] == 4
    assert outcome.coverage["source_committed_chain_digest"] == before[2]
    assert outcome.coverage["terminal_generation"] == P06_GENERATION
    with wire.factory() as db:
        coverage = wire.coverage(db)
        assert (coverage.active, coverage.source_committed_cursor,
                coverage.source_committed_chain_digest) == before
        # Resuming from the pushed coverage commits normally.
        resumed = wire.send(_tail(db, coverage))
    assert wire.count(SourceTailChunk) == 2
    assert resumed.ack["type"] == "source_tail_ack" and resumed.ack["committed_next_ordinal"] == 7


def test_missing_required_epoch_is_answered_with_add_coverage(wire):
    with wire.factory() as db:
        wire.connector(db).zkt_custody_enabled = True
        db.commit()
        epoch = source_epoch_uuid(db, wire.coverage(db))
        outcome = wire.send(_tail(db, wire.coverage(db), epoch=None))
    assert outcome.ack["code"] == SOURCE_TAIL_EPOCH_STALE
    assert outcome.coverage["source_epoch"] == epoch


def test_only_stale_certificates_are_answered_here(wire):
    with wire.factory() as db:
        current = _tail(db, wire.coverage(db))
        connector = wire.connector(db)
        # A current certificate goes to the ordinary path.
        assert refuse_tail_without_authority(db, connector=connector, envelope=wire.envelope(current, seq=900)) is None
        # Another generation or terminal is not this module's to judge.
        stale_epoch = str(uuid4())
        for update in ({"terminal_generation": P06_GENERATION - 1, "source_epoch": stale_epoch},
                       {"terminal_generation": P06_GENERATION + 1, "source_epoch": stale_epoch},
                       {"terminal_serial": "ANOTHER-TERMINAL", "source_epoch": stale_epoch}):
            other = wire.envelope(current.model_copy(update=update), seq=900)
            assert refuse_tail_without_authority(db, connector=connector, envelope=other) is None
        assert not db.new and not db.dirty
    # Evidence refusals stay refusals, with their reason code.
    first = current.records[0]
    live = current.model_copy(update={"records": [
        first.model_copy(update={"event": first.event.model_copy(update={"source": "LIVE"})}),
        *current.records[1:]]})
    with pytest.raises(ValueError) as refused:
        wire.send(live)
    assert rejection_reason(refused.value) == "SOURCE_EVENT_CAPTURE_TYPE_INVALID"
    with wire.factory() as db:
        assert wire.coverage(db).active and wire.coverage(db).source_committed_cursor == 1



def test_p06_replay_divergence_withdraws_the_certificate_and_retries_are_answered(wire):
    """13:07 to 16:08: the invalidating replay, then a retry every tail interval."""
    with wire.factory() as db:
        coverage = wire.coverage(db)
        start, chain = coverage.source_committed_cursor, coverage.source_committed_chain_digest
        first = _tail(db, coverage)
    committed = wire.send(first)
    assert committed.ack["type"] == "source_tail_ack" and committed.ack["committed_next_ordinal"] == start + 3
    before = tuple(wire.count(model) for model in (SourceTailChunk, TerminalRecordManifest, AttendanceEvent))
    # The rewound ESP resends the committed range from 94777. Here a row that
    # is not the newest slot differs, which stays a source divergence.
    other = _tail_source(start + 40, "INVALID_TIME")
    changed = first.records[0].model_copy(update={
        "raw_record_b64": other.raw_record_b64, "raw_record_digest": other.raw_record_digest})
    with wire.factory() as db:
        replay = _tail(db, wire.coverage(db), start=start, previous=chain, records=[changed, *first.records[1:]])
    outcome = wire.send(replay)
    assert outcome.ack["type"] == "error" and outcome.ack["code"] == "SOURCE_TAIL_REPLAY_DIVERGED"
    # The same reply withdraws the ESP's certificate, so it stops resending.
    _assert_firmware_parses(outcome.coverage, active=False)
    assert outcome.coverage["terminal_generation"] == P06_GENERATION
    # A retry already in flight is answered, never rejected at the coverage lookup.
    for _ in range(2):
        retry = wire.send(replay)
        assert retry.ack["code"] == SOURCE_TAIL_COVERAGE_INACTIVE
        _assert_firmware_parses(retry.coverage, active=False)
    assert tuple(wire.count(model) for model in (SourceTailChunk, TerminalRecordManifest, AttendanceEvent)) == before
    with wire.factory() as db:
        current = wire.coverage(db)
        assert not current.active and current.invalidated_reason == "SOURCE_TAIL_REPLAY_DIVERGED"
        assert current.source_committed_cursor == start + 3
        assert db.scalar(select(DeviceAlert.id).where(DeviceAlert.code == "DEVICE_MESSAGE_REJECTED")) is None
        assert db.scalar(select(DeviceAlert.id).where(
            DeviceAlert.code == "ADD_SOURCE_COVERAGE_INVALIDATED", DeviceAlert.state == "OPEN")) is not None
        assert wire.connector(db).last_error_code is None

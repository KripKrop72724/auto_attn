"""A coverage push queued while the reader waits for an ACK must not undo that ACK.

Production, ZONE-PESHAWAR-06 (zone-lite-2.7.5) on 21 Oct 2026 and
ZONE-SLICTOWER-3FL (zone-lite-2.7.2) on 22 Oct, device logs (UTC):

* 12:48:49 / 07:06:10 ADD_RECONNECTED. ADD pushes the identity catalog, then
  source coverage committed to cursor C. The reader's tail from C, sent while
  the bootstrap streamed, waits behind it.
* 12:52:02 / 07:06:37 CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED: ADD commits
  C..C+3 and the ACK moves the reader to C+3.
* 12:52:03 / 07:06:38 ADD_SOURCE_CHECKPOINT_REPLAY, "Local tail cursor was
  ahead of ADD": the reader applies the push it queued while it waited for
  that ACK, and steps back to C.
* 13:07:09 / 07:21:38 its next tail resends from C and ADD invalidates
  coverage as SOURCE_TAIL_REPLAY_DIVERGED; from 13:22:15 / 07:36:39 every tail
  was rejected until the socket reconnected.

ZONE-LAHORE-01 (zone-lite-2.6.15) on 12 Oct fits the same race with a longer
bootstrap: 12:06:54 reconnect, 12:07:57 CURRENT_TAIL_CHECKPOINT_RETRY while the
catalog was still streaming (applied 12:08:06), then a replay divergence at
12:23:02. A tail held behind the bootstrap past the reader's 30 s ACK wait is
committed by ADD while the reader keeps C.

Every Zone Lite generation (2.4.12 through 2.7.6) keeps source_coverage pushes
in a queue of one (xQueueOverwrite) and applies the queued push only at the
top of its main loop, after the step that waits for an ACK returns. Its
WebSocket task handles frames in order, and the gateway task may take the
queue as soon as the ACK releases its step, before a later frame is parsed.
So whenever the session still owes the reader a push, ADD sends its coverage
as of a positive tail or manifest ACK just before that ACK.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from test_device_stream_session import FakeWebSocket, until
from test_device_stream_session import envelope as device_envelope
from test_reconciliation import RAW_RECORD, _source_record, reconciliation_db  # noqa: F401
from test_source_tail_authority import (  # noqa: F401
    P06_GENERATION,
    SERIAL,
    Wire,
    _assert_firmware_parses,
    _postgres_copy,
    _sqlite,
    _tail,
    wire,
)
from zk_add import web
from zk_add.device_session import DeviceStreamSession
from zk_add.models import DeviceAlert, ReconciliationCoverage, SourceTailChunk
from zk_add.realtime import BrowserEventHub, ConnectorHub
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    create_reconciliation_job,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
    source_epoch_uuid,
)
from zk_add.schemas import (
    Envelope,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
)
from zk_add.source_tail_authority import SOURCE_TAIL_EPOCH_STALE


@dataclass
class ZoneLiteSourceReader:
    """The reader's source state as add_connector.c and zone_lite.c keep it.

    ``receive`` is the WebSocket task: a source_coverage push overwrites the
    queue of one, and the ACK it waits for releases the waiting tail or
    manifest step. ``loop_step`` is the top of the main loop
    (apply_add_source_coverage), which runs only after that step returned.
    """

    cursor: int
    chain: str
    epoch: str | None
    generation: int
    certified: bool = True
    queued: dict | None = None
    rewinds: list[tuple[int, int]] = field(default_factory=list)
    cursors: list[int | None] = field(default_factory=list)

    def receive(self, frame: dict, *, waiting_for=None) -> bool:
        """Handle one frame; True when it releases the step waiting for it."""
        if frame["type"] == "source_coverage":
            self.queued = frame
            return False
        if waiting_for is None or frame.get("message_id") != waiting_for.message_id:
            return False
        request = waiting_for.request
        if isinstance(request, ReconciliationManifestRequest):
            if frame["type"] == "reconcile_manifest_ack":
                # zone_lite.c: the positive manifest ACK installs the certificate.
                self.cursor, self.chain = request.cutoff_count, request.final_chain_digest
                self.epoch, self.generation = request.source_epoch, request.terminal_generation
                self.certified = True
        elif (
            frame["type"] == "source_tail_ack"
            and frame["terminal_serial"] == SERIAL
            and (not self.epoch or frame["source_epoch"] == self.epoch)
            and frame["terminal_generation"] == self.generation
            and frame["committed_next_ordinal"] == request.end_ordinal
            and frame["resulting_chain_digest"] == request.resulting_chain_digest
        ):
            self.cursor, self.chain = request.end_ordinal, request.resulting_chain_digest
        return True

    def loop_step(self) -> None:
        push, self.queued = self.queued, None
        if push is not None:
            if push["terminal_serial"] != SERIAL or not push["active"]:
                self.certified = False
            else:
                if self.certified and self.cursor > push["source_committed_cursor"]:
                    # ADD_SOURCE_CHECKPOINT_REPLAY
                    self.rewinds.append((self.cursor, push["source_committed_cursor"]))
                self.cursor = push["source_committed_cursor"]
                self.chain = push["source_committed_chain_digest"]
                self.epoch = push["source_epoch"]
                self.generation = push["terminal_generation"]
                self.certified = True
        self.cursors.append(self.cursor if self.certified else None)

    def deliver(self, frames, *, waiting_for=None, gateway_first=False) -> None:
        """Frames in arrival order, then the next loop pass.

        ``gateway_first`` is the worst case on the ESP32-S3: the gateway task
        (priority 5) takes the queue as soon as the ACK releases its step,
        before the WebSocket task (priority 4) parses any later frame.
        """
        for frame in frames:
            if self.receive(frame, waiting_for=waiting_for) and gateway_first:
                self.loop_step()
        self.loop_step()


@dataclass
class Sent:
    message_id: str
    request: object


def _reader_at(cursor, push, *, certified=True):
    return ZoneLiteSourceReader(
        cursor=cursor,
        chain=push["source_committed_chain_digest"],
        epoch=push.get("source_epoch"),
        generation=push["terminal_generation"],
        certified=certified,
    )


def _stream(wire, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web, "authenticate_stream", lambda connector_id, token: wire.connector_id)
    monkeypatch.setattr(web, "connector_hub", ConnectorHub())
    monkeypatch.setattr(web, "browser_events", BrowserEventHub())
    socket = FakeWebSocket()
    socket.headers = {"X-ADD-Connector-Id": wire.public_id, "Authorization": "Bearer device-token"}
    return socket


def _feed(wire, socket, request, *, message_type="source_tail_chunk") -> Sent:  # noqa: F811
    envelope = wire.envelope(request)
    if message_type != envelope.type:
        envelope = envelope.model_copy(update={"type": message_type})
    socket.feed_text(envelope.model_dump_json())
    return Sent(envelope.message_id, request)


SOURCE_FRAMES = {"source_coverage", "source_tail_ack", "reconcile_manifest_ack", "error"}


def _frames_after(socket, start):
    return [frame for frame in socket.sent[start:] if frame["type"] in SOURCE_FRAMES]


async def _answered(socket, mark, count=1):
    """Wait for ``count`` ACKs after ``mark``, then for anything that might follow them."""
    await until(lambda: sum(frame["type"] != "source_coverage" for frame in _frames_after(socket, mark)) >= count,
                timeout=60)
    await asyncio.sleep(0.05)
    return _frames_after(socket, mark)


def test_p06_bootstrap_push_queued_during_a_tail_no_longer_rewinds_the_reader(wire, monkeypatch):  # noqa: F811
    socket = _stream(wire, monkeypatch)
    with wire.factory() as db:
        coverage = wire.coverage(db)
        start = coverage.source_committed_cursor
        first = _tail(db, coverage)

    async def scenario():
        # The reader's tail is already on the socket when the bootstrap starts.
        sent = _feed(wire, socket, first)
        stream = asyncio.create_task(web.device_stream(socket))
        frames = await _answered(socket, 0)
        connect = list(socket.sent)

        # ADD's coverage as of the ACK goes out before the ACK itself.
        assert [frame["type"] for frame in frames] == ["source_coverage", "source_coverage", "source_tail_ack"]
        bootstrap, resync, ack = frames
        assert bootstrap["active"] and bootstrap["source_committed_cursor"] == start
        assert ack["committed_next_ordinal"] == start + 3
        _assert_firmware_parses(resync, active=True)
        assert (resync["source_committed_cursor"], resync["source_committed_chain_digest"],
                resync["source_epoch"], resync["terminal_generation"]) == (
            ack["committed_next_ordinal"], ack["resulting_chain_digest"], ack["source_epoch"], P06_GENERATION)
        # However the gateway and WebSocket tasks interleave, the reader never
        # steps back, not even for one loop pass.
        for gateway_first in (False, True):
            reader = _reader_at(start, bootstrap)
            reader.deliver(frames, waiting_for=sent, gateway_first=gateway_first)
            assert reader.rewinds == [] and set(reader.cursors) == {start + 3}
            assert reader.chain == ack["resulting_chain_digest"]

        # Without the resync this is exactly what both readers logged.
        production = _reader_at(start, bootstrap)
        production.deliver([bootstrap, ack], waiting_for=sent)
        assert production.rewinds == [(start + 3, start)] and production.cursor == start
        # The same push sent after the ACK loses the race whenever the gateway
        # takes its queue before the push is parsed.
        after_ack = _reader_at(start, bootstrap)
        after_ack.deliver([bootstrap, ack, resync], waiting_for=sent, gateway_first=True)
        assert after_ack.rewinds == [(start + 3, start)]
        # ZONE-LAHORE-01 (2.6.15, 12 Oct): the tail waited behind the bootstrap
        # longer than the reader's 30 s ACK wait, so the ACK found no waiter and
        # the reader kept C. The coverage sent with the ACK still moves it.
        timed_out = _reader_at(start, bootstrap)
        timed_out.deliver(frames)
        assert timed_out.cursor == start + 3 and timed_out.rewinds == []

        # The next tail extends from the ACKed cursor and commits. No push is
        # owed any more, so nothing precedes its ACK.
        with wire.factory() as db:
            following = _tail(db, wire.coverage(db), start=reader.cursor, previous=reader.chain)
        mark = len(socket.sent)
        sent = _feed(wire, socket, following)
        frames = await _answered(socket, mark)
        assert [frame["type"] for frame in frames] == ["source_tail_ack"]
        reader.deliver(frames, waiting_for=sent, gateway_first=True)
        assert reader.cursor == start + 6 and reader.rewinds == []
        socket.disconnect()
        await asyncio.wait_for(stream, 60)
        return connect

    connect = asyncio.run(scenario())
    assert [frame["type"] for frame in connect][:1] == ["identity_catalog_begin"]
    assert wire.count(SourceTailChunk) == 2
    with wire.factory() as db:
        coverage = wire.coverage(db)
        assert coverage.active and coverage.source_committed_cursor == start + 6


def test_stale_epoch_refusal_on_the_stream_resyncs_first_and_keeps_the_rejection_alert(wire, monkeypatch):  # noqa: F811
    socket = _stream(wire, monkeypatch)
    with wire.factory() as db:
        coverage = wire.coverage(db)
        start, epoch = coverage.source_committed_cursor, source_epoch_uuid(db, coverage)
        stale = _tail(db, coverage, epoch=str(uuid4()))
    # The deploy found PESHAWAR-06 with an open rejection alert for its tails.
    web.record_envelope_rejection(wire.connector_id, wire.envelope(stale),
                                  ValueError("Terminal has no active certified source coverage."))

    def rejection_open():
        with wire.factory() as db:
            return db.scalar(select(DeviceAlert.id).where(
                DeviceAlert.code == "DEVICE_MESSAGE_REJECTED", DeviceAlert.state == "OPEN")) is not None

    assert rejection_open()

    async def scenario():
        stream = asyncio.create_task(web.device_stream(socket))
        await until(lambda: socket.sent_of("source_coverage"), timeout=60)
        bootstrap = socket.sent_of("source_coverage")[0]
        # The reader still names a stale epoch (say its NVS save of the pushed
        # epoch failed): its tail is answered with ADD's coverage first, then
        # the error ACK that ends its tail step.
        reader = _reader_at(start, {**bootstrap, "source_epoch": stale.source_epoch})
        mark = len(socket.sent)
        sent = _feed(wire, socket, stale)
        frames = await _answered(socket, mark)
        assert [frame["type"] for frame in frames] == ["source_coverage", "error"]
        resync, refused = frames
        assert refused["code"] == SOURCE_TAIL_EPOCH_STALE and refused["message_id"] == sent.message_id
        _assert_firmware_parses(resync, active=True)
        assert resync["source_epoch"] == epoch and resync["source_committed_cursor"] == start
        reader.deliver(frames, waiting_for=sent, gateway_first=True)
        assert reader.epoch == epoch and reader.cursor == start and reader.rewinds == []
        # A refusal committed no evidence, so the rejection alert stays open.
        assert rejection_open()

        # The resumed tail commits. The refusal's push counts as owed, so ADD's
        # coverage as of the ACK precedes it once more.
        with wire.factory() as db:
            resumed = _tail(db, wire.coverage(db), start=reader.cursor, previous=reader.chain)
        mark = len(socket.sent)
        sent = _feed(wire, socket, resumed)
        frames = await _answered(socket, mark)
        assert [frame["type"] for frame in frames] == ["source_coverage", "source_tail_ack"]
        assert frames[0]["source_committed_cursor"] == frames[1]["committed_next_ordinal"] == start + 3
        reader.deliver(frames, waiting_for=sent, gateway_first=True)
        assert reader.cursor == start + 3 and reader.rewinds == []
        # Only the accepted tail clears the rejection.
        assert not rejection_open()
        socket.disconnect()
        await asyncio.wait_for(stream, 60)

    asyncio.run(scenario())
    assert wire.count(SourceTailChunk) == 1


def _ready_for_manifest(session, connector):
    """P06's shape, one source row captured and chained, the final manifest not yet sent."""
    connector.onboarding_generation = P06_GENERATION
    job = create_reconciliation_job(
        session, connector=connector, actor="operator",
        reason="Certify one source row through the device stream.",
        confirmation="RECONCILE 1 FROM START", idempotency_key="stream-manifest-0001",
    )
    epoch = source_epoch_uuid(session, job)
    connector.firmware_version = "zone-lite-2.7.5"
    apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        source_epoch=epoch, job_id=job.job_id, generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=1, latest_terminal_count=1,
        record_size=len(RAW_RECORD), source_total_bytes=4 + len(RAW_RECORD),
        first_anchor_digest=hashlib.sha256(RAW_RECORD).hexdigest()))
    draft = ReconciliationChunkRequest(
        source_epoch=epoch, job_id=job.job_id, generation=job.terminal_generation, sequence=0,
        start_ordinal=0, end_ordinal=1, chunk_digest="0" * 64, previous_chain_digest=None,
        resulting_chain_digest="0" * 64, records=[_source_record()])
    chunk_digest = reconciliation_chunk_digest(draft)
    chain = reconciliation_chain_digest(None, start_ordinal=0, end_ordinal=1, chunk_digest=chunk_digest)
    apply_reconciliation_chunk(session, connector=connector, payload=draft.model_copy(
        update={"chunk_digest": chunk_digest, "resulting_chain_digest": chain}))
    connector.zkt_device.attendance_count = 1
    connector.boot_id, connector.last_sequence = "p06-boot", 0
    session.commit()
    assert job.terminal_generation == P06_GENERATION
    return ReconciliationManifestRequest(
        source_epoch=epoch, job_id=job.job_id, generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=1, latest_terminal_count=1,
        final_chain_digest=chain)


@pytest.fixture(params=["sqlite", "postgres"])
def manifest_wire(request, reconciliation_db, monkeypatch):  # noqa: F811
    session, connector = reconciliation_db
    manifest = _ready_for_manifest(session, connector)
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
        yield Wire(factory=factory, connector_id=connector.id, public_id=connector.connector_id,
                   coverage_id=0), manifest


def test_bootstrap_placeholder_queued_during_the_final_manifest_keeps_the_new_certificate(
        manifest_wire, monkeypatch):
    stream_wire, manifest = manifest_wire
    socket = _stream(stream_wire, monkeypatch)

    async def scenario():
        # The final manifest is on the socket when a reconnect's bootstrap
        # starts; ADD holds no coverage yet, so the bootstrap withdraws it.
        sent = _feed(stream_wire, socket, manifest, message_type="reconcile_source_manifest")
        stream = asyncio.create_task(web.device_stream(socket))
        frames = await _answered(socket, 0)
        assert [frame["type"] for frame in frames] == [
            "source_coverage", "source_coverage", "reconcile_manifest_ack"]
        placeholder, certified, ack = frames
        _assert_firmware_parses(placeholder, active=False)
        _assert_firmware_parses(certified, active=True)
        assert ack["message_id"] == sent.message_id and ack["source_epoch"] == manifest.source_epoch
        assert (certified["source_committed_cursor"], certified["source_committed_chain_digest"],
                certified["source_epoch"], certified["terminal_generation"]) == (
            manifest.cutoff_count, manifest.final_chain_digest, manifest.source_epoch, P06_GENERATION)
        for gateway_first in (False, True):
            reader = _reader_at(0, placeholder, certified=False)
            reader.deliver(frames, waiting_for=sent, gateway_first=gateway_first)
            assert reader.certified and reader.rewinds == [] and set(reader.cursors) == {manifest.cutoff_count}
            assert reader.epoch == manifest.source_epoch
        # Without it the queued placeholder drops the certificate the ACK installed.
        production = _reader_at(0, placeholder, certified=False)
        production.deliver([placeholder, ack], waiting_for=sent)
        assert not production.certified

        # The first tail on the new certificate owes nothing more.
        with stream_wire.factory() as db:
            coverage = db.scalar(select(ReconciliationCoverage).where(
                ReconciliationCoverage.active == True))  # noqa: E712
            stream_wire.coverage_id = coverage.id
            tail = _tail(db, coverage)
        mark = len(socket.sent)
        sent = _feed(stream_wire, socket, tail)
        frames = await _answered(socket, mark)
        assert [frame["type"] for frame in frames] == ["source_tail_ack"]
        reader.deliver(frames, waiting_for=sent, gateway_first=True)
        assert reader.cursor == manifest.cutoff_count + 3 and reader.rewinds == []
        socket.disconnect()
        await asyncio.wait_for(stream, 60)

    asyncio.run(scenario())
    with stream_wire.factory() as db:
        coverage = db.get(ReconciliationCoverage, stream_wire.coverage_id)
        assert coverage.active and coverage.source_committed_cursor == manifest.cutoff_count + 3


def test_committed_and_duplicate_tails_carry_add_coverage_and_refusals_carry_their_own(wire):  # noqa: F811
    with wire.factory() as db:
        first = _tail(db, wire.coverage(db))
    committed = wire.send(first)
    assert committed.coverage is None and committed.coverage_before_ack is None
    _assert_firmware_parses(committed.ack_coverage, active=True)
    assert committed.ack_coverage["source_committed_cursor"] == committed.ack["committed_next_ordinal"]
    assert committed.ack_coverage["source_committed_chain_digest"] == committed.ack["resulting_chain_digest"]
    with wire.factory() as db:
        second = _tail(db, wire.coverage(db))
    wire.send(second)
    # A lost ACK's resend of the older chunk is ACKed as a duplicate at that
    # chunk's end; the coverage sent with it carries ADD's later cursor.
    duplicate = wire.send(first)
    assert duplicate.ack["duplicate"] is True
    assert duplicate.ack["committed_next_ordinal"] == first.end_ordinal
    assert duplicate.ack_coverage["source_committed_cursor"] == second.end_ordinal
    # An invalidating or refused tail sends its own push, which withdraws the
    # certificate, and never ADD's former coverage.
    with wire.factory() as db:
        diverged = _tail(db, wire.coverage(db), previous="f" * 64)
    refused = wire.send(diverged)
    assert refused.ack["code"] == "SOURCE_TAIL_CHAIN_DIVERGED" and refused.ack_coverage is None
    _assert_firmware_parses(refused.coverage_before_ack, active=False)
    retry = wire.send(diverged)
    assert retry.ack["code"] == "SOURCE_TAIL_COVERAGE_INACTIVE" and retry.ack_coverage is None
    _assert_firmware_parses(retry.coverage_before_ack, active=False)
    assert refused.coverage is None and retry.coverage is None


class PlainSocket:
    """The legacy socket path: no stream session, Starlette's send_json raising on failure."""

    def __init__(self, fail_type=None):
        self.sent = []
        self.fail_type = fail_type

    async def send_json(self, payload):
        if payload.get("type") == self.fail_type:
            raise TypeError("not JSON serializable")
        self.sent.append(payload)


@pytest.mark.parametrize("owed", [False, True])
def test_coverage_goes_before_the_ack_and_only_while_a_push_is_owed(owed):
    envelope = Envelope.model_validate(device_envelope(1, message_type="source_tail_chunk"))
    coverage = {"type": "source_coverage", "active": True, "source_committed_cursor": 4}
    accepted = web.EnvelopeOutcome(ack={"type": "source_tail_ack"}, ack_coverage=coverage)
    answered = web.EnvelopeOutcome(ack={"type": "error"}, coverage_before_ack={
        "type": "source_coverage", "active": False, "source_committed_cursor": 0})

    def committed(outcome):
        async def in_thread(function, *args):
            return outcome
        return in_thread

    async def scenario():
        socket = FakeWebSocket()
        session = DeviceStreamSession(socket, "ZL-STREAM-TEST")
        session.coverage_push_outstanding = owed
        for outcome in (accepted, accepted):
            await web.accept_envelope(7, envelope, session, in_thread=committed(outcome))
        assert not session.coverage_push_outstanding
        # A tail answer's own push precedes its ACK and leaves a push owed.
        await web.accept_envelope(7, envelope, session, in_thread=committed(answered))
        assert session.coverage_push_outstanding
        await web.accept_envelope(7, envelope, session, in_thread=committed(accepted))
        assert not session.coverage_push_outstanding
        # A plain socket gets a tail answer's push, never ADD's coverage as of an ACK.
        plain = PlainSocket()
        for outcome in (accepted, answered):
            await web.accept_envelope(7, envelope, plain, in_thread=committed(outcome))
        # A push that cannot be sent never costs the committed message its ACK.
        failing = PlainSocket(fail_type="source_coverage")
        assert await web.accept_envelope(7, envelope, failing, in_thread=committed(answered)) is answered
        return socket.sent, plain.sent, failing.sent

    stream, plain, failing = asyncio.run(scenario())
    kinds = [(frame["type"], frame.get("source_committed_cursor")) for frame in stream]
    ack, resync, answer, withdraw = ("source_tail_ack", None), ("source_coverage", 4), ("error", None), (
        "source_coverage", 0)
    assert kinds == [*([resync] if owed else []), ack, ack, withdraw, answer, resync, ack]
    assert [frame["type"] for frame in plain] == ["source_tail_ack", "source_coverage", "error"]
    assert failing == [{"type": "error"}]

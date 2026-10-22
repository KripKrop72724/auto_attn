"""A connect bootstrap's coverage push must not rewind a reader that committed a tail meanwhile.

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

Every Zone Lite generation (2.4.12 through 2.7.6) keeps source_coverage pushes
in a queue of one (xQueueOverwrite) and applies the queued push only between
loop steps, after the tail step returns. ADD now follows the ACK of a tail
with its coverage as of that ACK whenever the session still owes the reader
one, which replaces the queued push before it is applied.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from test_device_stream_session import FakeWebSocket, until
from test_device_stream_session import envelope as device_envelope
from test_reconciliation import reconciliation_db  # noqa: F401
from test_source_tail_authority import (  # noqa: F401
    P06_GENERATION,
    SERIAL,
    _assert_firmware_parses,
    _tail,
    wire,
)
from zk_add import web
from zk_add.device_session import DeviceStreamSession
from zk_add.models import SourceTailChunk
from zk_add.realtime import BrowserEventHub, ConnectorHub
from zk_add.reconciliation import source_epoch_uuid
from zk_add.schemas import Envelope


@dataclass
class ZoneLiteTailReader:
    """The reader's source-tail state as add_connector.c and zone_lite.c keep it.

    ``receive`` is the WebSocket task: a source_coverage push overwrites the
    queue of one, an ACK releases the waiting tail step. ``loop_step`` is the
    top of the main loop (apply_add_source_coverage), which runs only after the
    tail step returned.
    """

    cursor: int
    chain: str
    epoch: str | None
    generation: int
    certified: bool = True
    queued: dict | None = None
    rewinds: list[tuple[int, int]] = field(default_factory=list)

    def receive(self, frame: dict, *, waiting_for=None) -> None:
        if frame["type"] == "source_coverage":
            self.queued = frame
        elif waiting_for is not None and frame.get("message_id") == waiting_for.message_id:
            tail = waiting_for.request
            if (
                frame["type"] == "source_tail_ack"
                and frame["terminal_serial"] == SERIAL
                and (not self.epoch or frame["source_epoch"] == self.epoch)
                and frame["terminal_generation"] == self.generation
                and frame["committed_next_ordinal"] == tail.end_ordinal
                and frame["resulting_chain_digest"] == tail.resulting_chain_digest
            ):
                self.cursor, self.chain = tail.end_ordinal, tail.resulting_chain_digest

    def loop_step(self) -> None:
        push, self.queued = self.queued, None
        if push is None:
            return
        if push["terminal_serial"] != SERIAL or not push["active"]:
            self.certified = False
            return
        if self.certified and self.cursor > push["source_committed_cursor"]:
            # ADD_SOURCE_CHECKPOINT_REPLAY
            self.rewinds.append((self.cursor, push["source_committed_cursor"]))
        self.cursor = push["source_committed_cursor"]
        self.chain = push["source_committed_chain_digest"]
        self.epoch = push["source_epoch"]
        self.generation = push["terminal_generation"]
        self.certified = True


@dataclass
class Sent:
    message_id: str
    request: object


def _reader(db, coverage) -> ZoneLiteTailReader:
    return ZoneLiteTailReader(
        cursor=coverage.source_committed_cursor,
        chain=coverage.source_committed_chain_digest,
        epoch=source_epoch_uuid(db, coverage),
        generation=coverage.terminal_generation,
    )


def _stream(wire, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web, "authenticate_stream", lambda connector_id, token: wire.connector_id)
    monkeypatch.setattr(web, "connector_hub", ConnectorHub())
    monkeypatch.setattr(web, "browser_events", BrowserEventHub())
    socket = FakeWebSocket()
    socket.headers = {"X-ADD-Connector-Id": wire.public_id, "Authorization": "Bearer device-token"}
    return socket


def _feed(wire, socket, request) -> Sent:  # noqa: F811
    envelope = wire.envelope(request)
    socket.feed_text(envelope.model_dump_json())
    return Sent(envelope.message_id, request)


def _frames_after(socket, start):
    return [frame for frame in socket.sent[start:] if frame["type"] in {"source_coverage", "source_tail_ack", "error"}]


def test_p06_bootstrap_push_queued_during_a_tail_no_longer_rewinds_the_reader(wire, monkeypatch):  # noqa: F811
    socket = _stream(wire, monkeypatch)
    with wire.factory() as db:
        coverage = wire.coverage(db)
        start = coverage.source_committed_cursor
        reader = _reader(db, coverage)
        first = _tail(db, coverage)

    async def scenario():
        # The reader's tail is already on the socket when the bootstrap starts.
        sent = _feed(wire, socket, first)
        stream = asyncio.create_task(web.device_stream(socket))
        await until(lambda: socket.sent_of("source_tail_ack"), timeout=60)
        await asyncio.sleep(0.05)
        connect = list(socket.sent)

        # The main loop sat in the tail step for all of it, then reached its next step.
        frames = _frames_after(socket, 0)
        assert [frame["type"] for frame in frames] == ["source_coverage", "source_tail_ack", "source_coverage"]
        bootstrap, ack, resync = frames
        assert bootstrap["active"] and bootstrap["source_committed_cursor"] == start
        assert ack["committed_next_ordinal"] == start + 3
        _assert_firmware_parses(resync, active=True)
        assert (resync["source_committed_cursor"], resync["source_committed_chain_digest"],
                resync["source_epoch"], resync["terminal_generation"]) == (
            ack["committed_next_ordinal"], ack["resulting_chain_digest"], ack["source_epoch"], P06_GENERATION)
        for frame in frames:
            reader.receive(frame, waiting_for=sent)
        reader.loop_step()
        assert reader.rewinds == [] and reader.cursor == start + 3

        # Without the resync this is exactly what both readers logged.
        old_add = _reader_at(start, bootstrap)
        for frame in (bootstrap, ack):
            old_add.receive(frame, waiting_for=sent)
        old_add.loop_step()
        assert old_add.rewinds == [(start + 3, start)] and old_add.cursor == start
        # A loop step that runs before the resync arrives rewinds for a moment;
        # the resync then restores the cursor before any later tail is built.
        late = _reader_at(start, bootstrap)
        for frame in (bootstrap, ack):
            late.receive(frame, waiting_for=sent)
        late.loop_step()
        late.receive(resync)
        late.loop_step()
        assert late.cursor == start + 3 and late.chain == ack["resulting_chain_digest"]

        # The next tail extends from the ACKed cursor and commits. No push is
        # owed any more, so its ACK is not followed by another one.
        with wire.factory() as db:
            following = _tail(db, wire.coverage(db), start=reader.cursor, previous=reader.chain)
        mark = len(socket.sent)
        sent = _feed(wire, socket, following)
        await until(lambda: len(socket.sent_of("source_tail_ack")) == 2, timeout=60)
        await asyncio.sleep(0.05)
        frames = _frames_after(socket, mark)
        assert [frame["type"] for frame in frames] == ["source_tail_ack"]
        reader.receive(frames[0], waiting_for=sent)
        reader.loop_step()
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


def _reader_at(cursor, bootstrap):
    return ZoneLiteTailReader(
        cursor=cursor,
        chain=bootstrap["source_committed_chain_digest"],
        epoch=bootstrap["source_epoch"],
        generation=bootstrap["terminal_generation"],
    )


def test_committed_and_duplicate_tails_carry_add_coverage_and_refusals_do_not(wire):  # noqa: F811
    with wire.factory() as db:
        first = _tail(db, wire.coverage(db))
    committed = wire.send(first)
    assert committed.coverage is None
    _assert_firmware_parses(committed.tail_coverage, active=True)
    assert committed.tail_coverage["source_committed_cursor"] == committed.ack["committed_next_ordinal"]
    assert committed.tail_coverage["source_committed_chain_digest"] == committed.ack["resulting_chain_digest"]
    with wire.factory() as db:
        second = _tail(db, wire.coverage(db))
    wire.send(second)
    # A lost ACK's resend of the older chunk is ACKed as a duplicate at that
    # chunk's end; the coverage that may follow it carries ADD's later cursor.
    duplicate = wire.send(first)
    assert duplicate.ack["duplicate"] is True
    assert duplicate.ack["committed_next_ordinal"] == first.end_ordinal
    assert duplicate.tail_coverage["source_committed_cursor"] == second.end_ordinal
    # An answered or invalidating tail never carries it: its coverage push is
    # the one that withdraws or resyncs the certificate.
    with wire.factory() as db:
        diverged = _tail(db, wire.coverage(db), previous="f" * 64)
    refused = wire.send(diverged)
    assert refused.ack["code"] == "SOURCE_TAIL_CHAIN_DIVERGED" and refused.tail_coverage is None
    retry = wire.send(diverged)
    assert retry.ack["code"] == "SOURCE_TAIL_COVERAGE_INACTIVE" and retry.tail_coverage is None


@pytest.mark.parametrize("pushed", [False, True])
def test_tail_coverage_is_sent_only_while_a_push_is_outstanding(pushed):
    async def scenario():
        socket = FakeWebSocket()
        session = DeviceStreamSession(socket, "ZL-STREAM-TEST")
        envelope = Envelope.model_validate(device_envelope(1, message_type="source_tail_chunk"))
        coverage = {"type": "source_coverage", "active": True, "source_committed_cursor": 4}
        if pushed:
            await web.send_envelope_follow_ups(envelope, web.EnvelopeOutcome(
                ack={}, event=None, coverage={**coverage, "source_committed_cursor": 1}), session)
            assert session.coverage_push_outstanding
        outcome = web.EnvelopeOutcome(ack={}, event=None, tail_coverage=coverage)
        await web.send_envelope_follow_ups(envelope, outcome, session)
        await web.send_envelope_follow_ups(envelope, outcome, session)
        assert not session.coverage_push_outstanding
        # A plain socket (no stream session) never receives it.
        plain = FakeWebSocket()

        async def send_json(payload):
            plain.sent.append(payload)

        plain.send_json = send_json
        await web.send_envelope_follow_ups(envelope, outcome, plain)
        assert plain.sent == []
        return socket.sent

    sent = asyncio.run(scenario())
    resyncs = [frame for frame in sent if frame.get("source_committed_cursor") == 4]
    assert len(resyncs) == (1 if pushed else 0)

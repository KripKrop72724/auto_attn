"""Answer a source tail whose certificate ADD no longer holds, or holds under another epoch.

A ZKT reader extends ADD's certified source coverage with bounded
``source_tail_chunk`` messages. It builds each one from its own saved
cursor, chain, epoch and generation, and it changes those only when ADD
pushes ``source_coverage``. ADD pushes coverage on every connect, after a
confirmed source mutation and on a retained-certificate manifest handoff.
Before this module, a tail refused for any other reason was answered with a
generic MESSAGE_REJECTED (EVIDENCE_INVALID). That leaves the reader's state as
it was, so it sends the same range again at every tail interval for as long
as its socket stays up. Each rejection also marks the connector degraded.

Two of those refusals have nothing to do with the records:

* ADD holds no active coverage for the terminal. A tail or heartbeat check
  invalidated it fail-closed, or a confirmed source mutation superseded it,
  and the reader was never told. Every later tail then fails the
  "no active certified source coverage" check.
* ADD's active coverage has a valid epoch for the same terminal serial and
  generation, but the reader names another epoch, or none where one is
  required.

Both are answered here before any evidence is read. ADD sends the same
coverage push a reconnect sends, then a structured error ACK (the push goes
first so the reader has queued it before the ACK releases its tail step): the
inactive placeholder stops the reader's tail, and the active coverage moves it
to ADD's committed cursor, chain and epoch. No evidence is committed: no
ordinal is accounted, no certificate grows, and coverage is not changed. The
transaction keeps only the message sequence and one WARN device log row, and
it does not clear an open rejection alert. Every other refusal keeps the
ordinary path, where evidence refusals stay refusals. A tail in a different
generation is left to that path as well.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.models import Connector, ReconciliationCoverage
from zk_add.rejection import EvidenceRejected
from zk_add.schemas import DeviceLogIn, Envelope, SourceTailChunkRequest

SOURCE_TAIL_COVERAGE_INACTIVE = "SOURCE_TAIL_COVERAGE_INACTIVE"
SOURCE_TAIL_EPOCH_STALE = "SOURCE_TAIL_EPOCH_STALE"

_MESSAGES = {
    SOURCE_TAIL_COVERAGE_INACTIVE: (
        "ADD holds no active source coverage for this terminal. ADD told the ESP to stop extending "
        "its stale certificate. A full reconcile certifies new coverage."
    ),
    SOURCE_TAIL_EPOCH_STALE: (
        "The ESP extended source coverage under a stale source epoch. ADD sent its current coverage "
        "and the ESP resumes from ADD's committed cursor."
    ),
}


@dataclass(frozen=True)
class TailRefusal:
    code: str
    ack: dict
    event: dict
    coverage: dict


def inactive_coverage_push(connector: Connector, *, terminal_serial: str) -> dict:
    """The no-certificate push every Zone Lite generation already gets on connect.

    It has the same shape as ``stream_bootstrap``'s placeholder. The serial is
    the one the reader named, so the push only withdraws its certificate and
    never reads as another terminal's coverage.
    """
    return {
        "type": "source_coverage",
        "terminal_serial": terminal_serial,
        "terminal_generation": max(1, connector.onboarding_generation or 0),
        "source_committed_cursor": 0,
        "source_committed_chain_digest": "0" * 64,
        "active": False,
    }


def active_coverage_push(session: Session, coverage: ReconciliationCoverage) -> dict:
    """ADD's active certificate as the push a reconnect or manifest handoff sends."""
    from zk_add.reconciliation import source_epoch_uuid

    return {
        "type": "source_coverage",
        "coverage_id": coverage.coverage_id,
        "source_epoch_id": coverage.source_epoch_id,
        "source_epoch": source_epoch_uuid(session, coverage),
        "terminal_serial": coverage.terminal_serial,
        "terminal_generation": coverage.terminal_generation,
        "certified_source_cursor": coverage.certified_source_cursor,
        "source_chain_digest": coverage.source_chain_digest,
        "source_committed_cursor": coverage.source_committed_cursor,
        "source_committed_chain_digest": coverage.source_committed_chain_digest,
        "active": True,
    }


def refuse_tail_without_authority(
    session: Session, *, connector: Connector, envelope: Envelope,
) -> TailRefusal | None:
    """Return a refusal only when the reader's certificate is stale; otherwise None.

    The caller holds the connector row lock, as ``apply_source_tail_chunk``
    does. This function only reads coverage and writes one device log row.
    """
    from zk_add.reconciliation import _require_source_epoch, source_epoch_uuid

    payload = SourceTailChunkRequest.model_validate(envelope.payload)
    zkt = connector.zkt_device
    if zkt is None:
        return None
    coverage = session.scalar(
        select(ReconciliationCoverage)
        .where(
            ReconciliationCoverage.zkt_device_id == zkt.id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
        .with_for_update().execution_options(populate_existing=True)
    )
    if coverage is None:
        code = SOURCE_TAIL_COVERAGE_INACTIVE
        push = inactive_coverage_push(connector, terminal_serial=payload.terminal_serial)
    elif (
        payload.terminal_serial == coverage.terminal_serial
        and payload.terminal_generation == coverage.terminal_generation
        and source_epoch_uuid(session, coverage) is not None
    ):
        try:
            _require_source_epoch(session, connector, coverage, payload.source_epoch)
        except EvidenceRejected:
            code = SOURCE_TAIL_EPOCH_STALE
            push = active_coverage_push(session, coverage)
        else:
            return None
    else:
        return None
    _record(session, connector=connector, envelope=envelope, payload=payload, code=code, coverage=coverage)
    return TailRefusal(
        code=code,
        ack={
            "type": "error",
            "message_id": envelope.message_id,
            "message_type": envelope.type,
            "code": code,
            "duplicate": False,
        },
        event={
            "connector_id": connector.connector_id,
            "coverage_id": coverage.coverage_id if coverage is not None else None,
            "source_committed_cursor": coverage.source_committed_cursor if coverage is not None else None,
            "error_code": code,
        },
        coverage=push,
    )


def _record(session, *, connector, envelope, payload, code, coverage) -> None:
    """One bounded device log row per refused message: codes and ordinals, never records."""
    from zk_add.service import ingest_logs

    ingest_logs(session, connector=connector, logs=[DeviceLogIn(
        boot_id=envelope.boot_id,
        sequence=envelope.seq,
        level="WARN",
        subsystem="add_backend",
        code=code,
        message=_MESSAGES[code],
        context={
            "message_type": envelope.type,
            "request_id": envelope.message_id,
            "start_ordinal": payload.start_ordinal,
            "end_ordinal": payload.end_ordinal,
            "terminal_generation": payload.terminal_generation,
            "coverage_id": coverage.coverage_id if coverage is not None else None,
            "coverage_generation": coverage.terminal_generation if coverage is not None else None,
            "source_committed_cursor": coverage.source_committed_cursor if coverage is not None else None,
        },
        device_time=envelope.sent_at,
    )])

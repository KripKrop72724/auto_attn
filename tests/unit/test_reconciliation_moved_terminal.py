"""A terminal's source history follows the terminal across connectors.

Live incident, 20 Oct 2026: the full-history baseline (job 5a916c3a) for
ZONE-BLD9-01 (connector 9d8c589f, K50 ANM4261301077, zone-lite-2.7.4) had its
first chunk refused as EVIDENCE_INVALID on every retry, as it was on 15 Oct on
2.5.2. From 18 Aug to 7 Sep 2026 the ZONE-SLICTOWER-3FL ESP read this K50, and
that connector still owns 1243 of its punches. Zone Lite derives an event UID
from the device time, terminal serial, punch and user ID only, so the BLD9-01
baseline replays UIDs another connector owns. ADD raised "Replayed attendance
conflicts with retained source ownership." for the whole chunk. The K50's
wrong-clock prefix (zero, year-2000, all-ones and impossible dates) was never
the cause: those records are preserved as INVALID_TIME source exceptions.

A replay links such a row only while the terminal has provably moved: the
previous connector no longer reports, expects or has confirmed its serial, and
the row carries this terminal's serial. A certified tail never links one,
because its ordinals were read after the move.

Source rows come from a port of zone-lite ``append_terminal_source_record``
(firmware/zone_lite/main/zone_lite.c and zkt_record.c) for every layout it
emits: 40-byte (G3, MB40-VL, K50), 16-byte and 8-byte records.
"""
from __future__ import annotations

import base64
import calendar
from datetime import datetime, timedelta
import hashlib
import json
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.attendance_identity_evidence import owner_source_manifest, source_evidence
from zk_add.attendance_recovery import _event_statement
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent,
    DeviceAlert,
    ReconciliationCoverage,
    ReconciliationEvent,
    SourceTailChunk,
    TerminalRecordManifest,
)
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    apply_reconciliation_manifest,
    apply_source_tail_chunk,
    assignment_rows,
    create_reconciliation_job,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
    source_epoch_uuid,
)
from zk_add.rejection import (
    EvidenceRejected,
    rejection_category,
    rejection_reason,
    rejection_site,
)
from zk_add.schemas import (
    AttendanceEventIn,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
    SourceTailChunkRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import ingest_attendance, onboard_connector, replace_user_snapshot
from zk_add.settings import settings
from zk_add.time_utils import utc_now

K50 = "ANM4261301077"
G3 = "PGB1261200074"
USERS = {"166": "1", "1": "2", "2": "3"}  # terminal user ID -> enrollment UID
CONFLICT_MESSAGE = "Replayed attendance conflicts with retained source ownership."
PREFIX = 45  # the K50's wrong-clock records at the start of its log


# --- Zone Lite source-record port ------------------------------------------

def _sha256(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _fingerprint(user_id: str) -> str:
    return _sha256(f"terminal-user-{user_id}")


def zk_time(year, month, day, hour=0, minute=0, second=0) -> int:
    """ZKT packed time; the day may exceed its month, as on a wrong clock."""
    value = year - 2000
    for multiplier, part in ((12, month - 1), (31, day - 1), (24, hour), (60, minute), (60, second)):
        value = value * multiplier + part
    return value


def _unpack(value: int) -> tuple[int, int, int, int, int, int]:  # decode_zk_time
    second, value = value % 60, value // 60
    minute, value = value % 60, value // 60
    hour, value = value % 24, value // 24
    day, value = value % 31 + 1, value // 31
    month, value = value % 12 + 1, value // 12
    return value + 2000, month, day, hour, minute, second


def _plausible(value: int) -> bool:  # zk_attendance_timestamp_is_plausible
    if value == 0:
        return False
    year, month, day, *_ = _unpack(value)
    return 2010 <= year <= 2099 and day <= calendar.monthrange(year, month)[1]


def _iso_utc(value: int) -> str:  # iso_from_zk_time: terminal time is PKT (UTC+5)
    return (datetime(*_unpack(value)) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def record40(user_id: str, encoded: int, *, record_uid: int, status=1, punch=0) -> bytes:
    """40-byte record; its first two bytes carry the terminal record number."""
    raw = bytearray(40)
    raw[0:2] = record_uid.to_bytes(2, "little")
    raw[2:2 + len(user_id)] = user_id.encode()
    raw[26], raw[31] = status, punch
    raw[27:31] = encoded.to_bytes(4, "little")
    return bytes(raw)


def record16(user_id: str, encoded: int, *, record_uid: int = 0, status=1, punch=0) -> bytes:
    raw = bytearray(16)
    raw[0:4] = int(user_id).to_bytes(4, "little")
    raw[4:8] = encoded.to_bytes(4, "little")
    raw[8], raw[9] = status, punch
    return bytes(raw)


def record8(user_id: str, encoded: int, *, record_uid: int, status=1, punch=0) -> bytes:
    """8-byte record: an attendance UID only, never an enrollment reference."""
    raw = bytearray(8)
    raw[0:2] = record_uid.to_bytes(2, "little")
    raw[2], raw[7] = status, punch
    raw[3:7] = encoded.to_bytes(4, "little")
    return bytes(raw)


LAYOUTS = {40: record40, 16: record16, 8: record8}


def _event_uid(serial: str, device_event_time: str, punch: int, user_id: str) -> str:  # build_event_uid
    return _sha256(
        '{"device_event_time":"%s","device_serial":"%s","punch":"%u","user_id":"%s"}'
        % (device_event_time, serial, punch, user_id)
    )


def source_row(raw: bytes, ordinal: int, *, serial: str, captured_at: str) -> tuple[dict, dict]:
    """append_terminal_source_record for a ZKT_LEGACY (not raw-only) reader."""
    digest = _sha256(raw)
    observed_uid = observed_user_id = user_id = ""
    record_uid = 0
    if len(raw) == 16:
        user_id = observed_user_id = str(int.from_bytes(raw[0:4], "little"))
        encoded, status, punch = int.from_bytes(raw[4:8], "little"), raw[8], raw[9]
        identity_missing = False
    else:
        record_uid = int.from_bytes(raw[0:2], "little")
        observed_uid = str(record_uid)
        if len(raw) == 8:
            encoded, status, punch = int.from_bytes(raw[3:7], "little"), raw[2], raw[7]
            identity_missing = True
        else:
            field = raw[2:26].split(b"\0", 1)[0]
            observed_user_id = field.decode("latin-1").strip(" ")
            if all(32 <= byte <= 126 for byte in field):
                user_id = field.decode().rstrip(" ")
            encoded, status, punch = int.from_bytes(raw[27:31], "little"), raw[26], raw[31]
            identity_missing = not field.strip(b" ")
    parsed = bool(user_id) and _plausible(encoded)
    disposition = (
        "EVENT" if parsed
        else "INVALID_TIME" if not _plausible(encoded)
        else "IDENTITY_UNRESOLVED" if identity_missing
        else "MALFORMED"
    )
    source = {
        "ordinal": ordinal,
        "raw_record_digest": digest,
        "terminal_record_key": _sha256(f"{serial}:{digest}"),
        "occurrence_index": ordinal + 1,
        "disposition": disposition,
        "raw_timestamp": encoded,
        **({"observed_uid": observed_uid} if observed_uid else {}),
        **({"observed_user_id": observed_user_id} if observed_user_id else {}),
    }
    event = None
    if parsed:
        device_event_time = _iso_utc(encoded)
        uid = USERS.get(user_id)  # the roster supplies current identity evidence only
        event = {
            "event_uid": _event_uid(serial, device_event_time, punch, user_id),
            "terminal_serial": serial,
            **({"uid": uid, "terminal_identity_fingerprint": _fingerprint(user_id)} if uid else {}),
            "user_id": user_id,
            "device_event_time": device_event_time,
            "captured_at": captured_at,
            "source": "FULL_HISTORY",
            "status": status,
            "punch": punch,
            "raw_punch": False,
            "clock_quality": "OK",
            "raw_event": {
                **({"terminal_uid": uid} if uid else {}),
                **({"attendance_record_uid": str(record_uid)} if record_uid else {}),
            },
        }
        source["event"] = event
    else:
        source["error_code"] = {
            "INVALID_TIME": "IMPLAUSIBLE_TERMINAL_TIME",
            "IDENTITY_UNRESOLVED": "HISTORICAL_IDENTITY_EVIDENCE_REQUIRED",
        }.get(disposition, "UNPARSEABLE_TERMINAL_RECORD")
    source["raw_record_b64"] = base64.b64encode(raw).decode()
    canonical = {
        "disposition": disposition,
        "event_uid": event["event_uid"] if event else None,
        "occurrence_index": ordinal + 1,
        "ordinal": ordinal,
        "raw_record_digest": digest,
        "terminal_record_key": source["terminal_record_key"],
    }
    return source, canonical


def terminal_log(layout: int = 40, count: int = 140) -> list[bytes]:
    """A wrong-clock prefix, then real punches from 11 Aug 2026 (PKT)."""
    build, user_ids, raws = LAYOUTS[layout], list(USERS), []
    for number in range(1, count + 1):
        if number <= PREFIX:
            encoded = (0, zk_time(2000, 1, 1 + number % 28, number % 24, number % 60),
                       0xFFFFFFFF, zk_time(2026, 2, 31, 9, number % 60))[number % 4]
        else:
            encoded = zk_time(2026, 8, 11 + (number - PREFIX - 1) // 10, 8 + number % 9,
                              number % 60, number % 60)
        raws.append(build(user_ids[number % 3], encoded, record_uid=number, punch=number % 2))
    return raws


# --- ADD fixtures -----------------------------------------------------------

def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "reconciliation_self_healing_enabled", True)


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, monkeypatch):
    """The same evidence on SQLite and, when configured, a disposable PostgreSQL schema."""
    _settings(monkeypatch)
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
        engine.dispose()
        return
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "moved_terminal_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        # Independent DDL metadata, as in test_safe_attendance_repair_postgres.py.
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        with Session(engine) as session:
            yield session
            session.rollback()
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _connector(session, *, hardware_id: str, zone: str, serial: str, count: int = 140):
    connector, _token, _created = onboard_connector(
        session, hardware_id=hardware_id, zone_id=zone, zone_name=zone, device_id="1",
        firmware_version="zone-lite-2.7.4", expected_serial=serial, actor="test",
        ip_address="127.0.0.1",
    )
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    zkt.serial = zkt.confirmed_serial = serial
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = zkt.identity_snapshot_stable = True
    zkt.attendance_count = count
    zkt.capability_profile = {
        "read_attendance": True, "history_stream_v1": True, "history_stream_v2": True,
        "history_range_resume_verified": True, "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    # The K50 roster: none of its users has a CNIC in ADD.
    users = [UserSnapshotRow(uid=uid, user_id=user_id, name=f"Employee {user_id}",
                             terminal_identity_fingerprint=_fingerprint(user_id))
             for user_id, uid in USERS.items()]
    for snapshot in ("initial", "stable"):
        replace_user_snapshot(session, connector=connector, snapshot=UserSnapshotRequest(
            snapshot_id=f"{zone}-{snapshot}", complete=True, observed_at=utc_now(), users=users))
    zkt.identity_snapshot_stable = True
    session.flush()
    return connector


def _bld9(session):
    return _connector(session, hardware_id="ac:27:6e:a3:33:28", zone="ZONE-BLD9-01", serial=K50)


def _three_fl(session):
    return _connector(session, hardware_id="a4:cb:8f:d4:66:64", zone="ZONE-SLICTOWER-3FL",
                      serial=K50)


def first_chunk(session, connector, raws, *, serial: str = K50):
    """Anchor a baseline and build its first v2 chunk exactly as Zone Lite sends it."""
    job = create_reconciliation_job(
        session, connector=connector, actor="operator", confirmation="RECONCILE 1 FROM START",
        reason="Full-history baseline from the terminal's first record.",
        idempotency_key=f"baseline-{connector.connector_id}",
    )
    epoch = source_epoch_uuid(session, job)
    apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        source_epoch=epoch, job_id=job.job_id, generation=job.terminal_generation,
        terminal_serial=serial, terminal_generation=job.terminal_generation,
        cutoff_count=len(raws), latest_terminal_count=len(raws), record_size=len(raws[0]),
        source_total_bytes=4 + len(raws[0]) * len(raws), first_anchor_digest=_sha256(raws[0]),
    ))
    assignment = next(payload for _id, payload in assignment_rows(session)
                      if payload["job_id"] == job.job_id)
    assert assignment["protocol"] == "history_stream_v2"
    end = min(len(raws), assignment["chunk_records"])
    captured_at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [source_row(raw, ordinal, serial=serial, captured_at=captured_at)
            for ordinal, raw in enumerate(raws[:end])]
    chunk_digest = _sha256(json.dumps([canonical for _source, canonical in rows],
                                      separators=(",", ":")))
    wire = json.dumps({
        "job_id": job.job_id, "assignment_id": assignment["assignment_id"],
        "generation": job.terminal_generation, "sequence": 0, "start_ordinal": 0,
        "end_ordinal": end, "chunk_digest": chunk_digest, "previous_chain_digest": None,
        "resulting_chain_digest": _sha256(f"{'0' * 64}:0:{end}:{chunk_digest}"),
        "records": [source for source, _canonical in rows], "source_epoch": epoch,
    }, separators=(",", ":"))
    return job, ReconciliationChunkRequest.model_validate(json.loads(wire))


def captured_copies(raws, *, sources=("LIVE", "CURRENT_RECONCILE", "DUMP_STARTUP"),
                    start: int = 0, end: int = 100):
    """Punches (the first chunk's by default) as an ESP delivered them while it read this terminal."""
    rows = [source_row(raw, ordinal, serial=K50, captured_at="2026-08-21T09:00:00Z")[0]
            for ordinal, raw in enumerate(raws[start:end], start)]
    copies = []
    for index, event in enumerate(row["event"] for row in rows if "event" in row):
        copies.append(AttendanceEventIn.model_validate({
            **event, "source": sources[index % len(sources)],
            "raw_event": {"terminal_uid": event.get("uid")},
        }))
    return copies


def three_fl_owned_the_terminal(session, raws):
    """18 Aug - 7 Sep 2026: 3FL's ESP read the K50, then got its own G3 back."""
    three_fl = _three_fl(session)
    accepted, _duplicates = ingest_attendance(session, connector=three_fl,
                                              events=captured_copies(raws))
    assert len(accepted) == 100 - PREFIX
    zkt = three_fl.zkt_device
    zkt.serial = zkt.confirmed_serial = zkt.expected_serial = G3
    three_fl.connected = False
    session.flush()
    return three_fl


def certify(session, connector, raws):
    """Commit a one-chunk baseline over ``raws`` and seal its capture certificate."""
    job, chunk = first_chunk(session, connector, raws)
    job, committed, _duplicate = apply_reconciliation_chunk(session, connector=connector, payload=chunk)
    assert committed is not None and job.committed_next_ordinal == len(raws)
    apply_reconciliation_manifest(session, connector=connector, payload=ReconciliationManifestRequest(
        source_epoch=chunk.source_epoch, job_id=job.job_id, generation=job.terminal_generation,
        terminal_serial=K50, terminal_generation=job.terminal_generation, cutoff_count=len(raws),
        latest_terminal_count=len(raws), final_chain_digest=chunk.resulting_chain_digest,
    ))
    coverage = session.scalar(select(ReconciliationCoverage).where(
        ReconciliationCoverage.zkt_device_id == connector.zkt_device.id,
        ReconciliationCoverage.active == True,  # noqa: E712
    ))
    assert coverage is not None and coverage.source_committed_cursor == len(raws)
    return job, coverage


def tail_chunk(session, coverage, raws, *, start: int, end: int) -> SourceTailChunkRequest:
    """The source_tail_chunk Zone Lite sends for new ordinals after a certified cutoff."""
    captured_at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    records = [source_row(raw, ordinal, serial=K50, captured_at=captured_at)[0]
               for ordinal, raw in enumerate(raws[start:end], start)]
    for record in records:
        record["event"]["source"] = "CURRENT_RECONCILE"
    draft = SourceTailChunkRequest.model_validate({
        "source_epoch": source_epoch_uuid(session, coverage), "terminal_serial": K50,
        "terminal_generation": coverage.terminal_generation, "record_size": len(raws[0]),
        "start_ordinal": start, "end_ordinal": end, "latest_terminal_count": end,
        "chunk_digest": "0" * 64, "previous_chain_digest": coverage.source_committed_chain_digest,
        "resulting_chain_digest": "0" * 64, "records": records,
    })
    chunk_digest = reconciliation_chunk_digest(draft)
    return draft.model_copy(update={
        "chunk_digest": chunk_digest,
        "resulting_chain_digest": reconciliation_chain_digest(
            coverage.source_committed_chain_digest, start_ordinal=start, end_ordinal=end,
            chunk_digest=chunk_digest,
        ),
    })


def _scoped(session, **filters) -> set[int]:
    """Attendance an operator's recovery filter selects."""
    return {event.id for event, _outbox, _connector in session.execute(
        _event_statement(session, filters)).all()}


def _columns(row) -> dict:
    return {column.key: getattr(row, column.key) for column in AttendanceEvent.__table__.columns}


def _manifests(session, connector):
    return session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.connector_id == connector.id,
    ).order_by(TerminalRecordManifest.ordinal)).all()


def _job_events(session, job, state):
    return session.scalars(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == state,
    ).order_by(ReconciliationEvent.id)).all()


# --- K50 incident -----------------------------------------------------------

def test_k50_wrong_clock_prefix_is_quarantined_not_rejected(db) -> None:
    bld9 = _bld9(db)
    job, chunk = first_chunk(db, bld9, terminal_log())
    assert [row.disposition for row in chunk.records[:PREFIX]] == ["INVALID_TIME"] * PREFIX

    job, committed, duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    assert committed is not None and not duplicate
    assert (job.committed_next_ordinal, job.quarantined_count) == (100, PREFIX)
    assert (committed.accepted_count, committed.already_present_count) == (100 - PREFIX, 0)
    manifests = _manifests(db, bld9)
    assert [row.ordinal for row in manifests] == list(range(100))
    assert {(row.disposition, row.error_code, row.attendance_event_id)
            for row in manifests[:PREFIX]} == {("INVALID_TIME", "IMPLAUSIBLE_TERMINAL_TIME", None)}
    assert all(row.attendance_event_id for row in manifests[PREFIX:])


def test_k50_first_chunk_commits_when_its_previous_connector_owns_the_punches(db) -> None:
    """The live incident. Before the fix this raised CONFLICT_MESSAGE on every retry."""
    raws = terminal_log()
    three_fl = three_fl_owned_the_terminal(db, raws)
    owned = db.scalars(select(AttendanceEvent).where(
        AttendanceEvent.connector_id == three_fl.id).order_by(AttendanceEvent.id)).all()
    assert len(owned) == 100 - PREFIX and {row.device_serial for row in owned} == {K50}
    db.expire_all()
    before = {row.id: _columns(row) for row in owned}
    event_count = db.scalar(select(func.count(AttendanceEvent.id)))
    bld9 = _bld9(db)
    job, chunk = first_chunk(db, bld9, raws)

    job, committed, duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    # Ordinals, raw digests and the chain were verified and committed as usual.
    assert committed is not None and not duplicate
    assert job.committed_next_ordinal == 100
    assert job.last_chain_digest == chunk.resulting_chain_digest
    assert (committed.accepted_count, committed.already_present_count) == (0, 100 - PREFIX)
    assert job.quarantined_count == PREFIX
    # Every source record has a manifest; none is dropped.
    manifests = _manifests(db, bld9)
    assert [row.raw_record_digest for row in manifests] == [
        row.raw_record_digest for row in chunk.records]
    assert {row.disposition for row in manifests[:PREFIX]} == {"INVALID_TIME"}
    # Each replayed punch links to the row 3FL still owns; nothing new is minted.
    by_uid = {row.event_uid: row.id for row in owned}
    assert [row.attendance_event_id for row in manifests[PREFIX:]] == [
        by_uid[row.event.event_uid] for row in chunk.records[PREFIX:]]
    assert db.scalar(select(func.count(AttendanceEvent.id))) == event_count
    # 3FL's rows are not rewritten or released through BLD9-01's roster.
    db.flush()
    db.expire_all()
    assert {row.id: _columns(db.get(AttendanceEvent, row.id)) for row in owned} == before
    retained = _job_events(db, job, "SOURCE_REPLAY_RETAINED_ELSEWHERE")
    assert [event.details for event in retained] == [{
        "start_ordinal": 0, "end_ordinal": 100, "retained_elsewhere": 100 - PREFIX,
        "owner_count": 1, "owner_connector_ids": [three_fl.connector_id],
    }]
    committed_event = _job_events(db, job, "CHUNK_COMMITTED")[-1].details
    assert committed_event["retained_elsewhere"] == 100 - PREFIX
    # A recovery scoped to BLD9-01's source epoch does not reach 3FL's rows.
    assert _scoped(db, connector_id=three_fl.connector_id) == set(before)
    assert _scoped(db, source_epoch=source_epoch_uuid(db, job)) == set()


def test_previous_owner_keeps_its_own_source_evidence(db) -> None:
    """3FL also ran a baseline while it read the K50; its proof must survive the move."""
    raws = terminal_log()
    three_fl = _three_fl(db)
    _job, chunk = first_chunk(db, three_fl, raws)
    apply_reconciliation_chunk(db, connector=three_fl, payload=chunk)
    row = db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.event_uid == chunk.records[PREFIX].event.event_uid))
    proof = source_evidence(db, row, three_fl)
    assert proof["kind"] == "SAVED_TERMINAL_RECORD"
    owner_epoch = source_epoch_uuid(db, _job)
    assert row.id in _scoped(db, source_epoch=owner_epoch)
    zkt = three_fl.zkt_device
    zkt.serial = zkt.confirmed_serial = zkt.expected_serial = G3
    three_fl.connected = False
    db.flush()
    bld9 = _bld9(db)
    job, replay = first_chunk(db, bld9, raws)

    _job, committed, _duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=replay)

    assert committed.already_present_count == 100 - PREFIX
    linked = db.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.attendance_event_id == row.id)).all()
    assert {manifest.connector_id for manifest in linked} == {three_fl.id, bld9.id}
    # BLD9-01's later replay neither replaces nor revokes 3FL's own record.
    assert owner_source_manifest(db, row).connector_id == three_fl.id
    assert source_evidence(db, row, three_fl) == proof
    assert row.id in _scoped(db, source_epoch=owner_epoch)
    assert row.id not in _scoped(db, source_epoch=source_epoch_uuid(db, job))


@pytest.mark.parametrize(("owner", "fact", "reason"), [
    ("previous", "user_id", "SOURCE_REPLAY_USER_MISMATCH"),
    ("previous", "device_event_time", "SOURCE_REPLAY_TIME_MISMATCH"),
    ("previous", "device_serial", "SOURCE_REPLAY_TERMINAL_MISMATCH"),
    ("previous", "device_serial_missing", "SOURCE_REPLAY_TERMINAL_UNPROVEN"),
    ("previous", "punch", "SOURCE_REPLAY_PUNCH_MISMATCH"),
    ("same", "user_id", "SOURCE_REPLAY_USER_MISMATCH"),
    ("same", "device_serial", "SOURCE_REPLAY_TERMINAL_MISMATCH"),
])
def test_replay_contradicting_a_retained_row_still_fails_closed(db, owner, fact, reason) -> None:
    raws = terminal_log()
    if owner == "previous":
        three_fl_owned_the_terminal(db, raws)
        bld9 = _bld9(db)
        _job, chunk = first_chunk(db, bld9, raws)
    else:
        bld9 = _bld9(db)
        _job, chunk = first_chunk(db, bld9, raws)
        ingest_attendance(db, connector=bld9, events=captured_copies(raws, sources=("LIVE",)))
    row = db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.event_uid == chunk.records[PREFIX + 3].event.event_uid))
    if fact == "user_id":
        row.user_id = "999"
    elif fact == "device_event_time":
        row.device_event_time = row.device_event_time + timedelta(seconds=1)
    elif fact == "device_serial":
        row.device_serial = G3
    elif fact == "device_serial_missing":
        # ADD never recomputes the event UID, so a serial-less row owned
        # elsewhere proves nothing about which terminal it came from.
        row.device_serial = None
    else:
        row.punch = "1" if row.punch == "0" else "0"
    db.flush()

    with pytest.raises(EvidenceRejected, match=f"^{CONFLICT_MESSAGE[:-1]}[.]$") as caught:
        apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    assert caught.value.reason_code == reason == rejection_reason(caught.value)
    assert rejection_category(caught.value) == "EVIDENCE_INVALID"
    assert rejection_site(caught.value).startswith(
        "zk_add.reconciliation:_bound_source_attendance:")
    assert _manifests(db, bld9) == []


@pytest.mark.parametrize("field", ["serial", "confirmed_serial", "expected_serial"])
def test_replay_is_refused_while_its_owner_still_claims_the_terminal(db, field) -> None:
    """Two ESPs reading one terminal is not a move; the old fail-closed refusal stands."""
    raws = terminal_log()
    three_fl = three_fl_owned_the_terminal(db, raws)
    setattr(three_fl.zkt_device, field, K50)
    db.flush()
    bld9 = _bld9(db)
    _job, chunk = first_chunk(db, bld9, raws)

    with pytest.raises(EvidenceRejected, match=f"^{CONFLICT_MESSAGE[:-1]}[.]$") as caught:
        apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    assert rejection_reason(caught.value) == "SOURCE_REPLAY_OWNER_STILL_BOUND"
    assert rejection_site(caught.value).startswith(
        "zk_add.reconciliation:_bound_source_attendance:")
    assert _manifests(db, bld9) == []


@pytest.mark.parametrize("owner_moved", [False, True])
def test_tail_replaying_punches_another_connector_owns_invalidates_coverage(db, owner_moved) -> None:
    """A tail is read after the move, so another owner can only be a second reader."""
    raws = terminal_log()
    bld9 = _bld9(db)
    job, coverage = certify(db, bld9, raws[:100])
    three_fl = _three_fl(db)
    accepted, _duplicates = ingest_attendance(db, connector=three_fl, events=captured_copies(
        raws, sources=("LIVE",), start=110, end=120))
    assert len(accepted) == 10
    if owner_moved:
        zkt = three_fl.zkt_device
        zkt.serial = zkt.confirmed_serial = zkt.expected_serial = G3
    db.flush()
    db.expire_all()
    owned = db.scalars(select(AttendanceEvent).where(
        AttendanceEvent.connector_id == three_fl.id)).all()
    before = {row.id: _columns(row) for row in owned}
    event_count = db.scalar(select(func.count(AttendanceEvent.id)))
    manifest_count = db.scalar(select(func.count(TerminalRecordManifest.id)))
    tail = tail_chunk(db, coverage, raws, start=100, end=140)

    held, chunk, duplicate, error_code = apply_source_tail_chunk(db, connector=bld9, payload=tail)

    assert (chunk, duplicate, error_code) == (None, False, "SOURCE_TAIL_FOREIGN_OWNER")
    assert (held.active, held.capture_state, held.invalidated_reason) == (
        False, "SOURCE_COVERAGE_INVALIDATED", "SOURCE_TAIL_FOREIGN_OWNER")
    assert held.source_committed_cursor == 100
    assert (job.status, job.error_code) == ("INVALIDATED", "SOURCE_TAIL_FOREIGN_OWNER")
    alert = db.scalar(select(DeviceAlert).where(
        DeviceAlert.connector_id == bld9.id, DeviceAlert.code == "ADD_SOURCE_COVERAGE_INVALIDATED"))
    assert alert.details == {"failure_category": "SOURCE_TAIL_FOREIGN_OWNER"}
    # Nothing from the tail is linked, ingested or rewritten.
    assert db.scalar(select(func.count(SourceTailChunk.id))) == 0
    assert db.scalar(select(func.count(TerminalRecordManifest.id))) == manifest_count
    assert db.scalar(select(func.count(AttendanceEvent.id))) == event_count
    db.flush()
    db.expire_all()
    assert {row.id: _columns(db.get(AttendanceEvent, row.id)) for row in owned} == before


def test_tail_of_a_moved_terminal_commits_its_own_punches(db) -> None:
    raws = terminal_log()
    three_fl_owned_the_terminal(db, raws)
    bld9 = _bld9(db)
    _job, coverage = certify(db, bld9, raws[:100])
    tail = tail_chunk(db, coverage, raws, start=100, end=140)

    coverage, chunk, duplicate, error_code = apply_source_tail_chunk(db, connector=bld9, payload=tail)

    assert (error_code, duplicate, coverage.active) == (None, False, True)
    assert coverage.source_committed_cursor == 140
    assert (chunk.event_count, chunk.exception_count) == (40, 0)
    tail_events = db.scalars(select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(
        [row.event.event_uid for row in tail.records]))).all()
    assert {row.connector_id for row in tail_events} == {bld9.id}


# --- Other record layouts: regression -------------------------------------

@pytest.mark.parametrize("layout", [40, 16, 8])
def test_first_chunk_of_each_record_layout_commits(db, layout) -> None:
    bld9 = _bld9(db)
    job, chunk = first_chunk(db, bld9, terminal_log(layout))

    job, committed, _duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    manifests = _manifests(db, bld9)
    assert job.committed_next_ordinal == len(manifests) == 100
    assert {row.disposition for row in manifests[:PREFIX]} == {"INVALID_TIME"}
    if layout == 8:
        # An attendance UID is not an enrollment reference: preserved, never an event.
        assert {(row.disposition, row.attendance_event_id) for row in manifests[PREFIX:]} == {
            ("IDENTITY_UNRESOLVED", None)}
        assert (committed.accepted_count, job.quarantined_count) == (0, 100)
    else:
        assert all(row.attendance_event_id for row in manifests[PREFIX:])
        assert (committed.accepted_count, job.quarantined_count) == (100 - PREFIX, PREFIX)
    assert not _job_events(db, job, "SOURCE_REPLAY_RETAINED_ELSEWHERE")


@pytest.mark.parametrize("layout", [40, 16])
def test_same_connector_replay_keeps_its_verified_provenance_upgrade(db, layout) -> None:
    raws = terminal_log(layout)
    bld9 = _bld9(db)
    job, chunk = first_chunk(db, bld9, raws)
    ingest_attendance(db, connector=bld9, events=captured_copies(raws, sources=("LIVE",)))
    legacy = db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.event_uid == chunk.records[PREFIX].event.event_uid))
    legacy.device_serial = None  # captured by firmware that did not report a serial
    db.flush()

    job, committed, _duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    assert (committed.accepted_count, committed.already_present_count) == (0, 100 - PREFIX)
    assert legacy.device_serial == K50
    assert legacy.raw_event["terminal_provenance"] == "VERIFIED_SOURCE_REPLAY"
    assert not _job_events(db, job, "SOURCE_REPLAY_RETAINED_ELSEWHERE")


@pytest.mark.parametrize("layout", [40, 16])
def test_moved_terminal_replay_of_each_layout_commits(db, layout) -> None:
    raws = terminal_log(layout)
    three_fl = three_fl_owned_the_terminal(db, raws)
    bld9 = _bld9(db)
    job, chunk = first_chunk(db, bld9, raws)

    job, committed, _duplicate = apply_reconciliation_chunk(db, connector=bld9, payload=chunk)

    assert job.committed_next_ordinal == 100
    assert committed.already_present_count == 100 - PREFIX
    owners = {db.get(AttendanceEvent, row.attendance_event_id).connector_id
              for row in _manifests(db, bld9)[PREFIX:]}
    assert owners == {three_fl.id}

"""Reproduce the ZONE-BLD9-01 K50 first-chunk rejection (20 Oct 2026).

The K50 (serial ANM4261301077) was read by the ZONE-SLICTOWER-3FL ESP from
18 Aug to 7 Sep 2026, so ADD holds its oldest punches under that connector.
zone-lite-2.7.4 derives a FULL_HISTORY event_uid from (device_event_time,
terminal serial, punch, user_id) only, exactly as the live/dump path did on
3FL. The first chunk of a BLD9-01 baseline therefore replays event UIDs that
another connector owns, and ``_bound_source_attendance`` raises a plain
ValueError, which the transport reports as EVIDENCE_INVALID.

The rows below are built with a byte-for-byte port of the 2.7.4 builder
(``append_terminal_source_record`` in firmware/zone_lite/main/zone_lite.c at
87e14fe). Zero, year-2000 and impossible-date records are included to show
that the K50's implausible timestamps are accepted as INVALID_TIME evidence.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.db import Base
from zk_add.models import AttendanceEvent, TerminalRecordManifest
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    assignment_rows,
    create_reconciliation_job,
    source_epoch_uuid,
)
from zk_add.rejection import rejection_category
from zk_add.schemas import (
    AttendanceEventIn,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import ingest_attendance, onboard_connector, replace_user_snapshot
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_decode import PAKISTAN_TIME

K50_SERIAL = "ANM4261301077"
THREE_FL_TERMINAL = "PGB1261200074"
USERS = {"166": "1", "1": "2", "2": "3"}  # K50 user_id -> enrollment UID


def _fingerprint(user_id: str) -> str:
    return hashlib.sha256(f"k50-{user_id}".encode()).hexdigest()


def _encode(year, month, day, hour=0, minute=0, second=0) -> int:
    """ZKT packed time; day may exceed the month, as on a corrupt terminal."""
    value = year - 2000
    for multiplier, component in ((12, month - 1), (31, day - 1), (24, hour), (60, minute), (60, second)):
        value = value * multiplier + component
    return value


def _decode(value: int) -> tuple[int, int, int, int, int, int]:
    second, value = value % 60, value // 60
    minute, value = value % 60, value // 60
    hour, value = value % 24, value // 24
    day, value = value % 31 + 1, value // 31
    month, value = value % 12 + 1, value // 12
    return value + 2000, month, day, hour, minute, second


def _plausible(value: int) -> bool:  # zk_attendance_timestamp_is_plausible
    import calendar

    if value == 0:
        return False
    year, month, day, *_ = _decode(value)
    return 2010 <= year <= 2099 and day <= calendar.monthrange(year, month)[1]


def _iso_utc(value: int) -> str:  # iso_from_zk_time for a plausible time
    local = datetime(*_decode(value), tzinfo=PAKISTAN_TIME)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(*, record_uid: int, user_id: bytes, encoded: int, status=1, punch=0) -> bytes:
    raw = bytearray(40)
    raw[0:2] = record_uid.to_bytes(2, "little")
    raw[2:2 + len(user_id)] = user_id
    raw[26] = status
    raw[27:31] = encoded.to_bytes(4, "little")
    raw[31] = punch
    return bytes(raw)


def _event_uid(device_event_time: str, user_id: str, punch: int) -> str:  # build_event_uid
    material = (
        '{"device_event_time":"%s","device_serial":"%s","punch":"%u","user_id":"%s"}'
        % (device_event_time, K50_SERIAL, punch, user_id)
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _firmware_rows(raw: bytes, ordinal: int, captured_at: str) -> tuple[dict, dict]:
    """append_terminal_source_record for a 40-byte ZKT_LEGACY (non-raw) row."""
    digest = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(f"{K50_SERIAL}:{digest}".encode()).hexdigest()
    encoded = int.from_bytes(raw[27:31], "little")
    user_id = raw[2:26].split(b"\0", 1)[0].decode("ascii").strip()
    event = None
    if user_id and _plausible(encoded):
        device_event_time = _iso_utc(encoded)
        uid = USERS.get(user_id)
        event = {
            "event_uid": _event_uid(device_event_time, user_id, raw[31]),
            "terminal_serial": K50_SERIAL,
            **({"uid": uid, "terminal_identity_fingerprint": _fingerprint(user_id)} if uid else {}),
            "user_id": user_id,
            "device_event_time": device_event_time,
            "captured_at": captured_at,
            "source": "FULL_HISTORY",
            "status": raw[26],
            "punch": raw[31],
            "raw_punch": False,
            "clock_quality": "OK",
            "raw_event": {
                **({"terminal_uid": uid} if uid else {}),
                "attendance_record_uid": str(int.from_bytes(raw[0:2], "little")),
            },
        }
    disposition = "EVENT" if event else "INVALID_TIME" if not _plausible(encoded) else "MALFORMED"
    source = {
        "ordinal": ordinal,
        "raw_record_digest": digest,
        "terminal_record_key": key,
        "occurrence_index": ordinal + 1,
        "disposition": disposition,
        "raw_timestamp": encoded,
        "observed_uid": str(int.from_bytes(raw[0:2], "little")),
        **({"observed_user_id": user_id} if user_id else {}),
        **({"event": event} if event else {"error_code": "IMPLAUSIBLE_TERMINAL_TIME"}),
        "raw_record_b64": base64.b64encode(raw).decode(),
    }
    canonical = {
        "disposition": disposition,
        "event_uid": event["event_uid"] if event else None,
        "occurrence_index": ordinal + 1,
        "ordinal": ordinal,
        "raw_record_digest": digest,
        "terminal_record_key": key,
    }
    return source, canonical


def _firmware_chunk(*, job_id, assignment_id, generation, source_epoch, raws) -> dict:
    captured = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [_firmware_rows(raw, ordinal, captured) for ordinal, raw in enumerate(raws)]
    chunk_digest = hashlib.sha256(
        json.dumps([canonical for _source, canonical in rows], separators=(",", ":")).encode()
    ).hexdigest()
    resulting = hashlib.sha256(f"{'0' * 64}:0:{len(raws)}:{chunk_digest}".encode()).hexdigest()
    return {
        "job_id": job_id, "assignment_id": assignment_id, "generation": generation,
        "sequence": 0, "start_ordinal": 0, "end_ordinal": len(raws), "chunk_digest": chunk_digest,
        "previous_chain_digest": None, "resulting_chain_digest": resulting,
        "records": [source for source, _canonical in rows], "source_epoch": source_epoch,
    }


# The K50's first log rows: zero, year-2000 and impossible (31 Feb) times,
# then its oldest real punch (11 Aug 2026 12:07:19 PKT, user 166).
INVALID_TIME_RAWS = [
    _record(record_uid=1, user_id=b"1", encoded=0),
    _record(record_uid=2, user_id=b"2", encoded=_encode(2000, 1, 1, 8, 30)),
    _record(record_uid=3, user_id=b"166", encoded=_encode(2009, 2, 31, 9)),
]
FIRST_REAL_PUNCH = _record(record_uid=46, user_id=b"166", encoded=_encode(2026, 8, 11, 12, 7, 19))


def _connector(session, *, hardware_id, zone, serial):
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
    zkt.capability_profile = {
        "read_attendance": True, "history_stream_v1": True, "history_stream_v2": True,
        "history_range_resume_verified": True, "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    users = [UserSnapshotRow(uid=uid, user_id=user_id, name=f"Employee {user_id}",
                             terminal_identity_fingerprint=_fingerprint(user_id))
             for user_id, uid in USERS.items()]
    for snapshot_id in ("initial", "stable"):
        replace_user_snapshot(session, connector=connector, snapshot=UserSnapshotRequest(
            snapshot_id=f"{zone}-{snapshot_id}", complete=True, observed_at=utc_now(), users=users))
    zkt.identity_snapshot_stable = True
    session.flush()
    return connector


@pytest.fixture()
def k50_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "reconciliation_self_healing_enabled", True)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _three_fl_owned_the_k50(session) -> None:
    """3FL's ESP read the K50 and ingested its oldest punch (DUMP_STARTUP)."""
    three_fl = _connector(session, hardware_id="aa:bb:cc:00:03:0f", zone="ZONE-SLICTOWER-3FL",
                          serial=K50_SERIAL)
    device_event_time = _iso_utc(int.from_bytes(FIRST_REAL_PUNCH[27:31], "little"))
    ingest_attendance(session, connector=three_fl, events=[AttendanceEventIn(
        event_uid=_event_uid(device_event_time, "166", 0), terminal_serial=K50_SERIAL, uid="1",
        terminal_identity_fingerprint=_fingerprint("166"), user_id="166",
        device_event_time=device_event_time, captured_at=datetime(2026, 8, 18, 12, 12, 33, tzinfo=timezone.utc),
        source="DUMP_STARTUP", status=1, punch=0, clock_quality="OK",
    )])
    # 3FL later got its own terminal back; the K50 moved to BLD9-01.
    zkt = three_fl.zkt_device
    zkt.serial = zkt.confirmed_serial = zkt.expected_serial = THREE_FL_TERMINAL
    session.flush()


def _bld9_first_chunk(session, raws):
    bld9 = _connector(session, hardware_id="ac:27:6e:a3:33:28", zone="ZONE-BLD9-01", serial=K50_SERIAL)
    bld9.zkt_device.attendance_count = len(raws)
    job = create_reconciliation_job(
        session, connector=bld9, actor="operator", confirmation="RECONCILE 1 FROM START",
        reason="K50 full-history baseline for the 2.7.4 OTA gate.", idempotency_key="k50-baseline-0001",
    )
    epoch = source_epoch_uuid(session, job)
    apply_reconciliation_anchor(session, connector=bld9, payload=ReconciliationAnchorRequest(
        source_epoch=epoch, job_id=job.job_id, generation=job.terminal_generation,
        terminal_serial=K50_SERIAL, terminal_generation=job.terminal_generation,
        cutoff_count=len(raws), latest_terminal_count=len(raws), record_size=40,
        source_total_bytes=4 + 40 * len(raws), first_anchor_digest=hashlib.sha256(raws[0]).hexdigest(),
    ))
    assignment = next(payload for _id, payload in assignment_rows(session) if payload["job_id"] == job.job_id)
    wire = json.loads(json.dumps(_firmware_chunk(
        job_id=job.job_id, assignment_id=assignment["assignment_id"], generation=job.terminal_generation,
        source_epoch=epoch, raws=raws,
    )))
    return bld9, ReconciliationChunkRequest.model_validate(wire)


def test_k50_invalid_timestamps_alone_commit_as_preserved_exceptions(k50_db) -> None:
    session = k50_db
    raws = [*INVALID_TIME_RAWS, FIRST_REAL_PUNCH]
    bld9, chunk = _bld9_first_chunk(session, raws)

    job, committed, duplicate = apply_reconciliation_chunk(session, connector=bld9, payload=chunk)

    assert committed is not None and not duplicate
    assert job.committed_next_ordinal == len(raws)
    dispositions = session.scalars(
        select(TerminalRecordManifest.disposition).order_by(TerminalRecordManifest.ordinal)
    ).all()
    assert dispositions == ["INVALID_TIME", "INVALID_TIME", "INVALID_TIME", "BLOCKED_IDENTITY"]


def test_k50_first_chunk_is_rejected_for_punches_owned_by_its_previous_connector(k50_db) -> None:
    session = k50_db
    _three_fl_owned_the_k50(session)
    raws = [*INVALID_TIME_RAWS, FIRST_REAL_PUNCH]
    bld9, chunk = _bld9_first_chunk(session, raws)
    replayed = chunk.records[3].event
    owner = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == replayed.event_uid))
    assert owner is not None and owner.connector_id != bld9.id

    with pytest.raises(ValueError, match="^Replayed attendance conflicts with retained source ownership[.]$") as caught:
        apply_reconciliation_chunk(session, connector=bld9, payload=chunk)

    assert type(caught.value) is ValueError
    assert rejection_category(caught.value) == "EVIDENCE_INVALID"

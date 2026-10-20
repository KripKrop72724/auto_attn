"""K50 first-chunk diagnosis: firmware-shaped source rows through ADD.

The rows are built exactly as zone-lite 2.7.4 append_terminal_source_record
builds them (87e14fe zone_lite.c ~4296-4440): 40-byte ZK records, the
attendance UID field carrying the terminal record number, zero and year-2000
timestamps at the start of the log, and a roster without any CNIC identity.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.db import Base
from zk_add.models import AttendanceEvent
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    assignment_rows,
    create_reconciliation_job,
)
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


SERIAL = "ANM4261301077"
USERS = [(str(uid), str(1000 + uid), f"Employee {uid}") for uid in range(1, 9)]
MIN_YEAR = 2010


def _fingerprint(uid: str) -> str:
    return hashlib.sha256(f"k50-user-{uid}".encode()).hexdigest()


def _encode(year, month, day, hour, minute, second) -> int:
    value = year - 2000
    for multiplier, component in ((12, month - 1), (31, day - 1), (24, hour), (60, minute), (60, second)):
        value = value * multiplier + component
    return value


def _decode(value: int):
    second = value % 60
    value //= 60
    minute = value % 60
    value //= 60
    hour = value % 24
    value //= 24
    day = value % 31 + 1
    value //= 31
    month = value % 12 + 1
    value //= 12
    return value + 2000, month, day, hour, minute, second


def _days_in_month(year, month):
    if month == 2:
        return 29 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 28
    return 30 if month in {4, 6, 9, 11} else 31


def _plausible(value: int) -> bool:
    if value == 0:
        return False
    year, month, day, hour, minute, second = _decode(value)
    return MIN_YEAR <= year <= 2099 and 1 <= day <= _days_in_month(year, month)


def _iso(value: int) -> str:
    year, month, day, hour, minute, second = _decode(value)
    utc = datetime(year, month, day, hour, minute, second) - timedelta(hours=5)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(number: int, user_id: str, encoded: int, *, status=0, punch=0) -> bytes:
    raw = bytearray(40)
    raw[0:2] = number.to_bytes(2, "little")
    raw[2:2 + len(user_id)] = user_id.encode()
    raw[26] = status
    raw[27:31] = encoded.to_bytes(4, "little")
    raw[31] = punch
    return bytes(raw)


def _firmware_row(raw: bytes, ordinal: int) -> tuple[dict, dict]:
    """Python port of append_terminal_source_record for a 40-byte record."""
    raw_digest = hashlib.sha256(raw).hexdigest()
    terminal_key = hashlib.sha256(f"{SERIAL}:{raw_digest}".encode()).hexdigest()
    timestamp = int.from_bytes(raw[27:31], "little")
    observed_uid = str(int.from_bytes(raw[0:2], "little"))
    field = raw[2:26].split(b"\0", 1)[0]
    observed_user_id = field.decode("latin-1").strip(" ")
    user_id = field.decode("latin-1").rstrip(" ")
    printable = all(32 <= byte <= 126 for byte in field)
    parsed = bool(user_id) and printable and _plausible(timestamp)
    identity_missing = not field.rstrip(b" ")
    disposition = (
        "EVENT" if parsed
        else "INVALID_TIME" if not _plausible(timestamp)
        else "IDENTITY_UNRESOLVED" if identity_missing
        else "MALFORMED"
    )
    row = {
        "ordinal": ordinal,
        "raw_record_digest": raw_digest,
        "terminal_record_key": terminal_key,
        "occurrence_index": ordinal + 1,
        "disposition": disposition,
        "raw_timestamp": timestamp,
        "observed_uid": observed_uid,
    }
    if observed_user_id:
        row["observed_user_id"] = observed_user_id
    event_uid = None
    if parsed:
        user = next((item for item in USERS if item[1] == user_id), None)
        device_time = _iso(timestamp)
        punch = raw[31]
        event_uid = hashlib.sha256(
            (
                f'{{"device_event_time":"{device_time}","device_serial":"{SERIAL}",'
                f'"punch":"{punch}","user_id":"{user_id}"}}'
            ).encode()
        ).hexdigest()
        event = {
            "event_uid": event_uid,
            "terminal_serial": SERIAL,
            "user_id": user_id,
            "device_event_time": device_time,
            "captured_at": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "FULL_HISTORY",
            "status": raw[26],
            "punch": punch,
            "raw_punch": False,
            "clock_quality": "OK",
            "raw_event": {"attendance_record_uid": observed_uid},
        }
        if user is not None:
            event["uid"] = user[0]
            event["terminal_identity_fingerprint"] = _fingerprint(user[0])
            event["raw_name"] = user[2]
            event["raw_event"]["terminal_uid"] = user[0]
        row["event"] = event
    else:
        row["error_code"] = {
            "INVALID_TIME": "IMPLAUSIBLE_TERMINAL_TIME",
            "IDENTITY_UNRESOLVED": "HISTORICAL_IDENTITY_EVIDENCE_REQUIRED",
        }.get(disposition, "UNPARSEABLE_TERMINAL_RECORD")
    row["raw_record_b64"] = base64.b64encode(raw).decode()
    canonical = {
        "disposition": disposition,
        "event_uid": event_uid,
        "occurrence_index": ordinal + 1,
        "ordinal": ordinal,
        "raw_record_digest": raw_digest,
        "terminal_record_key": terminal_key,
    }
    return row, canonical


def k50_log(count: int = 140) -> list[bytes]:
    records = []
    for number in range(1, count + 1):
        user_id = USERS[number % len(USERS)][1]
        if number <= 45:
            # Wrong-clock prefix: zero, year 2000 and impossible encodings.
            encoded = (
                0 if number % 3 == 0
                else _encode(2000, 1, 1 + number % 28, number % 24, number % 60, 0) if number % 3 == 1
                else 0xFFFFFFFF
            )
        else:
            encoded = _encode(2026, 9, 1 + (number % 28), 8 + number % 9, number % 60, number % 60)
        records.append(_record(number, user_id, encoded, punch=number % 2))
    return records


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
        connector = _connector(session, "e0:72:a1:d6:f3:50", SERIAL)
        session.commit()
        yield session, connector


def _connector(session, hardware_id, serial, *, zone="ZONE-BLD9-01"):
    connector, _token, _created = onboard_connector(
        session,
        hardware_id=hardware_id,
        zone_id=zone,
        zone_name=zone,
        device_id="1",
        firmware_version="zone-lite-2.7.4",
        expected_serial=serial,
        actor="test",
        ip_address="127.0.0.1",
    )
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    zkt.serial = serial
    zkt.confirmed_serial = serial
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = True
    zkt.identity_snapshot_stable = True
    zkt.user_count = len(USERS)
    zkt.attendance_count = 140
    zkt.capability_profile = {
        "read_attendance": True,
        "history_stream_v1": True,
        "history_stream_v2": True,
        "history_range_resume_verified": True,
        "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    replace_user_snapshot(
        session,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=f"k50-roster-{hardware_id}",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(uid=uid, user_id=user_id, name=name,
                                terminal_identity_fingerprint=_fingerprint(uid))
                for uid, user_id, name in USERS
            ],
        ),
    )
    zkt.identity_snapshot_stable = True
    return connector


def _anchor_and_first_chunk(session, connector, records):
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Certify the K50 full history baseline from its first record.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="k50-baseline-0001",
    )
    anchor_assignment = assignment_rows(session)[0][1]
    epoch = anchor_assignment["source_epoch"]
    apply_reconciliation_anchor(
        session,
        connector=connector,
        payload=ReconciliationAnchorRequest(
            source_epoch=epoch,
            job_id=job.job_id,
            generation=anchor_assignment["generation"],
            terminal_serial=SERIAL,
            terminal_generation=anchor_assignment["generation"],
            cutoff_count=len(records),
            latest_terminal_count=len(records),
            record_size=40,
            source_total_bytes=4 + 40 * len(records),
            first_anchor_digest=hashlib.sha256(records[0]).hexdigest(),
        ),
    )
    session.flush()
    assignment = assignment_rows(session)[0][1]
    assert assignment["protocol"] == "history_stream_v2"
    start = assignment["committed_next_ordinal"]
    end = start + assignment["chunk_records"]
    rows, canonical = zip(*(_firmware_row(records[i], i) for i in range(start, end)))
    chunk_digest = hashlib.sha256(json.dumps(list(canonical), separators=(",", ":")).encode()).hexdigest()
    resulting = hashlib.sha256(f"{'0' * 64}:{start}:{end}:{chunk_digest}".encode()).hexdigest()
    wire = json.dumps({
        "job_id": job.job_id,
        "assignment_id": assignment["assignment_id"],
        "generation": assignment["generation"],
        "sequence": start,
        "start_ordinal": start,
        "end_ordinal": end,
        "chunk_digest": chunk_digest,
        "previous_chain_digest": None,
        "resulting_chain_digest": resulting,
        "records": list(rows),
        "source_epoch": epoch,
    }, separators=(",", ":"))
    payload = ReconciliationChunkRequest.model_validate(json.loads(wire))
    return job, payload


def test_k50_firmware_shaped_first_chunk_commits_on_a_fresh_job(k50_db):
    session, connector = k50_db
    records = k50_log()
    job, payload = _anchor_and_first_chunk(session, connector, records)
    dispositions = [row.disposition for row in payload.records]
    assert dispositions[:45].count("INVALID_TIME") == 45
    _job, chunk, duplicate = apply_reconciliation_chunk(session, connector=connector, payload=payload)
    assert chunk is not None and not duplicate
    assert job.committed_next_ordinal == 100
    assert job.quarantined_count == 45


def _live_copy(row) -> AttendanceEventIn:
    event = dict(row.event.model_dump(mode="json"))
    event["source"] = "LIVE"
    event["raw_event"] = {"terminal_uid": event.get("uid")}
    return AttendanceEventIn.model_validate(event)


def test_k50_first_chunk_passes_when_live_copies_belong_to_the_same_connector(k50_db):
    session, connector = k50_db
    records = k50_log()
    job, payload = _anchor_and_first_chunk(session, connector, records)
    live = [_live_copy(row) for row in payload.records if row.event is not None][:10]
    ingest_attendance(session, connector=connector, events=live)
    session.flush()
    _job, chunk, _duplicate = apply_reconciliation_chunk(session, connector=connector, payload=payload)
    assert chunk is not None
    assert chunk.already_present_count == 10


def test_k50_first_chunk_reproduces_live_incident_previous_connector_ownership(monkeypatch):
    """Live incident 20 Oct 2026, job 5a916c3a, connector 9d8c589f (ZONE-BLD9-01).

    Production evidence (read-only ADD API, 20 Oct): connector 2ca9a4c2
    (ZONE-SLICTOWER-3FL, now bound to G3 PGB1261200074) owns 1243 attendance
    rows with device_serial ANM4261301077, captured 18 Aug - 7 Sep 2026 while
    the K50 was attached to the 3FL ESP (365 punches from Aug 2026, 870 from
    Sep 2026). 1239 of them carry exactly the firmware build_event_uid of the
    K50 record, so every K50 source replay on BLD9 finds a retained row owned
    by another connector. ADD raises a plain ValueError, rejection.py maps it
    to EVIDENCE_INVALID and the firmware releases with TRANSIENT_STEP_FAILED.
    """
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
    records = k50_log()
    rows = [_firmware_row(raw, ordinal)[0] for ordinal, raw in enumerate(records[:100])]
    with Session(engine) as session:
        # August-September: the K50 was attached to the 3FL ESP.
        tower = _connector(session, "a4:cb:8f:d4:66:64", SERIAL, zone="ZONE-SLICTOWER-3FL")
        copies = []
        for index, row in enumerate(item for item in rows if "event" in item):
            event = dict(row["event"])
            event["source"] = ("LIVE", "CURRENT_RECONCILE", "DUMP_STARTUP")[index % 3]
            copies.append(AttendanceEventIn.model_validate(event))
        accepted, _duplicates = ingest_attendance(session, connector=tower, events=copies)
        assert len(accepted) == len(copies) == 55
        # The 3FL ESP was later moved to its G3; the K50 went to ZONE-BLD9-01.
        tower.zkt_device.serial = tower.zkt_device.confirmed_serial = "PGB1261200074"
        session.flush()
        bld9 = _connector(session, "ac:27:6e:a3:33:28", SERIAL)
        session.flush()
        job, payload = _anchor_and_first_chunk(session, bld9, records)
        assert payload.start_ordinal == 0 and payload.end_ordinal == 100
        with pytest.raises(ValueError) as error:
            apply_reconciliation_chunk(session, connector=bld9, payload=payload)
        assert type(error.value) is ValueError
        assert str(error.value) == "Replayed attendance conflicts with retained source ownership."
        from zk_add.rejection import rejection_category

        assert rejection_category(error.value) == "EVIDENCE_INVALID"
        owner = session.scalar(select(AttendanceEvent.connector_id).where(
            AttendanceEvent.event_uid == payload.records[45].event.event_uid))
        assert owner == tower.id != bld9.id

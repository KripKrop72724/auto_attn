"""queue_evidence exactly as Zone Lite 2.6.15 and 2.7.x put it on the wire.

Every firmware that sends queue_evidence builds it in one place,
add_connector_transfer_queue_evidence() in add_connector.c. That function
arrived in the same commit as ADD's QueueEvidenceRequest (16 Sep 2026) and
emits the same shape in every build since, 2.6.15 through 2.7.6; 2.4.12 and
the August 2.5.2 builds never send it. These tests replay that exact shape
through the device WebSocket path.

A copy whose bytes do not match their own digest must be refused: ADD must
never certify custody of bytes it cannot verify. The firmware keeps the
source row until a matching queue_evidence_ack arrives, so the honest retry
of the same identity must still be accepted afterwards. This is the
MULTAN-02 sequence of 21 Oct 2026: a rejection at the root path [""], then
the same lane accepted seconds later.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import zk_add.web as add_web
from zk_add.db import Base
from zk_add.models import DeviceAlert, DeviceLog, QueueEvidence
from zk_add.realtime import BrowserEventHub
from zk_add.service import onboard_connector
from zk_add.settings import settings

ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_SOURCE = ROOT / "firmware" / "zone_lite" / "main" / "add_connector.c"
RECEIPT_HEADER = ROOT / "firmware" / "zone_lite" / "main" / "evidence_receipt.h"

# qs_generation(): the 32-hex storage instance every 2.6.15 queue name starts with.
INSTANCE = "0123456789abcdef0123456789abcdef"
# 2.6.15 boot ids: twelve hex characters, a dash, then eight.
BOOT_ID = "e072a1000042-a20c2276"
SERIAL = "EVIDENCE-SERIAL"
# A truncated outbox row, the bytes the 2.6.15 outbox worker preserves as MALFORMED.
MALFORMED_ROW = (b'{"type":"attendance_batch","payload":{"schema_version":1,"batch_id":"legacy-0001",'
                 b'"events":[{"event_uid":"zkt:EVIDENCE-SERIAL:1:17","user_id":"1')

# The exact text 2.6.15 sends for that row from the add_bulk_legacy lane
# (cJSON_PrintUnformatted keeps insertion order; seq and sent_at come from
# message_envelope()). CONNECTOR stands for the provisioned connector id.
WIRE_TEXT_2615 = (
    '{"schema_version":"2","message_id":"e072a1000042-a20c2276-8","connector_id":"CONNECTOR",'
    '"boot_id":"e072a1000042-a20c2276","seq":8,"sent_at":"2026-10-21T12:53:57Z","type":"queue_evidence",'
    '"payload":{"schema_version":1,"connector_id":"CONNECTOR","queue":"add_bulk_legacy",'
    '"queue_generation":"0123456789abcdef0123456789abcdef-legacy-1607","record_id":"9746:2879891241",'
    '"payload_digest":"d289e19b1ad86e302fe002b269868bd273149f0fb9e8c624113755417ea02ee9",'
    '"raw_b64":"eyJ0eXBlIjoiYXR0ZW5kYW5jZV9iYXRjaCIsInBheWxvYWQiOnsic2NoZW1hX3ZlcnNpb24iOjEsImJhdGNoX2lkIjoi'
    'bGVnYWN5LTAwMDEiLCJldmVudHMiOlt7ImV2ZW50X3VpZCI6InprdDpFVklERU5DRS1TRVJJQUw6MToxNyIsInVzZXJfaWQiOiIx",'
    '"provenance":{"reason":"MALFORMED","encoding":"LEGACY_ROW"}}}'
)

# Every queue name, generation and record-id format the 2.6.15 call sites
# produce (add_connector.c outbox worker; zone_lite.c ORDS, blocked and
# quarantine drains), with the largest counters they can carry.
U32 = 4294967295
FIRMWARE_SHAPES = {
    "outbox_legacy": ("add_bulk_legacy", f"{INSTANCE}-legacy-1607", "9746:2879891241", "MALFORMED", None),
    "outbox_live_legacy": ("add_live_legacy", f"{INSTANCE}-legacy-{U32}", f"{U32}:{U32}", "MALFORMED", None),
    "outbox_segmented": ("add_bulk", f"{INSTANCE}-segmented-v2", f"{U32}:{U32}:{U32}", "MALFORMED", None),
    "outbox_evidence_lane": ("evidence", f"{INSTANCE}-segmented-v2", "3:4096:17", "MALFORMED", None),
    "ords_segmented": ("ords", INSTANCE, "1:16:3", "IDENTITY_UNRESOLVED", None),
    "ords_legacy": ("ords_legacy", f"{INSTANCE}-legacy-44", "16:3076897152", "IDENTITY_UNRESOLVED", None),
    "blocked_legacy": ("blocked_legacy", f"{INSTANCE}-legacy-446", "0:14986:471223707", "LEGACY_RECOVERY", SERIAL),
    "blocked_segmented": ("blocked", f"{INSTANCE}-segmented-2", "2:500:9", "LEGACY_RECOVERY", SERIAL),
    "blocked_binary": ("blocked_legacy", f"{INSTANCE}-legacy-447", "0:15486:12", "MALFORMED", None),
    "quarantine_backup": ("legacy_add_quarantine_backup", f"{INSTANCE}-quarantine-2-{U32}", f"{U32}:{U32}",
                          "LEGACY_RECOVERY", None),
}
# Bytes those paths can carry: a JSON row, a binary row with NUL and
# non-UTF-8 bytes, and the 1- and 8192-byte bounds both sides enforce.
RAW_SAMPLES = {
    "malformed_row": MALFORMED_ROW,
    "binary_row": b"\x00\xff\xfe" + bytes(range(256)) + b"\n",
    "one_byte": b"{",
    "max_record": (MALFORMED_ROW * 64)[:8192],
}


def firmware_payload(connector_id: str, queue: str, generation: str, record_id: str, raw: bytes,
                     reason: str, terminal_serial: str | None = None) -> dict:
    """add_connector_transfer_queue_evidence(), field for field, in its key order."""
    provenance = {"reason": reason, "encoding": "LEGACY_ROW"}
    if terminal_serial:
        provenance["terminal_serial"] = terminal_serial
    return {
        "schema_version": 1, "connector_id": connector_id, "queue": queue,
        "queue_generation": generation, "record_id": record_id,
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "raw_b64": base64.b64encode(raw).decode("ascii"),
        "provenance": provenance,
    }


def wire_text(connector_id: str, payload: dict, seq: int, sent_at: str = "2026-10-21T12:53:57Z") -> str:
    """message_envelope() + cJSON_PrintUnformatted() for one queue_evidence send."""
    return json.dumps({
        "schema_version": "2", "message_id": f"{BOOT_ID}-{seq}", "connector_id": connector_id,
        "boot_id": BOOT_ID, "seq": seq, "sent_at": sent_at, "type": "queue_evidence", "payload": payload,
    }, separators=(",", ":"))


def receipt_capacities() -> dict[str, int]:
    """The evidence_receipt_t buffers the firmware copies ACK fields into."""
    header = RECEIPT_HEADER.read_text(encoding="utf-8")
    return {name: int(size) for name, size in re.findall(r"\b(\w+)\[(\d+)\]", header)}


def firmware_accepts_ack(ack: dict, expected: dict) -> bool:
    """evidence_identity(root, receipt=true) followed by evidence_receipt_matches()."""
    capacity = receipt_capacities()
    fields = {"connector_id": "connector_id", "queue": "queue", "queue_generation": "generation",
              "record_id": "record_id", "payload_digest": "digest", "receipt_id": "receipt_id",
              "disposition": "disposition"}
    if ack.get("type") != "queue_evidence_ack" or ack.get("schema_version") != 1:
        return False
    for name, buffer in fields.items():
        value = ack.get(name)
        if not isinstance(value, str) or not value or len(value.encode()) >= capacity[buffer]:
            return False
    if len(ack["receipt_id"]) != 36 or ack["disposition"] != "PRESERVED_UNRESOLVED":
        return False
    return all(ack[name] == expected[name]
               for name in ("connector_id", "queue", "queue_generation", "record_id", "payload_digest"))


class FirmwareSocket:
    """The parts of DeviceStreamSession the device path uses; records what the ESP would receive."""

    attached = True
    websocket = None

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.closed: list[int] = []

    async def run_in_thread(self, function, *args):
        return function(*args)

    async def process(self, message_type, work):
        return await work

    async def send_json(self, payload: dict) -> bool:
        self.received.append(payload)
        return True

    async def close(self, code: int, reason: str = "") -> None:
        self.closed.append(code)


@pytest.fixture(params=["sqlite", "postgres"])
def device_stream(request, monkeypatch):
    """A provisioned 2.6.15 connector behind the real WebSocket envelope path."""
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    admin = schema = None
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
    else:
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
        if not url and os.environ.get("CI"):
            url = os.environ.get("ADD_DATABASE_URL")
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "queue_evidence_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
    monkeypatch.setattr(add_web, "browser_events", BrowserEventHub())
    try:
        with sessions() as db:
            connector, _token, _created = onboard_connector(
                db, hardware_id="e0:72:a1:00:00:42", zone_id="ZONE-EVIDENCE", zone_name="ZONE-EVIDENCE",
                device_id="1", firmware_version="2.6.15", expected_serial=SERIAL, actor="test",
                ip_address="127.0.0.1")
            db.commit()
            identity = (connector.id, connector.connector_id)
        yield sessions, *identity
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def deliver(connector_pk: int, connector_id: str, text_frame: str) -> FirmwareSocket:
    socket = FirmwareSocket()
    keep_reading = asyncio.run(add_web.process_device_message(socket, connector_pk, connector_id, text_frame))
    assert keep_reading and socket.closed == []
    return socket


def evidence_rows(sessions) -> list[QueueEvidence]:
    with sessions() as db:
        return list(db.scalars(select(QueueEvidence).order_by(QueueEvidence.id)))


def rejection_entry(sessions, connector_pk: int) -> tuple[DeviceAlert | None, dict | None]:
    with sessions() as db:
        alert = db.scalar(select(DeviceAlert).where(DeviceAlert.connector_id == connector_pk,
                                                    DeviceAlert.code == "DEVICE_MESSAGE_REJECTED"))
        if alert is None:
            return None, None
        return alert, ((alert.details or {}).get("types") or {}).get("queue_evidence")


def corrupt_base64_character(payload: dict) -> dict:
    """One flipped character inside raw_b64: still valid base64 and valid JSON."""
    encoded = payload["raw_b64"]
    index = len(encoded) // 2
    replacement = "B" if encoded[index] != "B" else "C"
    return {**payload, "raw_b64": encoded[:index] + replacement + encoded[index + 1:]}


def test_current_firmware_still_builds_the_2615_shape():
    source = CONNECTOR_SOURCE.read_text(encoding="utf-8")
    start = source.index("bool add_connector_transfer_queue_evidence(")
    body = source[start:source.index("\n}\n", start)]
    fields = re.findall(r'cJSON_Add\w+ToObject\((payload|provenance), "(\w+)"', body)
    assert fields == [
        ("payload", "schema_version"), ("payload", "connector_id"), ("payload", "queue"),
        ("payload", "queue_generation"), ("payload", "record_id"), ("payload", "payload_digest"),
        ("payload", "raw_b64"), ("provenance", "reason"), ("provenance", "encoding"),
        ("provenance", "terminal_serial"), ("payload", "provenance"),
    ]
    assert '"encoding", "LEGACY_ROW"' in body
    assert "raw_length > 8192" in body
    assert "mbedtls_sha256(raw, raw_length, digest, 0)" in body
    assert "mbedtls_base64_encode(encoded, encoded_capacity, &encoded_length, raw, raw_length)" in body
    assert receipt_capacities() == {"connector_id": 101, "queue": 41, "generation": 81, "record_id": 121,
                                    "digest": 65, "receipt_id": 37, "disposition": 40}


def test_wire_builder_reproduces_the_2615_text_exactly():
    payload = firmware_payload("CONNECTOR", "add_bulk_legacy", f"{INSTANCE}-legacy-1607", "9746:2879891241",
                               MALFORMED_ROW, "MALFORMED")
    assert wire_text("CONNECTOR", payload, 8) == WIRE_TEXT_2615


def test_exact_2615_wire_text_is_preserved_and_acknowledged(device_stream):
    sessions, connector_pk, connector_id = device_stream
    socket = deliver(connector_pk, connector_id, WIRE_TEXT_2615.replace("CONNECTOR", connector_id))
    payload = json.loads(WIRE_TEXT_2615.replace("CONNECTOR", connector_id))["payload"]
    [ack] = socket.received
    assert ack["message_id"] == f"{BOOT_ID}-8"
    assert firmware_accepts_ack(ack, payload)
    [row] = evidence_rows(sessions)
    assert (row.queue, row.queue_generation, row.record_id, row.byte_count, row.disposition) == (
        "add_bulk_legacy", f"{INSTANCE}-legacy-1607", "9746:2879891241", len(MALFORMED_ROW),
        "PRESERVED_UNRESOLVED")


def test_every_2615_lane_and_record_size_is_accepted(device_stream):
    sessions, connector_pk, connector_id = device_stream
    sent = []
    seq = 10
    for shape in sorted(FIRMWARE_SHAPES):
        queue, generation, record_id, reason, terminal_serial = FIRMWARE_SHAPES[shape]
        for index, raw_name in enumerate(sorted(RAW_SAMPLES)):
            raw = RAW_SAMPLES[raw_name]
            # Each sample is its own source record: same format, next position.
            identity = record_id if index == 0 else record_id.rsplit(":", 1)[0] + f":{1000 + index}"
            payload = firmware_payload(connector_id, queue, generation, identity, raw, reason, terminal_serial)
            seq += 1
            socket = deliver(connector_pk, connector_id, wire_text(connector_id, payload, seq))
            [ack] = socket.received
            assert firmware_accepts_ack(ack, payload), (shape, raw_name, ack)
            sent.append((queue, generation, identity, payload["payload_digest"], len(raw)))
    rows = evidence_rows(sessions)
    assert [(row.queue, row.queue_generation, row.record_id, row.payload_digest, row.byte_count)
            for row in rows] == sent
    assert len(rows) == len(FIRMWARE_SHAPES) * len(RAW_SAMPLES)
    assert rejection_entry(sessions, connector_pk) == (None, None)


@pytest.mark.parametrize("damage", ["flipped_base64", "non_base64", "flipped_digest", "truncated_base64"])
def test_damaged_copy_is_refused_and_the_honest_retry_is_still_preserved(device_stream, damage):
    sessions, connector_pk, connector_id = device_stream
    honest = firmware_payload(connector_id, "add_bulk_legacy", f"{INSTANCE}-legacy-1607", "9746:2879891241",
                              (MALFORMED_ROW * 40)[:4846], "MALFORMED")
    damaged = {
        "flipped_base64": lambda: corrupt_base64_character(honest),
        "non_base64": lambda: {**honest, "raw_b64": "*" + honest["raw_b64"][1:]},
        "flipped_digest": lambda: {**honest, "payload_digest": ("0" if honest["payload_digest"][0] != "0"
                                                                else "1") + honest["payload_digest"][1:]},
        "truncated_base64": lambda: {**honest, "raw_b64": honest["raw_b64"][:-1]},
    }[damage]()

    socket = deliver(connector_pk, connector_id, wire_text(connector_id, damaged, 8))
    # Only an error naming the message: 2.6.15 releases its ACK waiter
    # unmatched and leaves the source row in place.
    assert socket.received == [{"type": "error", "code": "MESSAGE_REJECTED",
                                "message_id": f"{BOOT_ID}-8", "message_type": "queue_evidence"}]
    assert evidence_rows(sessions) == []
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN"
    assert (entry["category"], entry["error_type"], entry["error_paths"]) == ("SCHEMA_INVALID",
                                                                             "ValidationError", [""])
    with sessions() as db:
        log = db.scalar(select(DeviceLog).where(DeviceLog.connector_id == connector_pk,
                                                DeviceLog.code == "DEVICE_MESSAGE_REJECTED"))
        assert log.context["error_paths"] == [""]
        # Neither the damaged nor the honest bytes reach a rejection record.
        recorded = json.dumps([alert.details, log.context, log.message])
    assert honest["raw_b64"][:32] not in recorded and damaged["raw_b64"][:32] not in recorded

    socket = deliver(connector_pk, connector_id, wire_text(connector_id, honest, 10))
    [ack] = socket.received
    assert firmware_accepts_ack(ack, honest)
    [row] = evidence_rows(sessions)
    assert (row.payload_digest, row.byte_count) == (honest["payload_digest"], 4846)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert alert.details["resolution"]["kind"] == "MESSAGE_ACCEPTED"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(QueueEvidence)) == 1


def test_replayed_honest_copy_returns_the_original_receipt(device_stream):
    sessions, connector_pk, connector_id = device_stream
    payload = firmware_payload(connector_id, "blocked_legacy", f"{INSTANCE}-legacy-446", "0:14986:471223707",
                               MALFORMED_ROW, "LEGACY_RECOVERY", SERIAL)
    first = deliver(connector_pk, connector_id, wire_text(connector_id, payload, 20)).received
    # A reconnect replays the unacknowledged send with a new sequence.
    second = deliver(connector_pk, connector_id, wire_text(connector_id, payload, 21)).received
    assert first[0]["receipt_id"] == second[0]["receipt_id"]
    assert firmware_accepts_ack(second[0], payload)
    assert len(evidence_rows(sessions)) == 1

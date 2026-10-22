"""queue_evidence as Zone Lite builds it, and what ADD keeps about a refused copy.

Every firmware that sends queue_evidence builds it in one function,
add_connector_transfer_queue_evidence() in add_connector.c. It arrived in the
same commit as ADD's QueueEvidenceRequest (a7a44b9, 16 Sep 2026) and has put
the same shape on the wire in every build since, through 2.7.6; 2.4.12 and
the August 2.5.2 builds never send it. The frames below are reconstructed
from that source (message_envelope() and cJSON_PrintUnformatted()), not
captured from a device. FIRMWARE_SHAPES holds the identity formats of the
2.6.15 call sites and of the lanes only 2.7.x sends, at their longest values.

A copy whose bytes do not match their own digest must be refused: ADD never
certifies custody of bytes it cannot verify. The refusal records which check
failed and which record was refused, never the bytes. The device gets only
MESSAGE_REJECTED and keeps its source row, and the rejection stays open until
ADD holds what that copy carried: the same identity with the digest the ESP
claimed or the digest of the bytes that arrived, or, under another generation
of the same queue, the claimed digest or the received digest with the same
record id. An acceptance from another lane, the same identity with other
bytes, or another record whose bytes match what arrived does not hold it, and
an identity conflict needs an operator. A refusal of a copy ADD already holds
(a reconnect overlap) leaves nothing open. The first acceptance after a
refusal, which used to close it, still lifts its HIL and factory hold; what
ADD does not hold stays open as a warning for an operator.
MULTAN-02 (zone-lite-2.6.15) had four such refusals on 8 and 21 Oct 2026. ADD
recorded no identity for them then, so whether each record was held later
cannot be shown from ADD's records alone.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import zlib
from pathlib import Path
from uuid import uuid4

import threading
from datetime import timedelta

import pytest
import sqlalchemy.exc
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import zk_add.web as add_web
from zk_add.db import Base
from zk_add.device_health import evaluate_health
from zk_add.models import Connector, DeviceAlert, DeviceLog, QueueEvidence
from zk_add.queue_evidence import BASE64_INVALID, DIGEST_MISMATCH, IDENTITY_REUSED, LATER_ACCEPTANCE, LENGTH_INVALID
from zk_add.realtime import BrowserEventHub
from zk_add.schemas import Envelope
from zk_add.service import MAX_EVIDENCE_RESOLUTIONS, MAX_PENDING_EVIDENCE, MAX_REJECTION_TYPES, onboard_connector
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_hil_schedule import _known_hold

ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_SOURCE = ROOT / "firmware" / "zone_lite" / "main" / "add_connector.c"
RECEIPT_HEADER = ROOT / "firmware" / "zone_lite" / "main" / "evidence_receipt.h"

# qs_generation(): the 32-hex storage instance every queue generation starts with.
INSTANCE = "0123456789abcdef0123456789abcdef"
# 2.6.15 boot ids: twelve hex characters, a dash, then eight.
BOOT_ID = "e072a1000042-a20c2276"
SERIAL = "EVIDENCE-SERIAL"
# A truncated outbox row, the bytes the outbox worker preserves as MALFORMED.
MALFORMED_ROW = (b'{"type":"attendance_batch","payload":{"schema_version":1,"batch_id":"legacy-0001",'
                 b'"events":[{"event_uid":"zkt:EVIDENCE-SERIAL:1:17","user_id":"1')


def legacy_record_id(offset: int, raw: bytes) -> str:
    """A legacy lane's record id: lq_peek()'s offset and the CRC-32 of the row (dq_crc32 is zlib's)."""
    return f"{offset}:{zlib.crc32(raw)}"


# What 2.6.15 would send for that row from the add_bulk_legacy lane, rebuilt
# from source: cJSON_PrintUnformatted() keeps insertion order, and seq and
# sent_at come from message_envelope(). CONNECTOR stands for the connector id.
WIRE_TEXT_2615 = (
    '{"schema_version":"2","message_id":"e072a1000042-a20c2276-8","connector_id":"CONNECTOR",'
    '"boot_id":"e072a1000042-a20c2276","seq":8,"sent_at":"2026-10-21T12:53:57Z","type":"queue_evidence",'
    '"payload":{"schema_version":1,"connector_id":"CONNECTOR","queue":"add_bulk_legacy",'
    '"queue_generation":"0123456789abcdef0123456789abcdef-legacy-1607","record_id":"9746:894515907",'
    '"payload_digest":"d289e19b1ad86e302fe002b269868bd273149f0fb9e8c624113755417ea02ee9",'
    '"raw_b64":"eyJ0eXBlIjoiYXR0ZW5kYW5jZV9iYXRjaCIsInBheWxvYWQiOnsic2NoZW1hX3ZlcnNpb24iOjEsImJhdGNoX2lkIjoi'
    'bGVnYWN5LTAwMDEiLCJldmVudHMiOlt7ImV2ZW50X3VpZCI6InprdDpFVklERU5DRS1TRVJJQUw6MToxNyIsInVzZXJfaWQiOiIx",'
    '"provenance":{"reason":"MALFORMED","encoding":"LEGACY_ROW"}}}'
)

U32 = 4294967295
U64 = 18446744073709551615
# A sanitized SPIFFS object name at its longest (SRP_NAME_BYTES - 1).
NAME32 = "zone_add_bulk_outbox-2026_jsonl_"
# Every queue name, generation and record-id format the call sites produce,
# with the largest counters they can carry: (queue, generation, record_id,
# provenance reason, terminal serial). These bound lengths and characters;
# ADD does not check the CRC inside a record id.
FIRMWARE_SHAPES = {
    # 2.6.15 and later: add_connector.c outbox worker; zone_lite.c ORDS,
    # blocked and quarantine drains.
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
    # Only 2.7.x: corrupt legacy checkpoints, retained-outbox custody, the
    # boot storage repair (storage_repair.c, storage_repair_plan.c) and the
    # storage recovery (storage_recovery.c, storage_recovery_core.c).
    "live_checkpoint": ("add_live_legacy_checkpoint", f"{INSTANCE}-legacy-{U32}", f"0:{U32}", "MALFORMED", None),
    "bulk_checkpoint": ("add_bulk_legacy_checkpoint", f"{INSTANCE}-legacy-{U32}", f"0:{U32}", "MALFORMED", None),
    "ords_checkpoint": ("ords_legacy_checkpoint", f"{INSTANCE}-legacy-{U32}", f"0:{U32}", "MALFORMED", None),
    "blocked_checkpoint": ("blocked_legacy_checkpoint", f"{INSTANCE}-legacy-{U32}", f"0:0:{U32}",
                           "LEGACY_RECOVERY", None),
    "retained_receipts": ("receipts", f"{INSTANCE}-segmented-v2", f"{U32}:{U32}:{U32}", "LEGACY_RECOVERY", None),
    "storage_repair_gap": ("storage_repair", f"storage-repair-v1-{INSTANCE}", f"gap:{NAME32}:{U32}:{U32}:{U32}",
                           "MALFORMED", None),
    "storage_repair_cursor": ("storage_repair", f"storage-repair-v1-{INSTANCE}", f"cursor:{NAME32}:{U32}:{U32}",
                              "LEGACY_RECOVERY", None),
    "storage_recovery_row": ("blocked_legacy", f"storage-recovery-v1-{U32}-9223372036854775807",
                             f"{U64}:8192:ffffffff", "LEGACY_RECOVERY", SERIAL),
    "storage_recovery_gap": ("blocked_legacy", "storage-recovery-v1-0--1", f"gap:{U64}:{U64}", "MALFORMED", None),
}
# Bytes those paths can carry: a JSON row, a binary row with NUL and
# non-UTF-8 bytes, and the 1- and 8192-byte bounds both sides enforce.
RAW_SAMPLES = {
    "malformed_row": MALFORMED_ROW,
    "binary_row": b"\x00\xff\xfe" + bytes(range(256)) + b"\n",
    "one_byte": b"{",
    "max_record": (MALFORMED_ROW * 64)[:8192],
}
HONEST_ROW = (MALFORMED_ROW * 40)[:4846]


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


def legacy_payload(connector_id: str, raw: bytes = HONEST_ROW, *, queue: str = "add_bulk_legacy",
                   generation: int = 1607, offset: int = 9746) -> dict:
    return firmware_payload(connector_id, queue, f"{INSTANCE}-legacy-{generation}",
                            legacy_record_id(offset, raw), raw, "MALFORMED")


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


def firmware_accepts_ack(ack: dict, expected: dict, message_id: str) -> bool:
    """The ACK waiter's message_id match, then evidence_identity(root, receipt=true)
    and evidence_receipt_matches() (add_connector.c, evidence_receipt.c)."""
    if ack.get("type") != "queue_evidence_ack" or ack.get("message_id") != message_id:
        return False
    if ack.get("schema_version") != 1:
        return False
    capacity = receipt_capacities()
    fields = {"connector_id": "connector_id", "queue": "queue", "queue_generation": "generation",
              "record_id": "record_id", "payload_digest": "digest", "receipt_id": "receipt_id",
              "disposition": "disposition"}
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


def send(connector_pk: int, connector_id: str, payload: dict, seq: int) -> dict:
    """One send; returns the single reply the ESP would receive."""
    [reply] = deliver(connector_pk, connector_id, wire_text(connector_id, payload, seq)).received
    return reply


def held(connector_pk: int, connector_id: str, payload: dict, seq: int) -> None:
    ack = send(connector_pk, connector_id, payload, seq)
    assert firmware_accepts_ack(ack, payload, f"{BOOT_ID}-{seq}"), ack


def refused(connector_pk: int, connector_id: str, payload: dict, seq: int) -> None:
    # Only an error naming the message: the firmware releases its ACK waiter
    # unmatched and leaves the source row in place.
    assert send(connector_pk, connector_id, payload, seq) == {
        "type": "error", "code": "MESSAGE_REJECTED", "message_id": f"{BOOT_ID}-{seq}",
        "message_type": "queue_evidence"}


def evidence_rows(sessions) -> list[QueueEvidence]:
    with sessions() as db:
        return list(db.scalars(select(QueueEvidence).order_by(QueueEvidence.id)))


def rejection_entry(sessions, connector_pk: int, code: str = "DEVICE_MESSAGE_REJECTED",
                    ) -> tuple[DeviceAlert | None, dict | None]:
    """The latest alert of that code and its queue_evidence entry."""
    with sessions() as db:
        alert = db.scalar(select(DeviceAlert).where(DeviceAlert.connector_id == connector_pk,
                                                    DeviceAlert.code == code).order_by(DeviceAlert.id.desc()).limit(1))
        if alert is None:
            return None, None
        return alert, ((alert.details or {}).get("types") or {}).get("queue_evidence")


def identity(payload: dict) -> tuple[str, str, str]:
    return payload["queue"], payload["queue_generation"], payload["record_id"]


def pending_identities(entry: dict) -> list[tuple[str, str, str]]:
    return [(item["queue"], item["queue_generation"], item["record_id"]) for item in entry["pending"]]


def summary(sessions, connector_pk: int) -> str:
    with sessions() as db:
        health = evaluate_health(db, db.get(Connector, connector_pk))
        return next(reason.evidence_summary for reason in health.reasons + health.other_active_alerts
                    if reason.code == "DEVICE_MESSAGE_REJECTED")


def hold(sessions, connector_pk: int) -> dict:
    """What the HIL schedule and factory trial see, and the rejection's derived reason."""
    with sessions() as db:
        connector = db.get(Connector, connector_pk)
        health = evaluate_health(db, connector)
        reason = next((item for item in health.reasons + health.other_active_alerts
                       if item.code == "DEVICE_MESSAGE_REJECTED"), None)
        return {"last_error_code": connector.last_error_code, "known_hold": _known_hold(db, connector),
                "derived_error": health.last_error_code,
                "reason": None if reason is None else (reason.tier, reason.currency, reason.gating,
                                                       reason.operator["resolvable"])}


def corrupt_base64_character(payload: dict) -> dict:
    """One changed character inside raw_b64: still valid base64 and valid JSON."""
    encoded = payload["raw_b64"]
    index = len(encoded) // 2
    replacement = "B" if encoded[index] != "B" else "C"
    return {**payload, "raw_b64": encoded[:index] + replacement + encoded[index + 1:]}


def inserted(payload: dict, extra: str) -> dict:
    """Text a lenient decoder would skip, so it would still decode the honest bytes."""
    encoded = payload["raw_b64"]
    return {**payload, "raw_b64": encoded[:76] + extra + encoded[76:]}


def test_current_firmware_still_builds_the_wire_shape():
    source = CONNECTOR_SOURCE.read_text(encoding="utf-8")
    start = source.index("bool add_connector_transfer_queue_evidence(")
    body = source[start:source.index("\n}\n", start)]
    # The wire contract: keys and their order, whatever the code around them.
    fields = re.findall(r'cJSON_Add\w+ToObject\(\s*(payload|provenance)\s*,\s*"(\w+)"', body)
    assert fields == [
        ("payload", "schema_version"), ("payload", "connector_id"), ("payload", "queue"),
        ("payload", "queue_generation"), ("payload", "record_id"), ("payload", "payload_digest"),
        ("payload", "raw_b64"), ("provenance", "reason"), ("provenance", "encoding"),
        ("provenance", "terminal_serial"), ("payload", "provenance"),
    ]
    assert re.search(r'"encoding"\s*,\s*"LEGACY_ROW"', body)
    # The digest and the base64 cover the same buffer and length, bounded at 8192.
    [hashed] = re.findall(r"mbedtls_sha256\(\s*(\w+)\s*,\s*(\w+)\s*,", body)
    [encoded] = re.findall(r"mbedtls_base64_encode\([^;]*?,\s*(\w+)\s*,\s*(\w+)\s*\)", body)
    assert hashed == encoded
    assert re.search(rf"\b{hashed[1]}\s*>\s*8192\b", body)
    assert receipt_capacities() == {"connector_id": 101, "queue": 41, "generation": 81, "record_id": 121,
                                    "digest": 65, "receipt_id": 37, "disposition": 40}


def test_wire_builder_reproduces_the_reconstructed_2615_text():
    payload = legacy_payload("CONNECTOR", MALFORMED_ROW)
    assert payload["record_id"] == "9746:894515907"
    assert wire_text("CONNECTOR", payload, 8) == WIRE_TEXT_2615


def test_reconstructed_2615_wire_text_is_preserved_and_acknowledged(device_stream):
    sessions, connector_pk, connector_id = device_stream
    socket = deliver(connector_pk, connector_id, WIRE_TEXT_2615.replace("CONNECTOR", connector_id))
    payload = json.loads(WIRE_TEXT_2615.replace("CONNECTOR", connector_id))["payload"]
    [ack] = socket.received
    assert firmware_accepts_ack(ack, payload, f"{BOOT_ID}-8")
    assert not firmware_accepts_ack({**ack, "message_id": f"{BOOT_ID}-9"}, payload, f"{BOOT_ID}-8")
    [row] = evidence_rows(sessions)
    assert (row.queue, row.queue_generation, row.record_id, row.byte_count, row.disposition) == (
        "add_bulk_legacy", f"{INSTANCE}-legacy-1607", "9746:894515907", len(MALFORMED_ROW),
        "PRESERVED_UNRESOLVED")


def test_every_firmware_lane_and_record_size_is_accepted(device_stream):
    sessions, connector_pk, connector_id = device_stream
    assert len(NAME32) == 32
    sent = []
    seq = 10
    for shape in sorted(FIRMWARE_SHAPES):
        queue, generation, record_id, reason, terminal_serial = FIRMWARE_SHAPES[shape]
        for index, raw_name in enumerate(sorted(RAW_SAMPLES)):
            raw = RAW_SAMPLES[raw_name]
            # Each sample is its own source record: same format, next position.
            record = record_id if index == 0 else record_id.rsplit(":", 1)[0] + f":{1000 + index}"
            payload = firmware_payload(connector_id, queue, generation, record, raw, reason, terminal_serial)
            seq += 1
            ack = send(connector_pk, connector_id, payload, seq)
            assert firmware_accepts_ack(ack, payload, f"{BOOT_ID}-{seq}"), (shape, raw_name, ack)
            sent.append((queue, generation, record, payload["payload_digest"], len(raw)))
    rows = evidence_rows(sessions)
    assert [(row.queue, row.queue_generation, row.record_id, row.payload_digest, row.byte_count)
            for row in rows] == sent
    assert len(rows) == len(FIRMWARE_SHAPES) * len(RAW_SAMPLES)
    assert rejection_entry(sessions, connector_pk) == (None, None)


DAMAGE = {
    # name: (damaged copy, reason, what the held honest copy shows)
    "flipped_base64": (corrupt_base64_character, DIGEST_MISMATCH, "SENT_BYTES_CHANGED"),
    "flipped_digest": (lambda honest: {**honest, "payload_digest": (
        "0" if honest["payload_digest"][0] != "0" else "1") + honest["payload_digest"][1:]},
        DIGEST_MISMATCH, "DIGEST_CHANGED"),
    "non_base64": (lambda honest: {**honest, "raw_b64": "*" + honest["raw_b64"][1:]},
                   BASE64_INVALID, "SENT_BYTES_CHANGED"),
    "truncated_base64": (lambda honest: {**honest, "raw_b64": honest["raw_b64"][:-1]},
                         BASE64_INVALID, "SENT_BYTES_CHANGED"),
    # A lenient decoder would skip these and verify the honest bytes; ADD
    # accepts only the exact text the firmware's encoder produces.
    "embedded_newline": (lambda honest: inserted(honest, "\n"), BASE64_INVALID, "SENT_BYTES_CHANGED"),
    "non_alphabet": (lambda honest: inserted(honest, "*"), BASE64_INVALID, "SENT_BYTES_CHANGED"),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_damaged_copy_is_refused_with_its_reason_until_the_honest_retry_is_held(device_stream, damage):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    make, reason, finding = DAMAGE[damage]
    damaged = make(honest)

    refused(connector_pk, connector_id, damaged, 8)
    assert evidence_rows(sessions) == []
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN"
    assert alert.message == f"queue_evidence message was rejected (SCHEMA_INVALID: {reason})."
    assert (entry["category"], entry["error_reason"], entry["error_types"]) == ("SCHEMA_INVALID", reason, [reason])
    [pending] = entry["pending"]
    received = (hashlib.sha256(base64.b64decode(damaged["raw_b64"], validate=True)).hexdigest()
                if reason == DIGEST_MISMATCH else None)
    assert (pending["queue"], pending["queue_generation"], pending["record_id"], pending["claimed_digest"],
            pending["received_digest"], pending["reason"], pending["request_id"]) == (
        *identity(honest), damaged["payload_digest"], received, reason, f"{BOOT_ID}-8")
    assert entry["untracked_refusals"] == 0
    with sessions() as db:
        log = db.scalar(select(DeviceLog).where(DeviceLog.connector_id == connector_pk,
                                                DeviceLog.code == "DEVICE_MESSAGE_REJECTED"))
        assert (log.context["error_reason"], log.context["error_types"]) == (reason, [reason])
        assert log.context["evidence"]["record_id"] == honest["record_id"]
        assert log.context["error_site"].startswith("zk_add.web:persist_envelope:")
        # Neither the damaged nor the honest bytes reach a rejection record.
        recorded = json.dumps([alert.details, log.context, log.message])
    assert honest["raw_b64"][:32] not in recorded and damaged["raw_b64"][100:132] not in recorded
    assert "1 refused record not yet held" in summary(sessions, connector_pk)

    held(connector_pk, connector_id, honest, 10)
    [row] = evidence_rows(sessions)
    assert (row.payload_digest, row.byte_count) == (honest["payload_digest"], len(HONEST_ROW))
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert alert.details["resolution"]["kind"] == "MESSAGE_ACCEPTED"
    [resolution] = alert.details["evidence_resolutions"]
    assert (resolution["record_id"], resolution["match"], resolution["finding"], resolution["reason"],
            resolution["refused_request_id"]) == (honest["record_id"], "SAME_IDENTITY", finding, reason,
                                                  f"{BOOT_ID}-8")
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(QueueEvidence)) == 1


def test_oversized_copy_is_refused_for_its_length(device_stream):
    sessions, connector_pk, connector_id = device_stream
    refused(connector_pk, connector_id, legacy_payload(connector_id, b"x" * 8193), 8)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert entry["error_reason"] == LENGTH_INVALID and entry["pending"][0]["received_bytes"] == 8193


def test_another_lanes_acceptance_does_not_close_a_refusal(device_stream):
    """The safety review's case: R is refused, then S from another lane is held."""
    sessions, connector_pk, connector_id = device_stream
    blocked = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    other = legacy_payload(connector_id)
    refused(connector_pk, connector_id, corrupt_base64_character(blocked), 8)
    held(connector_pk, connector_id, other, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(blocked)]
    assert "evidence_resolutions" not in alert.details
    held(connector_pk, connector_id, blocked, 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert [item["record_id"] for item in alert.details["evidence_resolutions"]] == [blocked["record_id"]]


def test_same_bytes_under_another_generation_of_that_queue_hold_the_refused_record(device_stream):
    sessions, connector_pk, connector_id = device_stream
    first = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(first), 8)
    # The same bytes in another queue do not account for this queue's record.
    held(connector_pk, connector_id, {**first, "queue": "ords_legacy"}, 9)
    assert rejection_entry(sessions, connector_pk)[0].state == "OPEN"
    reblocked = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=416, offset=0)
    held(connector_pk, connector_id, reblocked, 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    [resolution] = alert.details["evidence_resolutions"]
    assert (resolution["queue_generation"], resolution["held_as"]["queue_generation"], resolution["match"],
            resolution["finding"]) == (first["queue_generation"], reblocked["queue_generation"], "SAME_BYTES",
                                       "SENT_BYTES_CHANGED")


def test_a_refusal_without_a_readable_identity_waits_for_an_operator(device_stream):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    refused(connector_pk, connector_id, {**honest, "record_id": "9746 894515907"}, 8)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert (entry["error_paths"], entry["error_types"], entry["error_reason"]) == (
        ["record_id"], ["string_pattern_mismatch"], None)
    assert (entry["pending"], entry["untracked_refusals"]) == ([], 1)
    with sessions() as db:
        log = db.scalar(select(DeviceLog).where(DeviceLog.code == "DEVICE_MESSAGE_REJECTED"))
        assert log.context["evidence"]["record_id"] is None  # a malformed value is never echoed
    # Nothing ADD accepts later can be shown to be that record.
    held(connector_pk, connector_id, honest, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and entry["untracked_refusals"] == 1
    assert "1 unmatched refusal" in summary(sessions, connector_pk)


def test_pending_records_are_bounded(device_stream):
    sessions, connector_pk, connector_id = device_stream
    for index in range(MAX_PENDING_EVIDENCE + 1):
        payload = legacy_payload(connector_id, MALFORMED_ROW + str(index).encode(), offset=index)
        refused(connector_pk, connector_id, corrupt_base64_character(payload), 20 + index)
    # The same record refused again is one pending record, not two.
    refused(connector_pk, connector_id, corrupt_base64_character(
        legacy_payload(connector_id, MALFORMED_ROW + b"0", offset=0)), 60)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert len(entry["pending"]) == MAX_PENDING_EVIDENCE and entry["untracked_refusals"] == 1
    assert entry["count"] == MAX_PENDING_EVIDENCE + 2
    assert next(item for item in entry["pending"] if item["record_id"].startswith("0:"))["count"] == 2


def legacy_rejection(sessions, connector_pk: int) -> None:
    """An open queue_evidence rejection as ADD wrote it before identities were tracked."""
    with sessions() as db:
        db.add(DeviceAlert(connector_id=connector_pk, code="DEVICE_MESSAGE_REJECTED", severity="HIGH", state="OPEN",
                           message="queue_evidence message was rejected (SCHEMA_INVALID).", details={
                               "message_type": "queue_evidence", "types": {"queue_evidence": {
                                   "category": "SCHEMA_INVALID", "error_paths": [""], "count": 2,
                                   "first_at": "2026-10-21T12:34:50+00:00", "last_at": "2026-10-21T12:53:57+00:00",
                                   "request_id": f"{BOOT_ID}-8"}}}))
        db.commit()


def test_a_rejection_recorded_before_identities_were_tracked_keeps_the_old_rule(device_stream):
    sessions, connector_pk, connector_id = device_stream
    legacy_rejection(sessions, connector_pk)
    held(connector_pk, connector_id, legacy_payload(connector_id), 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"


def test_a_new_refusal_keeps_the_earlier_ones_under_their_old_rule(device_stream):
    """Refusals written before identities were tracked never become operator-only holds."""
    sessions, connector_pk, connector_id = device_stream
    legacy_rejection(sessions, connector_pk)
    tracked = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(tracked), 9)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert (entry["count"], entry["legacy_refusals"], entry["untracked_refusals"], len(entry["pending"])) == (
        3, 2, 0, 1)
    assert "1 refused record not yet held" in summary(sessions, connector_pk)
    # Any acceptance clears the two earlier refusals, as before; not the tracked one.
    held(connector_pk, connector_id, legacy_payload(connector_id), 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and (entry["legacy_refusals"], pending_identities(entry)) == (0, [identity(tracked)])
    held(connector_pk, connector_id, tracked, 11)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"


def test_replayed_honest_copy_returns_the_original_receipt(device_stream):
    sessions, connector_pk, connector_id = device_stream
    payload = firmware_payload(connector_id, "blocked_legacy", f"{INSTANCE}-legacy-446", "0:14986:471223707",
                               MALFORMED_ROW, "LEGACY_RECOVERY", SERIAL)
    first = send(connector_pk, connector_id, payload, 20)
    # A reconnect replays the unacknowledged send with a new sequence.
    second = send(connector_pk, connector_id, payload, 21)
    assert first["receipt_id"] == second["receipt_id"]
    assert firmware_accepts_ack(second, payload, f"{BOOT_ID}-21")
    assert len(evidence_rows(sessions)) == 1


def other_bytes(payload: dict, raw: bytes) -> dict:
    """The same identity carrying other bytes, each verifying against its own digest."""
    return {**payload, "payload_digest": hashlib.sha256(raw).hexdigest(),
            "raw_b64": base64.b64encode(raw).decode("ascii")}


def resolutions(alert: DeviceAlert) -> list[tuple[str, str, str]]:
    return [(item["record_id"], item["match"], item["finding"])
            for item in (alert.details or {}).get("evidence_resolutions") or []]


def reject_other_type(connector_pk: int, connector_id: str, message_type: str, seq: int) -> None:
    """A rejection of another message type, recorded as the WebSocket path does."""
    envelope = Envelope.model_validate_json(wire_text(connector_id, {}, seq).replace(
        '"type":"queue_evidence"', f'"type":"{message_type}"'))
    add_web.record_envelope_rejection(connector_pk, envelope, ValueError("bad"))


def test_same_identity_with_other_bytes_never_holds_a_refused_copy(device_stream):
    """Both the digest and the bytes differ from what ADD later holds under that identity."""
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    flipped = ("0" if honest["payload_digest"][0] != "0" else "1") + honest["payload_digest"][1:]
    refused(connector_pk, connector_id, {**corrupt_base64_character(honest), "payload_digest": flipped}, 8)
    held(connector_pk, connector_id, honest, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(honest)]
    assert resolutions(alert) == []


def test_identity_conflict_waits_for_an_operator_even_when_replayed(device_stream):
    """ADD holds X under an identity; the device sends other valid bytes under it (a reused counter)."""
    sessions, connector_pk, connector_id = device_stream
    original = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    held(connector_pk, connector_id, original, 8)
    conflict = other_bytes(original, MALFORMED_ROW + b"-DIFFERENT-PUNCH")
    refused(connector_pk, connector_id, conflict, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.message == f"queue_evidence message was rejected (EVIDENCE_INVALID: {IDENTITY_REUSED})."
    [pending] = entry["pending"]
    assert (pending["reason"], pending["claimed_digest"], pending["received_digest"]) == (
        IDENTITY_REUSED, conflict["payload_digest"], conflict["payload_digest"])
    # A replay of what ADD already holds accounts for the original, not the conflicting copy.
    held(connector_pk, connector_id, original, 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and len(entry["pending"]) == 1 and resolutions(alert) == []
    assert conflict["payload_digest"] not in {row.payload_digest for row in evidence_rows(sessions)}
    assert "1 refused record not yet held (1 is an identity conflict)" in summary(sessions, connector_pk)


def test_provenance_conflict_is_not_held_by_the_bytes_already_held(device_stream):
    sessions, connector_pk, connector_id = device_stream
    original = legacy_payload(connector_id)
    held(connector_pk, connector_id, original, 8)
    changed = {**original, "provenance": {**original["provenance"], "terminal_serial": "ANOTHER-TERMINAL"}}
    refused(connector_pk, connector_id, changed, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    # ADD holds these very bytes, yet it and the device disagree about the record.
    assert entry["pending"][0]["claimed_digest"] == original["payload_digest"]
    assert entry["pending"][0]["reason"] == IDENTITY_REUSED and resolutions(alert) == []
    held(connector_pk, connector_id, legacy_payload(connector_id, MALFORMED_ROW, offset=1), 10)
    assert rejection_entry(sessions, connector_pk)[0].state == "OPEN"


def test_bytes_that_arrived_never_hold_another_records_refusal(device_stream):
    """A's identity and digest with B's bytes (a buffer reused before encoding)."""
    sessions, connector_pk, connector_id = device_stream
    a = legacy_payload(connector_id, MALFORMED_ROW + b"A", queue="blocked_legacy", generation=218, offset=1)
    b = legacy_payload(connector_id, MALFORMED_ROW + b"B", queue="blocked_legacy", generation=218, offset=2)
    refused(connector_pk, connector_id, {**a, "raw_b64": b["raw_b64"]}, 8)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert entry["pending"][0]["received_digest"] == b["payload_digest"]
    held(connector_pk, connector_id, b, 9)
    held(connector_pk, connector_id, legacy_payload(
        connector_id, MALFORMED_ROW + b"B", queue="blocked_legacy", generation=416, offset=2), 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(a)] and resolutions(alert) == []
    held(connector_pk, connector_id, a, 11)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert resolutions(alert) == [(a["record_id"], "SAME_IDENTITY", "SENT_BYTES_CHANGED")]


def test_same_bytes_in_the_same_generation_are_another_record(device_stream):
    sessions, connector_pk, connector_id = device_stream
    first = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(first), 8)
    # A duplicate row at another offset of that generation is its own record.
    held(connector_pk, connector_id, {**first, "record_id": legacy_record_id(500, MALFORMED_ROW)}, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(first)]


def test_add_side_failure_then_the_retry_is_held_as_an_intact_copy(device_stream, monkeypatch):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    real = add_web.preserve_queue_evidence

    def unavailable(*_args, **_kwargs):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("lock timeout"))

    monkeypatch.setattr(add_web, "preserve_queue_evidence", unavailable)
    refused(connector_pk, connector_id, honest, 8)
    monkeypatch.setattr(add_web, "preserve_queue_evidence", real)
    alert, entry = rejection_entry(sessions, connector_pk, "ADD_MESSAGE_PROCESSING_FAILED")
    assert alert.state == "OPEN" and entry["pending"][0]["reason"] == "DATABASE_UNAVAILABLE"
    held(connector_pk, connector_id, honest, 9)
    alert, entry = rejection_entry(sessions, connector_pk, "ADD_MESSAGE_PROCESSING_FAILED")
    assert entry is None and alert.state == "RESOLVED"
    assert resolutions(alert) == [(honest["record_id"], "SAME_IDENTITY", "COPY_INTACT")]


def test_refusal_recorded_after_its_record_is_held_is_resolved_at_once(device_stream):
    """Reconnect overlap: the new socket's honest copy commits before the old socket's refusal.

    The device never sends that record again, so nothing may stay open for it:
    no alert, no HIL or factory hold. The refusal is still on record.
    """
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    held(connector_pk, connector_id, honest, 8)
    refused(connector_pk, connector_id, corrupt_base64_character(honest), 7)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert (alert.details["resolution"]["kind"], alert.details["resolution"]["message_type"]) == (
        "EVIDENCE_HELD", "queue_evidence")
    assert alert.message == f"queue_evidence message was rejected (SCHEMA_INVALID: {DIGEST_MISMATCH})."
    [resolution] = alert.details["evidence_resolutions"]
    [row] = evidence_rows(sessions)
    assert (resolution["record_id"], resolution["match"], resolution["finding"], resolution["refused_request_id"],
            resolution["held_as"]["receipt_id"]) == (
        honest["record_id"], "SAME_IDENTITY", "SENT_BYTES_CHANGED", f"{BOOT_ID}-7", row.receipt_id)
    assert (resolution["received_bytes"], resolution["held_as"]["byte_count"]) == (len(HONEST_ROW),
                                                                                 len(HONEST_ROW))
    status = hold(sessions, connector_pk)
    assert (status["last_error_code"], status["derived_error"], status["reason"]) == (None, None, None)
    assert status["known_hold"] != "CONNECTOR_ERROR_REQUIRES_REVIEW"
    with sessions() as db:
        log = db.scalar(select(DeviceLog).where(DeviceLog.code == "DEVICE_MESSAGE_REJECTED"))
        assert (log.context["error_reason"], log.context["evidence"]["record_id"]) == (
            DIGEST_MISMATCH, honest["record_id"])
    # A later acceptance finds nothing to close.
    held(connector_pk, connector_id, legacy_payload(connector_id, MALFORMED_ROW, offset=1), 9)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(DeviceAlert).where(DeviceAlert.state == "OPEN")) == 0


def test_refusal_recorded_after_a_reblocked_copy_is_held_is_resolved_at_once(device_stream):
    sessions, connector_pk, connector_id = device_stream
    first = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    held(connector_pk, connector_id, legacy_payload(
        connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=416, offset=0), 8)
    refused(connector_pk, connector_id, corrupt_base64_character(first), 7)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert resolutions(alert) == [(first["record_id"], "SAME_BYTES", "SENT_BYTES_CHANGED")]


def test_overlap_refusal_while_another_type_is_rejected_leaves_only_that_type(device_stream):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    reject_other_type(connector_pk, connector_id, "attendance_batch", 6)
    held(connector_pk, connector_id, honest, 8)
    before, _entry = rejection_entry(sessions, connector_pk)
    refused(connector_pk, connector_id, corrupt_base64_character(honest), 7)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.id == before.id and alert.state == "OPEN" and entry is None
    assert list(alert.details["types"]) == ["attendance_batch"]
    assert alert.message == before.message and alert.message.startswith("attendance_batch message was rejected")
    assert resolutions(alert) == [(honest["record_id"], "SAME_IDENTITY", "SENT_BYTES_CHANGED")]
    # The attendance_batch rejection still holds, as on main.
    assert hold(sessions, connector_pk)["last_error_code"] == "DEVICE_MESSAGE_REJECTED"


@pytest.mark.parametrize("record_id", [
    legacy_record_id(0, MALFORMED_ROW),  # outbox and ORDS legacy lanes: offset:crc
    f"0:0:{zlib.crc32(MALFORMED_ROW)}",  # the blocked legacy lane, as MULTAN-02's 0:0:2915066926
])
def test_a_damaged_digest_is_held_by_the_reblocked_row_under_the_same_record_id(device_stream, record_id):
    """The digest, not the bytes, was damaged; the row is re-blocked under a new generation.

    The received digest names the record only with its record id, which for a
    legacy lane ends with the CRC-32 of the row (lq_peek) and so of what arrived.
    """
    sessions, connector_pk, connector_id = device_stream
    generation = f"{INSTANCE}-legacy-218"
    first = firmware_payload(connector_id, "blocked_legacy", generation, record_id, MALFORMED_ROW, "LEGACY_RECOVERY")
    flipped = ("0" if first["payload_digest"][0] != "0" else "1") + first["payload_digest"][1:]
    refused(connector_pk, connector_id, {**first, "payload_digest": flipped}, 8)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert entry["pending"][0]["received_crc32"] == zlib.crc32(MALFORMED_ROW)
    # The same bytes at another record id of another generation are another record.
    held(connector_pk, connector_id, legacy_payload(
        connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=300, offset=7), 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(first)]
    reblocked = {**first, "queue_generation": f"{INSTANCE}-legacy-416"}
    held(connector_pk, connector_id, reblocked, 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    [resolution] = alert.details["evidence_resolutions"]
    assert (resolution["held_as"]["queue_generation"], resolution["match"], resolution["finding"]) == (
        reblocked["queue_generation"], "SAME_BYTES", "DIGEST_CHANGED")


def test_a_record_id_without_the_rows_crc_never_names_the_bytes_that_arrived(device_stream):
    """Segmented ids (segment:offset:sequence) repeat across storage instances: no CRC, no hold."""
    sessions, connector_pk, connector_id = device_stream
    queue, generation, record_id, reason, serial = FIRMWARE_SHAPES["blocked_segmented"]
    first = firmware_payload(connector_id, queue, generation, record_id, MALFORMED_ROW, reason, serial)
    flipped = ("0" if first["payload_digest"][0] != "0" else "1") + first["payload_digest"][1:]
    refused(connector_pk, connector_id, {**first, "payload_digest": flipped}, 8)
    other_instance = generation.replace(INSTANCE, "f" * 32)
    held(connector_pk, connector_id, {**first, "queue_generation": other_instance}, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(first)] and resolutions(alert) == []
    # The device's own retry under the refused identity still holds it.
    held(connector_pk, connector_id, first, 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and resolutions(alert) == [(record_id, "SAME_IDENTITY", "DIGEST_CHANGED")]


def test_a_damaged_digest_overlapping_its_reblocked_row_is_resolved_at_once(device_stream):
    sessions, connector_pk, connector_id = device_stream
    first = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    held(connector_pk, connector_id, legacy_payload(
        connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=416, offset=0), 8)
    flipped = ("0" if first["payload_digest"][0] != "0" else "1") + first["payload_digest"][1:]
    refused(connector_pk, connector_id, {**first, "payload_digest": flipped}, 7)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert resolutions(alert) == [(first["record_id"], "SAME_BYTES", "DIGEST_CHANGED")]


def test_a_later_acceptance_lifts_the_hold_but_keeps_the_unheld_copy_visible(device_stream):
    """As before this tracking, the next accepted queue_evidence ends the HIL and factory hold."""
    sessions, connector_pk, connector_id = device_stream
    blocked = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(blocked), 8)
    current = hold(sessions, connector_pk)
    assert (current["last_error_code"], current["known_hold"], current["derived_error"]) == (
        "DEVICE_MESSAGE_REJECTED", "CONNECTOR_ERROR_REQUIRES_REVIEW", "DEVICE_MESSAGE_REJECTED")
    assert current["reason"] == ("DEGRADED", "CURRENT", True, False)

    held(connector_pk, connector_id, legacy_payload(connector_id), 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(blocked)]
    assert isinstance(entry[LATER_ACCEPTANCE], str)
    released = hold(sessions, connector_pk)
    assert (released["last_error_code"], released["derived_error"]) == (None, None)
    assert released["known_hold"] != "CONNECTOR_ERROR_REQUIRES_REVIEW"
    # Still visible as a warning an operator may resolve now.
    assert released["reason"] == ("WARNING", "LATCHED", False, True)
    assert "1 refused record not yet held, no HIL or factory hold" in summary(sessions, connector_pk)

    # A further acceptance changes nothing; a new refusal makes it current and gating again.
    held(connector_pk, connector_id, legacy_payload(connector_id, MALFORMED_ROW, offset=1), 10)
    assert rejection_entry(sessions, connector_pk)[1][LATER_ACCEPTANCE] == entry[LATER_ACCEPTANCE]
    other = legacy_payload(connector_id, MALFORMED_ROW + b"X", queue="blocked_legacy", generation=218, offset=2)
    refused(connector_pk, connector_id, corrupt_base64_character(other), 11)
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert LATER_ACCEPTANCE not in entry
    assert sorted(pending_identities(entry)) == sorted([identity(blocked), identity(other)])
    assert hold(sessions, connector_pk)["last_error_code"] == "DEVICE_MESSAGE_REJECTED"
    # Holding both closes it.
    held(connector_pk, connector_id, blocked, 12)
    held(connector_pk, connector_id, other, 13)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and alert.state == "RESOLVED"
    assert hold(sessions, connector_pk)["last_error_code"] is None


def test_a_refusal_no_acceptance_follows_keeps_its_hold_however_old(device_stream):
    """As before: only an acceptance, an operator or cleanup ends it. Cleanup now marks it Needs review."""
    sessions, connector_pk, connector_id = device_stream
    blocked = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(blocked), 8)
    with sessions() as db:
        alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "DEVICE_MESSAGE_REJECTED"))
        old = (utc_now() - timedelta(days=3)).isoformat()
        entry = alert.details["types"]["queue_evidence"]
        alert.details = {**alert.details, "types": {"queue_evidence": {**entry, "last_at": old, "first_at": old}}}
        db.get(Connector, connector_pk).boot_id = "e072a1000042-ffffffff"
        db.commit()
    aged = hold(sessions, connector_pk)
    assert (aged["last_error_code"], aged["known_hold"]) == (
        "DEVICE_MESSAGE_REJECTED", "CONNECTOR_ERROR_REQUIRES_REVIEW")
    assert aged["reason"] == ("WARNING", "LATCHED", True, True)


@pytest.mark.parametrize("acceptance_first", [True, False])
def test_acceptance_racing_the_refusal_of_its_damaged_copy_never_leaves_a_hold(device_stream, acceptance_first):
    """Two sockets at once (PostgreSQL): both paths serialize on the connector row lock.

    Whichever commits first, ADD holds the record, no alert stays open and no
    HIL or factory hold remains.
    """
    sessions, connector_pk, connector_id = device_stream
    with sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("SQLite runs one connection; the race needs PostgreSQL row locks")
    rounds = 12
    for index in range(rounds):
        honest = legacy_payload(connector_id, MALFORMED_ROW + str(index).encode(), offset=index)
        frames = [wire_text(connector_id, honest, 100 + 2 * index),
                  wire_text(connector_id, corrupt_base64_character(honest), 101 + 2 * index)]
        if not acceptance_first:
            frames.reverse()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run(frame: str) -> None:
            try:
                barrier.wait(timeout=10)
                deliver(connector_pk, connector_id, frame)
            except BaseException as exc:  # noqa: BLE001 - reported on the main thread
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(frame,)) for frame in frames]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == [] and not any(thread.is_alive() for thread in threads)
    rows = evidence_rows(sessions)
    assert len(rows) == rounds
    with sessions() as db:
        alerts = db.scalars(select(DeviceAlert).where(DeviceAlert.code == "DEVICE_MESSAGE_REJECTED")).all()
        assert [alert.state for alert in alerts if alert.state == "OPEN"] == []
        held_records = sorted(item["record_id"] for alert in alerts
                              for item in (alert.details or {}).get("evidence_resolutions") or [])
    assert held_records == sorted(row.record_id for row in rows)
    status = hold(sessions, connector_pk)
    assert (status["last_error_code"], status["derived_error"]) == (None, None)


def test_each_refused_copy_of_a_record_is_kept(device_stream):
    """A later refusal of the same record never replaces what an earlier copy showed."""
    sessions, connector_pk, connector_id = device_stream
    first = legacy_payload(connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=218, offset=0)
    refused(connector_pk, connector_id, corrupt_base64_character(first), 8)  # the ESP hashed the honest bytes
    flipped = ("0" if first["payload_digest"][0] != "0" else "1") + first["payload_digest"][1:]
    refused(connector_pk, connector_id, {**inserted(first, "*"), "payload_digest": flipped}, 9)  # both damaged
    _alert, entry = rejection_entry(sessions, connector_pk)
    assert [(item["claimed_digest"], item["count"]) for item in entry["pending"]] == [
        (first["payload_digest"], 1), (flipped, 1)]
    held(connector_pk, connector_id, legacy_payload(
        connector_id, MALFORMED_ROW, queue="blocked_legacy", generation=416, offset=0), 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert resolutions(alert) == [(first["record_id"], "SAME_BYTES", "SENT_BYTES_CHANGED")]
    assert alert.state == "OPEN" and [item["claimed_digest"] for item in entry["pending"]] == [flipped]


def test_other_rejected_types_never_evict_unheld_evidence(device_stream):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    refused(connector_pk, connector_id, corrupt_base64_character(honest), 8)
    for index in range(MAX_REJECTION_TYPES):
        reject_other_type(connector_pk, connector_id, f"probe_{index}", 20 + index)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert len(alert.details["types"]) == MAX_REJECTION_TYPES
    assert pending_identities(entry) == [identity(honest)] and "probe_0" not in alert.details["types"]
    for index in range(MAX_REJECTION_TYPES):
        frame = wire_text(connector_id, {}, 40 + index).replace(
            '"type":"queue_evidence"', f'"type":"probe_{index}"')
        deliver(connector_pk, connector_id, frame)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and list(alert.details["types"]) == ["queue_evidence"]
    held(connector_pk, connector_id, honest, 60)
    assert rejection_entry(sessions, connector_pk)[0].state == "RESOLVED"


def test_what_was_held_survives_later_rejections(device_stream):
    sessions, connector_pk, connector_id = device_stream
    a = legacy_payload(connector_id, MALFORMED_ROW + b"A", offset=1)
    c = legacy_payload(connector_id, MALFORMED_ROW + b"C", offset=3)
    d = legacy_payload(connector_id, MALFORMED_ROW + b"D", offset=4)
    refused(connector_pk, connector_id, corrupt_base64_character(a), 8)
    refused(connector_pk, connector_id, corrupt_base64_character(c), 9)
    held(connector_pk, connector_id, a, 10)
    reject_other_type(connector_pk, connector_id, "heartbeat", 11)
    refused(connector_pk, connector_id, corrupt_base64_character(d), 12)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert resolutions(alert) == [(a["record_id"], "SAME_IDENTITY", "SENT_BYTES_CHANGED")]
    assert sorted(pending_identities(entry)) == sorted([identity(c), identity(d)])
    held(connector_pk, connector_id, c, 13)
    alert, _entry = rejection_entry(sessions, connector_pk)
    assert [record for record, _match, _finding in resolutions(alert)] == [a["record_id"], c["record_id"]]


def test_held_findings_are_bounded(device_stream):
    sessions, connector_pk, connector_id = device_stream
    # An unmatched refusal keeps the alert open while the others are held.
    refused(connector_pk, connector_id, {**legacy_payload(connector_id), "record_id": "9746 1"}, 99)
    records = [legacy_payload(connector_id, MALFORMED_ROW + str(index).encode(), offset=index)
               for index in range(MAX_EVIDENCE_RESOLUTIONS + 2)]
    for index, record in enumerate(records):
        refused(connector_pk, connector_id, corrupt_base64_character(record), 100 + index)
        held(connector_pk, connector_id, record, 200 + index)
    alert, _entry = rejection_entry(sessions, connector_pk)
    assert [record for record, _match, _finding in resolutions(alert)] == [
        record["record_id"] for record in records[-MAX_EVIDENCE_RESOLUTIONS:]]


def test_a_copy_naming_another_connector_is_held_by_its_honest_retry(device_stream):
    sessions, connector_pk, connector_id = device_stream
    honest = legacy_payload(connector_id)
    refused(connector_pk, connector_id, {**honest, "connector_id": "ANOTHER-CONNECTOR"}, 8)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.message == ("queue_evidence message was rejected "
                             "(EVIDENCE_INVALID: QUEUE_EVIDENCE_CONNECTOR_MISMATCH).")
    held(connector_pk, connector_id, honest, 9)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert entry is None and resolutions(alert) == [(honest["record_id"], "SAME_IDENTITY", "COPY_INTACT")]


def test_a_2615_ords_row_delivered_to_oracle_instead_waits_for_an_operator(device_stream):
    """2.6.x re-runs oracle_send_live() on retry: once the user maps, the row goes to ORDS, not ADD.

    Its Oracle receipt names only the event_uid. ADD cannot tie that to the
    refused copy's digest, and the uid inside the bytes that arrived may be
    another record's, so the refusal stays for an operator to verify.
    """
    sessions, connector_pk, connector_id = device_stream
    event_uid = hashlib.sha256(b"ords-event").hexdigest()
    row = json.dumps({"event_uid": event_uid, "user_id": "17", "event_time": "2026-10-21T12:30:00+05:00"},
                     separators=(",", ":")).encode()
    queue, generation, record_id, reason, _serial = FIRMWARE_SHAPES["ords_segmented"]
    ords = firmware_payload(connector_id, queue, generation, record_id, row, reason)
    refused(connector_pk, connector_id, corrupt_base64_character(ords), 8)
    receipt = wire_text(connector_id, {"confirmation_path": "FIRMWARE_LIVE",
                                       "oracle_observed_at": "2026-10-21T12:31:00Z", "event_uids": [event_uid]},
                        9).replace('"type":"queue_evidence"', '"type":"oracle_receipt_batch"')
    [reply] = deliver(connector_pk, connector_id, receipt).received
    assert reply.get("type") != "error", reply
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and pending_identities(entry) == [identity(ords)]
    assert "1 refused record not yet held" in summary(sessions, connector_pk)

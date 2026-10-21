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
ADD holds that record; an acceptance from another lane does not close it.
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

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import zk_add.web as add_web
from zk_add.db import Base
from zk_add.device_health import evaluate_health
from zk_add.models import Connector, DeviceAlert, DeviceLog, QueueEvidence
from zk_add.queue_evidence import BASE64_INVALID, DIGEST_MISMATCH, LENGTH_INVALID
from zk_add.realtime import BrowserEventHub
from zk_add.service import MAX_PENDING_EVIDENCE, onboard_connector
from zk_add.settings import settings

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


def rejection_entry(sessions, connector_pk: int) -> tuple[DeviceAlert | None, dict | None]:
    with sessions() as db:
        alert = db.scalar(select(DeviceAlert).where(DeviceAlert.connector_id == connector_pk,
                                                    DeviceAlert.code == "DEVICE_MESSAGE_REJECTED"))
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


def test_same_bytes_under_a_later_generation_of_that_queue_hold_the_refused_record(device_stream):
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


def test_a_new_refusal_never_drops_the_unmatched_ones_before_it(device_stream):
    sessions, connector_pk, connector_id = device_stream
    legacy_rejection(sessions, connector_pk)
    refused(connector_pk, connector_id, corrupt_base64_character(legacy_payload(connector_id)), 9)
    _alert, entry = rejection_entry(sessions, connector_pk)
    # Its two earlier refusals have no identity to match: they stay unmatched.
    assert (entry["count"], entry["untracked_refusals"], len(entry["pending"])) == (3, 2, 1)
    held(connector_pk, connector_id, legacy_payload(connector_id), 10)
    alert, entry = rejection_entry(sessions, connector_pk)
    assert alert.state == "OPEN" and (entry["pending"], entry["untracked_refusals"]) == ([], 2)


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
